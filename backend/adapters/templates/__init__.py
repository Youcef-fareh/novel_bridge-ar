"""
NovelBridge — shared adapter template library.
"""
from .generic import GenericAdapter
from .novelfull import NovelFullAdapter
from .wordpress import WordpressAdapter

__all__ = ["GenericAdapter", "NovelFullAdapter", "WordpressAdapter"]
