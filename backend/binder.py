"""Format dispatch and local artifact generation."""
from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Optional, Sequence

import httpx

from backend.epub_builder import build_epub
from backend.config import get_config
from backend.models import Chapter, Novel

_CALIBRE_FORMATS = {"pdf", "azw3", "mobi", "docx"}


def available_formats(command: Optional[str] = None) -> list[str]:
    formats = ["epub", "txt", "json"]
    config = get_config()
    calibre_command = command or config.calibre.command
    if config.calibre.api_enabled or shutil.which(calibre_command):
        formats.extend(sorted(_CALIBRE_FORMATS))
    return formats


def _safe_title(title: str, novel_id: Optional[int] = None) -> str:
    safe_title = "".join(
        char for char in title
        if char.isalnum() or char in " _.-"
    ).strip(" .")
    if not safe_title:
        return f"novel_{novel_id}" if novel_id is not None else "novel"
    if safe_title.upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }:
        safe_title = f"{safe_title}_{novel_id or 'book'}"
    return safe_title


def _select_chapters(
    chapters: Sequence[Chapter],
    volume: Optional[int],
    native_arabic: bool,
) -> list[Chapter]:
    selected = [
        chapter for chapter in chapters
        if volume is None or (chapter.volume_serial or chapter.index // 100 + 1) == volume
    ]
    selected = [
        chapter for chapter in selected
        if (chapter.translated_text and chapter.translated_text.strip())
        or (native_arabic and chapter.raw_text and chapter.raw_text.strip())
    ]
    if not selected:
        raise ValueError("No chapters are available for the requested artifact.")
    return sorted(selected, key=lambda chapter: chapter.index)


def _chapter_text(chapter: Chapter, native_arabic: bool) -> str:
    text = (chapter.translated_text or "").strip()
    if not text and native_arabic:
        text = (chapter.raw_text or "").strip()
    if not text:
        raise ValueError(f"Chapter {chapter.index + 1} has no translated content.")
    return text


def make_artifact(
    novel: Novel,
    chapters: Sequence[Chapter],
    fmt: str,
    output_dir: Path,
    *,
    volume: Optional[int] = None,
    language: str = "ar",
    native_arabic: bool = False,
    chapter_images: Optional[dict[int, list]] = None,
) -> Path:
    fmt = fmt.lower().lstrip(".")
    if language != "ar":
        raise ValueError(
            "Only Arabic artifacts are currently supported; other languages "
            "require localized EPUB markup and translation prompts."
        )
    config = get_config()
    if fmt not in available_formats(config.calibre.command):
        if fmt in _CALIBRE_FORMATS:
            raise ValueError(
                f"Format '{fmt}' requires Calibre's ebook-convert command. "
                "Install Calibre or choose epub, txt, or json."
            )
        raise ValueError(f"Unsupported artifact format: {fmt}")
    selected = _select_chapters(chapters, volume, native_arabic)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_title(novel.title, novel.id)
    volume_suffix = f".Vol_{volume:03d}" if volume is not None else ""
    lang_suffix = f".{language}" if language else ""

    if fmt == "epub":
        return build_epub(
            novel,
            selected,
            output_dir,
            volume=volume,
            language=language,
            chapter_images=chapter_images,
        )

    if fmt in {"txt", "json"}:
        output_path = output_dir / f"{stem}{volume_suffix}{lang_suffix}.{fmt}.zip"
        metadata = {
            "novel_id": novel.id,
            "title": novel.title,
            "author": novel.author,
            "source_url": novel.source_url,
            "language": language,
            "volume": volume,
            "chapter_count": len(selected),
        }
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if fmt == "txt":
                archive.writestr("meta.txt", "\n".join(
                    f"{key}: {value or ''}" for key, value in metadata.items()
                ))
                for chapter in selected:
                    text = _chapter_text(chapter, native_arabic)
                    serial = chapter.index + 1
                    volume_serial = chapter.volume_serial or chapter.index // 100 + 1
                    archive.writestr(
                        f"chapters/vol_{volume_serial:03d}/chapter_{serial:05d}.txt",
                        f"{chapter.title}\n\n{text}",
                    )
            else:
                archive.writestr(
                    "meta.json",
                    json.dumps(metadata, ensure_ascii=False, indent=2),
                )
                archive.writestr(
                    "chapters.json",
                    json.dumps(
                        [
                            {
                                "index": chapter.index,
                                "title": chapter.title,
                                "volume": chapter.volume_serial or chapter.index // 100 + 1,
                                "text": _chapter_text(chapter, native_arabic),
                            }
                            for chapter in selected
                        ],
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
        return output_path

    epub_path = build_epub(
        novel, selected, output_dir, volume=volume, language=language
    )
    output_path = output_dir / f"{stem}{volume_suffix}{lang_suffix}.{fmt}"
    api_failure: Optional[Exception] = None
    if config.calibre.api_enabled:
        try:
            with epub_path.open("rb") as epub_file:
                response = httpx.post(
                    config.calibre.api_url,
                    files={"file": (epub_path.name, epub_file, "application/epub+zip")},
                    data={"format": fmt},
                    timeout=900,
                )
            response.raise_for_status()
            if not response.content:
                raise RuntimeError("Calibre conversion API returned an empty artifact.")
            output_path.write_bytes(response.content)
            return output_path
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            api_failure = exc
            if not config.calibre.api_fallback_to_local:
                raise RuntimeError(f"Calibre API conversion failed: {exc}") from exc

    command = shutil.which(config.calibre.command)
    if not command:
        if api_failure:
            raise RuntimeError(
                f"Calibre API conversion failed ({api_failure}) and the configured "
                "local ebook-convert command is unavailable."
            ) from api_failure
        raise ValueError("Calibre's ebook-convert command is not available.")
    completed = subprocess.run(
        [command, str(epub_path), str(output_path)],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"Calibre conversion to {fmt} failed: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return output_path
