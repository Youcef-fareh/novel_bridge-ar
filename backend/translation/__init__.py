"""
NovelBridge — Translation providers module.
"""
from typing import Dict, List, Optional, Tuple

from backend.translation.base import (
    ProviderFailureError,
    TranslationProvider,
    apply_glossary_postpass,
    build_system_prompt,
)
from backend.translation.gemini_provider import GEMINI_MODELS, GeminiProvider
from backend.translation.groq_provider import GROQ_MODELS, GroqProvider
from backend.translation.orcarouter_provider import (
    ORCAROUTER_MODELS,
    OrcaRouterProvider,
)
from backend.translation.tokenrouter_provider import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    TOKENROUTER_MODELS,
    TokenRouterProvider,
)

PROVIDER_MODELS: Dict[str, List[str]] = {
    "tokenrouter": TOKENROUTER_MODELS,
    "orcarouter": ORCAROUTER_MODELS,
    "gemini": GEMINI_MODELS,
    "groq": GROQ_MODELS,
}

PROVIDER_DISPLAY_NAMES: List[Tuple[str, str]] = [
    ("tokenrouter", "TokenRouter (DeepSeek / Qwen)"),
    ("orcarouter", "OrcaRouter (DeepSeek / Free)"),
    ("gemini", "Google Gemini"),
    ("groq", "Groq"),
    ("auto", "Auto (Best Available)"),
]


def get_provider(
    provider_name: Optional[str] = None,
    model: Optional[str] = None,
) -> TranslationProvider:
    """
    Instantiate requested translation provider or pick the best available.
    """
    name = (provider_name or "auto").lower().strip()

    if name == "tokenrouter":
        provider = TokenRouterProvider(model=model)
        if not provider.is_available():
            raise RuntimeError(
                "TokenRouter API key not configured. "
                "Please set TOKENROUTER_API_KEY in your .env or API Keys settings."
            )
        return provider

    elif name == "orcarouter":
        provider = OrcaRouterProvider(model=model)
        if not provider.is_available():
            raise RuntimeError(
                "OrcaRouter API key not configured. "
                "Please set ORCAROUTER_API_KEY in your .env or API Keys settings."
            )
        return provider

    elif name == "gemini":
        provider = GeminiProvider(model=model)
        if not provider.is_available():
            raise RuntimeError(
                "Gemini API key not configured. "
                "Please set GEMINI_API_KEY in your .env or API Keys settings."
            )
        return provider

    elif name == "groq":
        provider = GroqProvider(model=model)
        if not provider.is_available():
            raise RuntimeError(
                "Groq API key not configured. "
                "Please set GROQ_API_KEY in your .env or API Keys settings."
            )
        return provider

    # "auto" or unspecified: pick first available provider
    # Try TokenRouter -> OrcaRouter -> Gemini -> Groq
    tr = TokenRouterProvider(model=model)
    if tr.is_available():
        return tr

    orca = OrcaRouterProvider(model=model)
    if orca.is_available():
        return orca

    gemini = GeminiProvider(model=model)
    if gemini.is_available():
        return gemini

    groq = GroqProvider(model=model)
    if groq.is_available():
        return groq

    raise RuntimeError(
        "No translation API key configured.\n"
        "Please configure TOKENROUTER_API_KEY, ORCAROUTER_API_KEY, GEMINI_API_KEY, or GROQ_API_KEY in the 'API Keys' tab."
    )


__all__ = [
    "TranslationProvider",
    "ProviderFailureError",
    "TokenRouterProvider",
    "OrcaRouterProvider",
    "GeminiProvider",
    "GroqProvider",
    "build_system_prompt",
    "apply_glossary_postpass",
    "get_provider",
    "PROVIDER_MODELS",
    "PROVIDER_DISPLAY_NAMES",
    "TOKENROUTER_MODELS",
    "ORCAROUTER_MODELS",
    "GEMINI_MODELS",
    "GROQ_MODELS",
]
