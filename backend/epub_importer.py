"""Import EPUB novels into the library as chapters ready for translation."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT, epub

from backend.database import get_novel, get_session
from backend.models import Chapter, ChapterStatus, Novel, NovelStatus


@dataclass(frozen=True)
class ImportedChapter:
    title: str
    text: str


@dataclass(frozen=True)
class ImportedEpub:
    title: str
    author: str | None
    description: str | None
    chapters: list[ImportedChapter]


_FRONT_MATTER_NAME = re.compile(
    r"(^|[-_\W])(cover|title|copyright|contents?|toc|nav|title[-_ ]?page|"
    r"front[-_ ]?matter|dedication|imprint|colophon|acknowledg(e)?ments?)"
    r"([-_\W]|$)",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")


def _metadata_value(book: epub.EpubBook, name: str) -> str | None:
    values = book.get_metadata("DC", name)
    if not values:
        return None
    value = str(values[0][0]).strip()
    return value or None


def _chapter_from_item(item, chapter_number: int) -> ImportedChapter | None:
    soup = BeautifulSoup(item.get_content(), "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript", "svg"]):
        tag.decompose()

    heading = soup.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    title = _WHITESPACE.sub(" ", heading.get_text(" ", strip=True)).strip() if heading else ""

    paragraphs = [
        _WHITESPACE.sub(" ", paragraph.get_text(" ", strip=True)).strip()
        for paragraph in soup.select("p")
    ]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    if paragraphs:
        text = "\n\n".join(paragraphs)
    else:
        content = soup.body or soup
        text = "\n\n".join(
            _WHITESPACE.sub(" ", line).strip()
            for line in content.get_text("\n", strip=True).splitlines()
            if line.strip()
        )

    if not text:
        return None

    return ImportedChapter(title=title or f"Chapter {chapter_number}", text=text)


def read_epub(path: str | Path) -> ImportedEpub:
    """Read chapter text and metadata from an EPUB, in its reading order."""
    epub_path = Path(path).expanduser()
    if epub_path.suffix.casefold() != ".epub":
        raise ValueError("Choose a file with the .epub extension.")
    if not epub_path.is_file():
        raise ValueError(f"EPUB file not found: {epub_path}")

    book = epub.read_epub(str(epub_path))
    chapters: list[ImportedChapter] = []
    for item_id, linear in book.spine:
        if str(linear).casefold() == "no":
            continue
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ITEM_DOCUMENT:
            continue
        if _FRONT_MATTER_NAME.search(Path(item.get_name()).stem):
            continue

        chapter = _chapter_from_item(item, len(chapters) + 1)
        if chapter is not None:
            chapters.append(chapter)

    if not chapters:
        raise ValueError("No readable chapter text was found in this EPUB.")

    title = _metadata_value(book, "title") or epub_path.stem.replace("-", " ").strip()
    return ImportedEpub(
        title=title,
        author=_metadata_value(book, "creator"),
        description=_metadata_value(book, "description"),
        chapters=chapters,
    )


def import_epub_file(path: str | Path) -> Novel:
    """Create a library novel with imported chapters ready for translation."""
    epub_path = Path(path).expanduser().resolve()
    imported = read_epub(epub_path)
    source_url = epub_path.as_uri()

    with get_session() as session:
        novel = Novel(
            title=imported.title,
            author=imported.author,
            description=imported.description,
            source_url=source_url,
            source_site="epub_import",
            status=NovelStatus.scraped,
        )
        session.add(novel)
        session.flush()

        for index, chapter in enumerate(imported.chapters):
            session.add(
                Chapter(
                    novel_id=novel.id,
                    index=index,
                    title=chapter.title,
                    source_url=f"{source_url}#chapter-{index + 1}",
                    raw_text=chapter.text,
                    status=ChapterStatus.scraped,
                )
            )
        session.commit()
        novel_id = novel.id

    if novel_id is None:
        raise RuntimeError("The imported novel was not assigned a database ID.")
    saved_novel = get_novel(novel_id)
    if saved_novel is None:
        raise RuntimeError("The imported novel could not be read back from the database.")
    return saved_novel
