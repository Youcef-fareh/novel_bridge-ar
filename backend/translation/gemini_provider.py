"""
NovelBridge — Gemini translation provider (primary).
Uses Google's Gemini API via official google-genai SDK.
"""
from __future__ import annotations
from typing import Optional
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

# Kept for compatibility with older callers; the runtime cache is instance-scoped.
_client = None


# The client must be instance-scoped so different API keys/settings do not leak
# across provider objects created at different times or by tests.


GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-2.0-flash",
]


class GeminiProvider(TranslationProvider):
    provider_name = "gemini"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self._model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def is_available(self) -> bool:
        key = self._api_key or os.getenv("GEMINI_API_KEY", "")
        return bool(key and key.strip() not in ("your_gemini_api_key_here", "<YOUR_API_KEY>"))

    async def translate_chapter(
        self,
        text: str,
        glossary: List[GlossaryRule],
        model: Optional[str] = None,
    ) -> str:
        if not self.is_available():
            raise RuntimeError("Gemini API key not configured. Please set GEMINI_API_KEY in your .env or API Keys settings.")

        system_prompt = build_system_prompt(glossary)
        model_to_use = model or self._model or "gemini-2.5-flash"

        # Run the blocking genai call in a thread pool
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
        from google.genai import types

        client = self._get_client()
        response = client.models.generate_content(
            model=model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
            ),
        )
        translated = response.text.strip() if response.text else ""
        if not translated:
            raise RuntimeError("Gemini returned an empty response.")
        return translated


