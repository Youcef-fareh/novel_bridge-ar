"""
NovelBridge — Adapters package.
"""
from __future__ import annotations

from backend.adapters.base import AdapterRegistry
from backend.adapters.lncrawl_bridge import register_lncrawl_adapters


def register_standard_adapters() -> None:
    """Register all built-in site adapters with AdapterRegistry."""
    from backend.adapters.easternwordsmith import EasternWordsmithAdapter
    from backend.adapters.galaxynovels import GalaxyNovelsAdapter
    from backend.adapters.novelfire import NovelFireAdapter
    from backend.adapters.novelphoenix import NovelPhoenixAdapter
    from backend.adapters.ranovel import RanovelAdapter
    from backend.adapters.wtrlab import WTRLabAdapter
    from backend.adapters.wuxiaspot import WuxiaSpotAdapter

    for adapter_cls in (
        EasternWordsmithAdapter,
        NovelFireAdapter,
        NovelPhoenixAdapter,
        GalaxyNovelsAdapter,
        RanovelAdapter,
        WTRLabAdapter,
        WuxiaSpotAdapter,
    ):
        try:
            AdapterRegistry.register(adapter_cls())
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Failed to register built-in adapter %s", adapter_cls.__name__
            )

    register_lncrawl_adapters()


__all__ = ["register_lncrawl_adapters", "register_standard_adapters"]
