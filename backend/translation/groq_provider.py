"""
NovelBridge — Groq translation provider (fallback).
Uses the Groq free API (llama-3.3-70b-versatile or similar).
"""
from __future__ import annotations
from typing import Optional
import asyncio
import os
from typing import List
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

load_dotenv()

from backend.models import GlossaryRule
from backend.translation.base import (
    TranslationProvider,
    apply_glossary_postpass,
    build_system_prompt,
)

import logging

logger = logging.getLogger("novelbridge.translation.groq")

_API_KEY = os.getenv("GROQ_API_KEY", "")
_MODEL   = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

GROQ_MODELS = [
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
]

MAX_CHUNK_CHARS = 6000
MAX_COMPLETION_TOKENS = 8192


class GroqProvider(TranslationProvider):
    provider_name = "groq"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self._model = model or os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

    def is_available(self) -> bool:
        key = self._api_key or os.getenv("GROQ_API_KEY", "")
        return bool(key and key.strip() not in ("your_groq_api_key_here", "<YOUR_API_KEY>"))

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        """Groq's SDK appends /openai/v1 to its base URL automatically."""
        parsed = urlsplit(base_url.strip().rstrip("/"))
        path = parsed.path.rstrip("/")
        for suffix in ("/openai/v1", "/openai"):
            if path.endswith(suffix):
                path = path[: -len(suffix)] or "/"
                break
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)).rstrip("/")

    @staticmethod
    def _field(value, name: str, default=None):
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    @classmethod
    def _message_text(cls, message) -> str:
        content = cls._field(message, "content", "")
        if isinstance(content, list):
            content = "".join(
                cls._field(part, "text", "") or ""
                for part in content
                if cls._field(part, "text", "")
            )
        return (content or "").strip()

    @classmethod
    def _response_text(cls, response) -> str:
        choices = cls._field(response, "choices", []) or []
        if not choices:
            return ""
        message = cls._field(choices[0], "message")
        return cls._message_text(message) if message else ""

    @classmethod
    def _stream_text(cls, chunk) -> str:
        choices = cls._field(chunk, "choices", []) or []
        if not choices:
            return ""
        delta = cls._field(choices[0], "delta")
        if not delta:
            return ""
        content = cls._field(delta, "content", "")
        if isinstance(content, list):
            content = "".join(
                cls._field(part, "text", "") or ""
                for part in content
                if cls._field(part, "text", "")
            )
        return content or ""

    @staticmethod
    def _request_options(model: str, messages: List[dict]) -> dict:
        options = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if model.startswith("openai/gpt-oss"):
            options.update(
                reasoning_effort="medium",
                max_completion_tokens=MAX_COMPLETION_TOKENS,
                temperature=1,
                top_p=1,
                stop=None,
            )
        elif model.startswith("qwen/qwen3.8"):
            options.update(
                reasoning_effort="default",
                max_completion_tokens=MAX_COMPLETION_TOKENS,
                temperature=0.6,
                top_p=0.95,
                stop=None,
            )
        return options

    @staticmethod
    def _split_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> List[str]:
        paragraphs = text.split("\n\n")
        chunks: List[str] = []
        current: List[str] = []
        current_length = 0

        for paragraph in paragraphs:
            if len(paragraph) > max_chars:
                if current:
                    chunks.append("\n\n".join(current))
                    current, current_length = [], 0
                chunks.extend(
                    paragraph[index:index + max_chars]
                    for index in range(0, len(paragraph), max_chars)
                )
                continue

            added_length = len(paragraph) + (2 if current else 0)
            if current and current_length + added_length > max_chars:
                chunks.append("\n\n".join(current))
                current, current_length = [], 0
            current.append(paragraph)
            current_length += len(paragraph) + (2 if len(current) > 1 else 0)

        if current:
            chunks.append("\n\n".join(current))
        return chunks or [text]

    async def translate_chapter(
        self,
        text: str,
        glossary: List[GlossaryRule],
        model: Optional[str] = None,
    ) -> str:
        if not self.is_available():
            raise RuntimeError("Groq API key not configured. Please set GROQ_API_KEY in your .env or API Keys settings.")

        system_prompt = build_system_prompt(glossary)
        model_to_use = model or self._model or "openai/gpt-oss-20b"

        translated_chunks = []
        for chunk in self._split_text(text):
            translated_chunks.append(
                await asyncio.get_event_loop().run_in_executor(
                    None, self._call_api, system_prompt, chunk, model_to_use
                )
            )
        return apply_glossary_postpass("\n\n".join(translated_chunks), glossary)

    @retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _call_api(self, system_prompt: str, text: str, model: str) -> str:
        from groq import Groq
        key = self._api_key or os.getenv("GROQ_API_KEY", "")
        configured_base_url = os.getenv("GROQ_BASE_URL", "https://api.groq.com")
        client = Groq(api_key=key, base_url=self._normalize_base_url(configured_base_url))

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": text},
        ]
        request_options = self._request_options(model, messages)

        try:
            completion = client.chat.completions.create(
                **request_options,
            )
            content_parts: List[str] = []
            for chunk in completion:
                chunk_text = self._stream_text(chunk)
                if chunk_text:
                    content_parts.append(chunk_text)
            translated = "".join(content_parts).strip()
            if not translated:
                logger.warning("Groq stream returned no content, trying non-streaming completion")
                fallback_options = dict(request_options)
                fallback_options["stream"] = False
                response = client.chat.completions.create(
                    **fallback_options,
                )
                translated = self._response_text(response)
        except Exception as stream_err:
            logger.warning(f"Groq streaming completion failed, trying non-streaming: {stream_err}")
            fallback_options = dict(request_options)
            fallback_options["stream"] = False
            response = client.chat.completions.create(
                **fallback_options,
            )
            translated = self._response_text(response)

        if not translated:
            raise RuntimeError(f"Groq ({model}) returned an empty response.")
        return translated


