"""
NovelBridge - xAI Grok translation provider (OpenAI-compatible).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional

from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

load_dotenv()

from backend.models import GlossaryRule
from backend.translation.base import (
    TranslationProvider,
    apply_glossary_postpass,
    build_system_prompt,
)

logger = logging.getLogger("novelbridge.translation.grok")

DEFAULT_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-3-mini"

GROK_MODELS = [
    "grok-3-mini",
    "grok-3",
    "grok-2-1212",
]


class GrokProvider(TranslationProvider):
    provider_name = "grok"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self._api_key = api_key or os.getenv("XAI_API_KEY", "") or os.getenv("GROK_API_KEY", "")
        self._base_url = base_url or os.getenv("XAI_BASE_URL", "") or os.getenv("GROK_BASE_URL", "") or DEFAULT_BASE_URL
        self._model = model or os.getenv("GROK_MODEL", "") or DEFAULT_MODEL

    def is_available(self) -> bool:
        key = self._api_key or os.getenv("XAI_API_KEY", "") or os.getenv("GROK_API_KEY", "")
        return bool(key and key.strip() not in ("your_xai_api_key_here", "your_grok_api_key_here", "<YOUR_API_KEY>"))

    async def translate_chapter(
        self,
        text: str,
        glossary: List[GlossaryRule],
        model: Optional[str] = None,
        context: Optional[str] = None,
    ) -> str:
        if not self.is_available():
            raise RuntimeError("Grok API key not configured. Please set XAI_API_KEY in your .env or API Keys settings.")

        system_prompt = build_system_prompt(glossary, context)
        model_to_use = model or self._model or DEFAULT_MODEL
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
        from openai import OpenAI

        api_key = self._api_key or os.getenv("XAI_API_KEY", "") or os.getenv("GROK_API_KEY", "")
        base_url = self._base_url or DEFAULT_BASE_URL
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=120.0)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            stream=False,
        )
        translated = response.choices[0].message.content.strip() if response.choices else ""
        if not translated:
            raise RuntimeError(f"Grok ({model}) returned an empty response.")
        return translated