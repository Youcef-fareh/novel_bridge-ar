"""
NovelBridge — Gemini translation provider (primary).
Uses Google's Gemini API via official google-genai SDK.
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

_API_KEY = os.getenv("GEMINI_API_KEY", "")
_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Lazy-initialise client to avoid errors if key is missing
_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=_API_KEY)
    return _client


class GeminiProvider(TranslationProvider):
    provider_name = "gemini"

    def is_available(self) -> bool:
        return bool(_API_KEY and _API_KEY != "your_gemini_api_key_here")

    async def translate_chapter(self, text: str, glossary: List[GlossaryRule]) -> str:
        if not self.is_available():
            raise RuntimeError("Gemini API key not configured. Please set GEMINI_API_KEY in your .env file.")

        system_prompt = build_system_prompt(glossary)

        # Run the blocking genai call in a thread pool
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
        from google.genai import types

        client = _get_client()
        response = client.models.generate_content(
            model=_MODEL,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
            ),
        )
        translated = response.text.strip() if response.text else ""
        if not translated:
            raise RuntimeError("Gemini returned an empty response.")
        return translated

