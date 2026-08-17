"""
NovelBridge — Groq translation provider (fallback).
Uses the Groq free API (llama-3.3-70b-versatile or similar).
"""
from __future__ import annotations

import asyncio
import os
from typing import List

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
    "qwen/qwen3.6-27b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
]


class GroqProvider(TranslationProvider):
    provider_name = "groq"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self._model = model or os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

    def is_available(self) -> bool:
        key = self._api_key or os.getenv("GROQ_API_KEY", "")
        return bool(key and key.strip() not in ("your_groq_api_key_here", "<YOUR_API_KEY>"))

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

        result = await asyncio.get_event_loop().run_in_executor(
            None, self._call_api, system_prompt, text, model_to_use
        )
        return apply_glossary_postpass(result, glossary)

    @retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _call_api(self, system_prompt: str, text: str, model: str) -> str:
        from groq import Groq
        key = self._api_key or os.getenv("GROQ_API_KEY", "")
        client = Groq(api_key=key)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": text},
        ]

        try:
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
            )
            content_parts: List[str] = []
            for chunk in completion:
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        content_parts.append(delta.content)
            translated = "".join(content_parts).strip()
        except Exception as stream_err:
            logger.warning(f"Groq streaming completion failed, trying non-streaming: {stream_err}")
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=False,
            )
            translated = response.choices[0].message.content.strip() if response.choices else ""

        if not translated:
            raise RuntimeError(f"Groq ({model}) returned an empty response.")
        return translated


