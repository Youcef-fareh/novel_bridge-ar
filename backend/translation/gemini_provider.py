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


def _build_safety_settings(types):
    harm_category = getattr(types, "HarmCategory", None)
    block_threshold = getattr(types, "HarmBlockThreshold", None)
    safety_setting = getattr(types, "SafetySetting", None)
    if harm_category is None or block_threshold is None or safety_setting is None:
        return []

    categories = (
        harm_category.HARM_CATEGORY_HARASSMENT,
        harm_category.HARM_CATEGORY_HATE_SPEECH,
        harm_category.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        harm_category.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
    return [
        safety_setting(
            category=category,
            threshold=block_threshold.BLOCK_ONLY_HIGH,
        )
        for category in categories
    ]


def _response_text(response) -> str:
    try:
        translated = response.text or ""
    except (AttributeError, ValueError):
        translated = ""
    if translated.strip():
        return translated.strip()

    text_parts = []
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            part_text = getattr(part, "text", None)
            if part_text:
                text_parts.append(part_text)
    if text_parts:
        return "".join(text_parts).strip()

    details = []
    prompt_feedback = getattr(response, "prompt_feedback", None)
    block_reason = getattr(prompt_feedback, "block_reason", None)
    if block_reason:
        details.append(f"prompt block reason: {block_reason}")
    for candidate in candidates:
        finish_reason = getattr(candidate, "finish_reason", None)
        if finish_reason:
            details.append(f"finish reason: {finish_reason}")
            break
    suffix = f" ({'; '.join(details)})" if details else ""
    raise RuntimeError(f"Gemini returned no text{suffix}.")


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
                safety_settings=_build_safety_settings(types),
            ),
        )
        return _response_text(response)


