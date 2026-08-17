"""
NovelBridge — TokenRouter translation provider (OpenAI-compatible).
Supports DeepSeek, Qwen, and any OpenAI-compatible models via TokenRouter API.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional

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

logger = logging.getLogger("novelbridge.translation.tokenrouter")

DEFAULT_BASE_URL = "https://api.tokenrouter.com/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-pro-0813-free"

TOKENROUTER_MODELS = [
    "deepseek/deepseek-v4-pro-0813-free",
    "qwen/qwen3.8-max-free",
    "deepseek/deepseek-chat",
    "deepseek/deepseek-reasoner",
    "qwen/qwen-2.5-72b-instruct",
]


class TokenRouterProvider(TranslationProvider):
    provider_name = "tokenrouter"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self._api_key = (
            api_key
            or os.getenv("TOKENROUTER_API_KEY", "")
            or os.getenv("DEEPSEEK_API_KEY", "")
        )
        self._base_url = (
            base_url
            or os.getenv("TOKENROUTER_BASE_URL", "")
            or DEFAULT_BASE_URL
        )
        self._model = (
            model
            or os.getenv("TOKENROUTER_MODEL", "")
            or DEFAULT_MODEL
        )

    def is_available(self) -> bool:
        key = self._api_key or os.getenv("TOKENROUTER_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
        return bool(key and key.strip() not in ("your_tokenrouter_api_key_here", "your_deepseek_api_key_here", "<YOUR_API_KEY>"))

    async def translate_chapter(
        self,
        text: str,
        glossary: List[GlossaryRule],
        model: Optional[str] = None,
    ) -> str:
        if not self.is_available():
            raise RuntimeError(
                "TokenRouter API key not configured. "
                "Please set TOKENROUTER_API_KEY in your .env or API Keys settings."
            )

        system_prompt = build_system_prompt(glossary)
        model_to_use = model or self._model or DEFAULT_MODEL

        # Run the synchronous API call in a thread pool
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
        api_key = (
            self._api_key
            or os.getenv("TOKENROUTER_API_KEY", "")
            or os.getenv("DEEPSEEK_API_KEY", "")
        )
        base_url = (
            self._base_url
            or os.getenv("TOKENROUTER_BASE_URL", "")
            or DEFAULT_BASE_URL
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]

        # Use OpenAI client SDK
        try:
            from openai import OpenAI

            client = OpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=120.0,
            )

            try:
                stream = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=True,
                    stream_options={"include_usage": True},
                    extra_body={},
                )
                content_parts: List[str] = []
                for chunk in stream:
                    if chunk.choices:
                        delta = chunk.choices[0].delta
                        if delta and delta.content:
                            content_parts.append(delta.content)
                translated = "".join(content_parts).strip()
            except Exception as stream_err:
                logger.warning(f"TokenRouter stream completion failed, trying non-streaming: {stream_err}")
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=False,
                )
                translated = response.choices[0].message.content.strip() if response.choices else ""

        except ImportError:
            # Fallback to direct httpx request if openai is not yet imported
            import httpx

            url = f"{base_url.rstrip('/')}/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
            }
            with httpx.Client(timeout=120.0) as http_client:
                resp = http_client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                translated = data["choices"][0]["message"]["content"].strip()

        if not translated:
            raise RuntimeError(f"TokenRouter ({model}) returned an empty response.")

        return translated
