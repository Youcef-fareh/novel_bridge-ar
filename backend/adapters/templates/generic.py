"""
NovelBridge — Generic adapter template.

Fallback template for sites that expose standard OG-meta tags and a
simple chapter list / body.  Useful as a starting point for new adapters
where a more specific template (Wordpress/NovelFull) does not apply.
"""
from __future__ import annotations

from backend.adapters.soup_template import SoupAdapter


class GenericAdapter(SoupAdapter):
    """Template for simple sites with standard OG-meta and a flat chapter list.

    Minimal subclass example::

        class MyNewSiteAdapter(GenericAdapter):
            site_id = "mynewsite"
            base_url = "https://mynewsite.com"

            novel_title_selectors    = ["h1.novel-title"]
            novel_cover_selectors    = [".cover img"]
            novel_description_selectors = [".synopsis"]
            chapter_list_selectors   = [".chapter-list a"]
            chapter_body_selectors   = ["#chapter-content"]

            def can_handle(self, url: str) -> bool:
                return "mynewsite.com" in url
    """

    # OG-meta fallbacks are already in SoupAdapter; override only what differs.
    novel_title_selectors = [
        'meta[property="og:title"]',
        'meta[name="title"]',
        "h1",
        "title",
    ]
    novel_cover_selectors = [
        'meta[property="og:image"]',
        'meta[name="image"]',
    ]
    novel_author_selectors = [
        'meta[property="og:author"]',
        'meta[name="author"]',
    ]
    novel_description_selectors = [
        'meta[property="og:description"]',
        'meta[name="description"]',
    ]
    chapter_body_selectors = [
        "#chapter-container",
        ".chapter-content",
        "article",
        "main",
    ]
