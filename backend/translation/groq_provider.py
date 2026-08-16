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

_API_KEY = os.getenv("GROQ_API_KEY", "")
_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


class GroqProvider(TranslationProvider):
    provider_name = "groq"

    def is_available(self) -> bool:
        return bool(_API_KEY and _API_KEY != "your_groq_api_key_here")

    async def translate_chapter(self, text: str, glossary: List[GlossaryRule]) -> str:
        if not self.is_available():
            raise RuntimeError("Groq API key not configured. Please set GROQ_API_KEY in your .env file.")

        system_prompt = build_system_prompt(glossary)

        result = await asyncio.get_event_loop().run_in_executor(
            None, self._call_api, system_prompt, text
        )
        return apply_glossary_postpass(result, glossary)

    @retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_exponential(multiplier=1, min=4, max=60),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _call_api(self, system_prompt: str, text: str) -> str:
        from groq import Groq
        client = Groq(api_key=_API_KEY)
        response = client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": text},
            ],
            temperature=0.3,
            max_tokens=8192,
        )
        translated = response.choices[0].message.content.strip()
        if not translated:
            raise RuntimeError("Groq returned an empty response.")
        return translated
