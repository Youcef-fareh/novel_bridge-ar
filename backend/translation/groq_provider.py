"""
NovelBridge — Groq translation provider (fallback).
Uses the Groq free API (llama-3.3-70b-versatile or similar).
"""
from __future__ import annotations
from typing import Optional
import asyncio
import os
import re
from typing import List
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

load_dotenv()

from backend.models import GlossaryRule
from backend.translation.base import (
    TranslationProvider,
    TruncatedOutputError,
    apply_glossary_postpass,
    build_system_prompt,
    is_retryable_error,
    is_streaming_unsupported_error,
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
        """Split *text* into chunks of at most *max_chars*, preserving paragraphs.

        Normal paragraphs pack into chunks joined with "\\n\\n". Oversized
        paragraphs are split at sentence boundaries and their pieces pack into
        chunks joined with a single "\\n" — so a paragraph's pieces never get
        separated by a fabricated blank line inside a chunk. Only unavoidable
        chunk boundaries (API call limits) fall mid-paragraph.
        """
        chunks: List[str] = []
        current: List[str] = []
        current_seps: List[str] = []  # separator preceding each segment
        current_length = 0

        def flush() -> None:
            nonlocal current, current_seps, current_length
            if current:
                chunks.append(
                    current[0]
                    + "".join(sep + seg for sep, seg in zip(current_seps[1:], current[1:]))
                )
                current, current_seps, current_length = [], [], 0

        def add(segment: str, sep: str) -> None:
            """Pack *segment* into the chunk being built, using *sep* before it."""
            nonlocal current_length
            if current and current_length + len(sep) + len(segment) > max_chars:
                flush()
            current_seps.append("" if not current else sep)
            current.append(segment)
            current_length += len(segment) + (len(sep) if len(current) > 1 else 0)

        for paragraph in text.split("\n\n"):
            if len(paragraph) <= max_chars:
                add(paragraph, "\n\n")
                continue

            # Oversized paragraph: split at sentence boundaries, packing the
            # pieces with soft ("\n") breaks only.
            pieces: List[str] = []
            buf = ""
            for sentence in re.split(r"(?<=[.!?؟。！？])\s+", paragraph):
                candidate = sentence if not buf else buf + " " + sentence
                if len(candidate) <= max_chars:
                    buf = candidate
                    continue
                if buf:
                    pieces.append(buf)
                    buf = ""
                # The sentence alone may still exceed max_chars: hard-split
                # at word boundaries.
                while len(sentence) > max_chars:
                    cut = sentence.rfind(" ", 0, max_chars + 1)
                    if cut <= 0:
                        cut = max_chars
                    pieces.append(sentence[:cut].rstrip())
                    sentence = sentence[cut:].lstrip()
                buf = sentence
            if buf:
                pieces.append(buf)

            for index, piece in enumerate(pieces):
                # The first piece continues the previous *paragraph* boundary
                # ("\n\n"); only later pieces of the SAME oversized paragraph
                # use a soft "\n" break.
                add(piece, "\n\n" if index == 0 else "\n")

        flush()
        return chunks or [text]

    async def translate_chapter(
        self,
        text: str,
        glossary: List[GlossaryRule],
        model: Optional[str] = None,
        context: Optional[str] = None,
    ) -> str:
        if not self.is_available():
            raise RuntimeError("Groq API key not configured. Please set GROQ_API_KEY in your .env or API Keys settings.")

        system_prompt = build_system_prompt(glossary, context)
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
        retry=retry_if_exception(is_retryable_error),
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
            finish_reason = None
            for chunk in completion:
                chunk_text = self._stream_text(chunk)
                if chunk_text:
                    content_parts.append(chunk_text)
                choices = self._field(chunk, "choices", []) or []
                if choices:
                    finish_reason = self._field(choices[0], "finish_reason") or finish_reason
            translated = "".join(content_parts).strip()
            if finish_reason == "length":
                raise TruncatedOutputError(
                    f"Groq ({model}) hit its output-token limit mid-chunk; "
                    "the translation would be incomplete."
                )
            if not translated:
                logger.warning("Groq stream returned no content, trying non-streaming completion")
                fallback_options = dict(request_options)
                fallback_options["stream"] = False
                response = client.chat.completions.create(
                    **fallback_options,
                )
                translated = self._response_text(response)
        except TruncatedOutputError:
            raise
        except Exception as stream_err:
            # Only fall back to non-streaming for streaming-specific failures.
            # Retrying auth/quota/network errors with a second (paid) request
            # would hide the real failure behind a duplicate call.
            if not is_streaming_unsupported_error(stream_err):
                raise
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

