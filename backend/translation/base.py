"""
NovelBridge — Translation provider base class.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import List

from backend.models import GlossaryRule


def build_system_prompt(glossary: List[GlossaryRule]) -> str:
    """Build the system prompt with glossary rules injected."""
    glossary_block = ""
    if glossary:
        pairs = "\n".join(f'- "{r.source_term}" → "{r.target_term}"' for r in glossary)
        glossary_block = f"""

Apply these exact term translations wherever they appear (case-insensitive):
{pairs}
"""
    return (
        "You are an expert literary translator specializing in Arabic translation of English web novels. "
        "Translate the following English novel chapter into fluent, literary Arabic. "
        "Preserve all paragraph breaks exactly as they appear in the source. "
        "Do not translate proper names (characters, places) unless they are explicitly listed in the rules below. "
        "Output ONLY the translated Arabic text — no commentary, no preamble, no English text."
        + glossary_block
    )


def apply_glossary_postpass(text: str, glossary: List[GlossaryRule]) -> str:
    """
    Regex post-pass: force-correct any glossary term the model missed.
    Applies case-insensitive replacement of source_term → target_term.
    """
    for rule in glossary:
        pattern = re.compile(re.escape(rule.source_term), re.IGNORECASE)
        text = pattern.sub(rule.target_term, text)
    return text


class TranslationProvider(ABC):
    """
    Abstract base class for translation providers.
    Implementing a new provider = one new file implementing this interface.
    """

    provider_name: str = ""

    @abstractmethod
    async def translate_chapter(self, text: str, glossary: List[GlossaryRule]) -> str:
        """
        Translate a chapter of text, applying glossary rules.
        Returns the translated Arabic text.
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the provider has a valid API key configured."""
        ...
