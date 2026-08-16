"""
NovelBridge — EPUB builder.
Produces a valid RTL Arabic EPUB from translated chapters.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import List, Optional

from ebooklib import epub

from backend.models import Chapter, Novel

_OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))

# RTL CSS for Arabic EPUB
_RTL_CSS = """
@charset "UTF-8";

body {
    direction: rtl;
    text-align: right;
    font-family: "Amiri", "Traditional Arabic", "Arial Unicode MS", "Scheherazade New", serif;
    font-size: 1.1em;
    line-height: 1.8;
    margin: 1em 1.5em;
    color: #1a1a1a;
}

h1, h2, h3 {
    direction: rtl;
    text-align: right;
    font-family: "Amiri", "Traditional Arabic", serif;
    font-weight: bold;
    margin-top: 1.5em;
    margin-bottom: 0.5em;
    color: #111;
}

p {
    direction: rtl;
    text-align: justify;
    text-indent: 1.5em;
    margin: 0.4em 0;
}

.chapter-title {
    font-size: 1.4em;
    text-align: center;
    border-bottom: 2px solid #333;
    padding-bottom: 0.5em;
    margin-bottom: 1em;
}

.novel-title {
    font-size: 2em;
    text-align: center;
}

.author {
    font-size: 1.2em;
    text-align: center;
    color: #555;
}

.description {
    font-style: italic;
    margin: 1em 2em;
    text-align: right;
}
"""

_COVER_HTML_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ar" dir="rtl">
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <link rel="stylesheet" type="text/css" href="../styles/style.css"/>
</head>
<body>
  <h1 class="novel-title">{title}</h1>
  {author_block}
  {desc_block}
</body>
</html>
"""

_CHAPTER_HTML_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ar" dir="rtl">
<head>
  <meta charset="utf-8"/>
  <title>{chapter_title}</title>
  <link rel="stylesheet" type="text/css" href="../styles/style.css"/>
</head>
<body>
  <h2 class="chapter-title">{chapter_title}</h2>
  {body}
</body>
</html>
"""


def _text_to_html_paragraphs(text: str) -> str:
    """Convert plain text (with blank-line paragraph breaks) to <p> tags."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "\n".join(f"<p>{p.replace(chr(10), ' ')}</p>" for p in paragraphs)


def build_epub(novel: Novel, chapters: List[Chapter], output_dir: Optional[Path] = None) -> Path:
    """
    Build an EPUB from a novel and its translated chapters.
    Returns the path to the generated .epub file.
    Only chapters with status=translated and non-empty translated_text are included.
    """
    out_dir = output_dir or _OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    book = epub.EpubBook()

    # ── Metadata ──────────────────────────────────────────────────────────────
    book.set_identifier(f"novelbridge-{novel.id}-{novel.source_site}")
    book.set_title(novel.title)
    book.set_language("ar")
    if novel.author:
        book.add_author(novel.author)

    # Add RTL/direction metadata
    book.add_metadata("OPF", "meta", None, {"name": "primary-writing-mode", "content": "horizontal-rl"})

    # ── CSS ───────────────────────────────────────────────────────────────────
    css_item = epub.EpubItem(
        uid="style_main",
        file_name="styles/style.css",
        media_type="text/css",
        content=_RTL_CSS.encode("utf-8"),
    )
    book.add_item(css_item)

    # ── Cover image (if available) ────────────────────────────────────────────
    if novel.cover_url:
        try:
            import httpx
            resp = httpx.get(novel.cover_url, timeout=10, follow_redirects=True)
            if resp.status_code == 200:
                cover_data = resp.content
                cover_item = epub.EpubItem(
                    uid="cover_img",
                    file_name="images/cover.jpg",
                    media_type="image/jpeg",
                    content=cover_data,
                )
                book.add_item(cover_item)
                book.set_cover("images/cover.jpg", cover_data)
        except Exception:
            pass  # Cover is optional

    # ── Title/intro page ─────────────────────────────────────────────────────
    author_block = f'<p class="author">المؤلف: {novel.author}</p>' if novel.author else ""
    desc_block = f'<div class="description">{novel.description}</div>' if novel.description else ""
    cover_content = _COVER_HTML_TEMPLATE.format(
        title=novel.title,
        author_block=author_block,
        desc_block=desc_block,
    )
    cover_page = epub.EpubHtml(
        title=novel.title,
        file_name="text/cover.xhtml",
        lang="ar",
        content=cover_content.encode("utf-8"),
    )
    cover_page.add_item(css_item)
    book.add_item(cover_page)

    # ── Chapters ─────────────────────────────────────────────────────────────
    epub_chapters: list[epub.EpubHtml] = [cover_page]
    
    # Check if this novel is from a native Arabic source
    is_native_ar = False
    if novel.source_site:
        from backend.adapters.base import AdapterRegistry
        adapter = AdapterRegistry._adapters.get(novel.source_site)
        if adapter and getattr(adapter, "is_native_arabic", False):
            is_native_ar = True

    # Gather chapters to include: translated_text if available, else raw_text for native Arabic novels
    valid_chapters = []
    for c in chapters:
        text = c.translated_text if (c.translated_text and c.translated_text.strip()) else (c.raw_text if is_native_ar else None)
        if text and text.strip():
            valid_chapters.append((c, text))

    for chapter, text in sorted(valid_chapters, key=lambda x: x[0].index):
        body_html = _text_to_html_paragraphs(text)
        chapter_content = _CHAPTER_HTML_TEMPLATE.format(
            chapter_title=chapter.title,
            body=body_html,
        )
        epub_chapter = epub.EpubHtml(
            title=chapter.title,
            file_name=f"text/chapter_{chapter.index:04d}.xhtml",
            lang="ar",
            content=chapter_content.encode("utf-8"),
        )
        epub_chapter.add_item(css_item)
        book.add_item(epub_chapter)
        epub_chapters.append(epub_chapter)

    # ── Table of Contents + Spine ─────────────────────────────────────────────
    book.toc = [
        epub.Link(ch.file_name, ch.title, f"chapter-{i}")
        for i, ch in enumerate(epub_chapters)
    ]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + epub_chapters

    # ── Write file ────────────────────────────────────────────────────────────
    safe_title = "".join(c for c in novel.title if c.isalnum() or c in " _-").rstrip()
    output_path = out_dir / f"{safe_title or f'novel_{novel.id}'}.epub"
    epub.write_epub(str(output_path), book)
    return output_path
