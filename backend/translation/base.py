"""
NovelBridge — Translation provider base class.
"""
from __future__ import annotations
from typing import Optional
import re
from abc import ABC, abstractmethod
from typing import List

from backend.models import GlossaryRule


class TruncatedOutputError(RuntimeError):
    """The provider stopped generating because it hit its output-token limit.

    Raised instead of silently returning a cut-off translation, which would
    publish incomplete chapters as if they were complete.
    """


def is_streaming_unsupported_error(error: Exception) -> bool:
    """Return True only when a streaming request failed for a streaming-specific reason.

    Falling back to a non-streaming request for auth/quota/network errors would
    issue a duplicate paid request and hide the real failure.
    """
    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    if status in (400, 404, 405, 415, 422):
        return True

    message = str(error).lower()
    markers = (
        "does not support stream",
        "streaming is not supported",
        "stream is not supported",
        "unknown parameter: stream",
        "stream_options",
        "unsupported parameter",
        "unrecognized request argument",
    )
    return any(marker in message for marker in markers)


def is_retryable_error(error: Exception) -> bool:
    """Return True only for transient errors worth retrying.

    Retrying auth failures (401), bad requests (400/404/422) or invalid keys
    just burns paid calls on requests that can never succeed and delays the
    user-visible failure. Only network-level errors, timeouts, 408, 429 and
    5xx responses are worth a retry.
    """
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    if status is not None:
        return status in (408, 429) or status >= 500
    # No HTTP status (connection reset, DNS failure, SDK-internal timeouts…):
    # treat as transient.
    message = str(error).lower()
    transient_markers = (
        "timed out",
        "timeout",
        "connection",
        "temporarily unavailable",
        "rate limit",
        "overloaded",
        "server error",
    )
    return any(marker in message for marker in transient_markers)


def build_system_prompt(glossary: List[GlossaryRule], context: Optional[str] = None) -> str:
    """Build the system prompt with glossary rules injected."""
    glossary_block = ""
    if glossary:
        pairs = "\n".join(f'- "{r.source_term}" → "{r.target_term}"' for r in glossary)
        glossary_block = f"""

GLOSSARY (highest priority — apply these exact translations wherever the term
appears, case-insensitive, in any grammatical form or inflection):
{pairs}

If a name below is a variant/nickname/title referring to a glossaried
character (e.g. a shortened form, honorific + name, or romanization
variant), still map it to the glossaried Arabic form rather than
transliterating it separately.
"""

    continuity_block = ""
    if context:
        continuity_block = (
            "\n\nCONTINUITY CONTEXT (reference only; do not translate or repeat it):\n"
            f"{context[-500:]}\n"
            "Use this prior translated passage to keep names, voices, and references consistent."
        )

    return (
        "You are an expert literary translator specializing in Arabic translation of "
        "English-language web novels — including novels translated from Chinese, "
        "Korean, or Japanese into English (often with WuXia/XianXia/CN-web-novel "
        "conventions) — as well as originally English-language fiction.\n\n"

        "TASK\n"
        "Translate the following chapter into fluent, literary, publication-quality "
        "Arabic. Preserve all paragraph breaks exactly as they appear in the source. "
        "Translate every sentence and every paragraph in order; do not summarize, "
        "shorten, skip, continue, or invent any part of the source. The input may be "
        "one part of a longer chapter, so translate only the text provided. Never ask "
        "the user to provide the chapter text. "
        "Output ONLY the translated Arabic text — no commentary, no preamble, no "
        "notes, no English text, no romanization left in place.\n\n"

        "HANDLING NAMES (READ CAREFULLY)\n"
        "The source text may contain personal names, place names, sect/clan/organization "
        "names, and titles of several different origins — Chinese (pinyin, e.g. 'Li Wei', "
        "'Xiao Yan'), Korean, Japanese, Western, or invented/fantasy names. Follow these "
        "rules exactly:\n\n"

        "1. TRANSLITERATE, DON'T TRANSLATE, PERSONAL NAMES. Render personal names and "
        "place names phonetically in Arabic script based on how they are pronounced in "
        "the source language — NOT by translating the literal meaning of their "
        "characters/syllables, unless the glossary explicitly instructs otherwise. "
        "Example: a Chinese name like '陈平安' romanized as 'Chen Ping'an' should become "
        "a phonetic Arabic rendering (شين پينغ آن / تشن بينغ آن-style), never a literal "
        "translation of 'Chen' (the surname) or 'Ping'an' (peaceful) as common Arabic words.\n\n"

        "2. USE STANDARD SINO-ARABIC / CJK-ARABIC TRANSLITERATION CONVENTIONS. For "
        "Chinese pinyin, map sounds consistently (e.g. 'zh' → ج/تش, 'x' → سh/شـ, 'q' → تش, "
        "'ü' → يو) rather than inventing ad-hoc spellings. For Korean and Japanese "
        "romanized names, use the closest standard Arabic phonetic equivalent. The goal "
        "is a rendering a native Arabic reader can pronounce consistently, not a literal "
        "letter-by-letter transcription of the Latin romanization.\n\n"

        "3. BE INTERNALLY CONSISTENT. Once you choose an Arabic rendering for a name "
        "within this chapter, use that exact same spelling every time the name "
        "reappears in this chapter — including in different grammatical positions, "
        "with attached Arabic prefixes/suffixes (و/ف/بـ/لـ/ال), or when referred to by "
        "surname only, given name only, or title + name. Do not silently switch between "
        "multiple valid transliterations of the same name.\n\n"

        "4. DISTINGUISH NAMES FROM MEANINGFUL EPITHETS/TITLES. If a 'name' is actually a "
        "descriptive title, cultivation-technique name, sect name, or nickname built from "
        "ordinary words (e.g. 'Sword Saint', 'Azure Cloud Sect', 'the Nameless One'), "
        "translate its MEANING into Arabic rather than transliterating it, since these "
        "are meant to be understood by the reader — unless the glossary gives an exact "
        "term to use.\n\n"

        "5. THE GLOSSARY ALWAYS WINS. If a name, place, or term (in any form) is listed "
        "in the glossary below, use the glossary's Arabic form exactly, even if it "
        "conflicts with rules 1–4.\n\n"

        "6. WHEN UNCERTAIN, PICK ONE FORM AND STAY CONSISTENT. If a name's origin or "
        "correct transliteration is ambiguous, choose the most natural, pronounceable "
        "Arabic phonetic rendering and use it consistently for the rest of the chapter "
        "rather than alternating between guesses."
        + glossary_block
        + continuity_block
    )


def apply_glossary_postpass(text: str, glossary: List[GlossaryRule]) -> str:
    """
    Regex post-pass: force-correct any glossary term the model missed.
    Applies case-insensitive, word-boundary-aware replacement of
    source_term → target_term.

    Uses \b boundaries where the term is alphanumeric so we don't
    clobber substrings inside unrelated words; falls back to a plain
    (non-bounded) match for terms containing non-word characters
    (e.g. already-Arabic terms, terms with punctuation/spacing).

    The replacement is always treated as literal text: target terms are
    substituted via a lambda so regex template escapes (\\1, \\n, \\g<0>…)
    can never crash the pass or mangle the output. Rules with empty or
    whitespace-only source terms are skipped — a previously empty
    re.escape("") pattern would match at every position and corrupt the
    entire chapter.
    """
    for rule in glossary:
        source = rule.source_term.strip()
        if not source or source == rule.target_term:
            continue
        escaped = re.escape(source)
        if re.match(r"^\w+$", source, re.UNICODE):
            pattern = re.compile(rf"\b{escaped}\b", re.IGNORECASE | re.UNICODE)
        else:
            pattern = re.compile(escaped, re.IGNORECASE | re.UNICODE)
        # Literal replacement: never interpret the target as a regex template.
        text = pattern.sub(lambda _m, _t=rule.target_term: _t, text)
    return text


class ProviderFailureError(Exception):
    """
    Raised when a translation provider fails critically or exhausts retries.
    Contains structured information and actionable suggestions for the user.
    """
    def __init__(self, provider: str, error_detail: str, suggestion: str = ""):
        self.provider = provider
        self.error_detail = error_detail
        self.suggestion = (
            suggestion
            or f"Translation failed with provider '{provider}' ({error_detail}). "
               f"Please consider switching to another provider (e.g., TokenRouter, Gemini, or Groq) "
               f"or check your API key in the 'API Keys' tab."
        )
        super().__init__(self.suggestion)


class TranslationProvider(ABC):
    """
    Abstract base class for translation providers.
    Implementing a new provider = one new file implementing this interface.
    """

    provider_name: str = ""

    @abstractmethod
    async def translate_chapter(
        self,
        text: str,
        glossary: List[GlossaryRule],
        model: Optional[str] = None,
        context: Optional[str] = None,
    ) -> str:
        """
        Translate a chapter of text, applying glossary rules.
        Returns the translated Arabic text.
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the provider has a valid API key configured."""
        ...
