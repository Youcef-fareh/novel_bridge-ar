"""Compatibility adapters for crawler classes in an external lncrawl checkout."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import inspect
import logging
import os
import re
import sys
import threading
from collections.abc import Iterable, Mapping
from functools import cached_property
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from backend.adapters.base import (
    AdapterRegistry,
    ChapterRef,
    NovelMeta,
    SearchResult,
    SiteAdapter,
)

logger = logging.getLogger("novelbridge.adapters.lncrawl")

_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "ol",
        "p",
        "section",
        "table",
        "tr",
        "ul",
    }
)


def _get_value(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping):
            found = value.get(name)
        else:
            found = getattr(value, name, None)
        if found is not None:
            return found
    return default


def _as_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        value = ", ".join(str(part).strip() for part in value if str(part).strip())
    text = str(value).strip()
    return text or None


def _html_to_paragraph_text(html: str) -> str:
    """Extract chapter text while retaining HTML block and line-break boundaries."""
    soup = BeautifulSoup(html, "html.parser")

    def extract(node: Any) -> str:
        if isinstance(node, Comment):
            return ""
        if isinstance(node, NavigableString):
            return str(node)
        if not isinstance(node, Tag) or node.name in {"script", "style"}:
            return ""
        if node.name in {"br", "hr"}:
            return "\n\n"

        content = "".join(extract(child) for child in node.children)
        if node.name in _BLOCK_TAGS:
            return f"\n\n{content}\n\n"
        return content

    paragraphs = [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in extract(soup).split("\n\n")
    ]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


def _base_urls(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Iterable):
        values = list(value)
    else:
        values = []
    return [str(item).strip() for item in values if str(item).strip()]


class LNCrawlBridgeAdapter(SiteAdapter):
    """Run a synchronous lncrawl crawler behind NovelBridge's async interface.

    A fresh crawler is created for each blocking operation, preventing mutable
    crawler state from being shared between concurrently scraped novels.
    """

    source_language = "Multiple"
    scraping_method = "lncrawl compatibility bridge"

    def __init__(
        self,
        crawler_cls: type,
        site_id: str,
        novel_cls: type,
        chapter_cls: type,
        source_language: str = "Unknown",
    ) -> None:
        self.crawler_cls = crawler_cls
        self.site_id = site_id
        configured_language = _as_text(source_language)
        declared_language = _as_text(getattr(crawler_cls, "language", None))
        self.source_language = (
            "Arabic"
            if declared_language and declared_language.casefold() in {"ar", "arabic"}
            else configured_language
            if configured_language and configured_language.casefold() != "unknown"
            else declared_language or "Unknown"
        )
        self.is_native_arabic = self.source_language.casefold() in {"ar", "arabic"}
        self._novel_cls = novel_cls
        self._chapter_cls = chapter_cls
        self._base_urls = _base_urls(getattr(crawler_cls, "base_url", None))
        self._metadata_chapters: dict[str, list[ChapterRef]] = {}
        self._chapter_novel_urls: dict[str, str] = {}
        self._source_chapters: dict[str, Any] = {}
        self._cache_lock = threading.Lock()

    def _search(self, query: str, limit: int) -> list[SearchResult]:
        crawler = self.crawler_cls()
        try:
            self._initialize_crawler(crawler)
            results = crawler.search(query)
            allowed_hosts = {
                (urlparse(base if "://" in base else f"https://{base}").hostname or "").lower()
                for base in self._base_urls
            }
            found: list[SearchResult] = []
            seen: set[str] = set()
            for result in results or []:
                url = urljoin(
                    self._base_urls[0] if self._base_urls else "",
                    _as_text(_get_value(result, "url", "source_url")),
                )
                parsed = urlparse(url)
                host = (parsed.hostname or "").lower()
                if (
                    parsed.scheme not in {"http", "https"}
                    or not any(host == base or host.endswith(f".{base}") for base in allowed_hosts)
                    or url in seen
                ):
                    continue
                title = _as_text(_get_value(result, "title", "name"))
                if not title:
                    continue
                seen.add(url)
                cover_url = _as_text(_get_value(result, "cover_url", "cover"))
                found.append(SearchResult(
                    title=title,
                    url=url,
                    source_site=self.site_id,
                    cover_url=urljoin(url, cover_url) if cover_url else None,
                ))
                if len(found) >= limit:
                    break
            return found
        finally:
            self._close_crawler(crawler)

    async def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        if not self.can_search:
            return []
        return await asyncio.to_thread(self._search, query, max(1, min(limit, 50)))

    def can_handle(self, url: str) -> bool:
        requested = urlparse(url)
        if requested.scheme not in ("http", "https") or not requested.hostname:
            return False

        host = requested.hostname.lower().rstrip(".")
        for base in self._base_urls:
            parsed_base = urlparse(base if "://" in base else f"https://{base}")
            base_host = (parsed_base.hostname or "").lower().rstrip(".")
            if not base_host or not (host == base_host or host.endswith(f".{base_host}")):
                continue
            base_path = parsed_base.path.rstrip("/")
            if not base_path or requested.path.rstrip("/") == base_path or requested.path.startswith(f"{base_path}/"):
                return True
        return False

    def _read_novel(self, novel_url: str) -> tuple[Any, list[ChapterRef]]:
        crawler = self.crawler_cls()
        try:
            crawler.novel_url = novel_url
            self._initialize_crawler(crawler)
            read_novel = crawler.read_novel
            parameters = inspect.signature(read_novel).parameters.values()
            accepts_novel = any(
                parameter.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
                for parameter in parameters
            )

            if accepts_novel:
                novel = self._novel_cls(url=novel_url)
                read_novel(novel)
                result = novel
            else:
                crawler.novel_url = novel_url
                read_novel()
                result = getattr(crawler, "novel", None)
                if result is None:
                    result = SimpleNamespace(
                        title=getattr(crawler, "novel_title", ""),
                        author=getattr(crawler, "novel_author", ""),
                        authors=getattr(crawler, "authors", None),
                        cover_url=getattr(crawler, "novel_cover", None),
                        synopsis=getattr(crawler, "novel_synopsis", ""),
                        chapters=getattr(crawler, "chapters", []),
                    )

            chapters = _get_value(result, "chapters", default=None)
            if chapters is None:
                chapters = getattr(crawler, "chapters", [])

            refs: list[ChapterRef] = []
            source_chapters: dict[str, Any] = {}
            seen: set[str] = set()
            for chapter in chapters or []:
                raw_url = _get_value(chapter, "url", "source_url", "href", default="")
                raw_url = str(raw_url).strip()
                if not raw_url:
                    continue
                chapter_url = urljoin(novel_url, raw_url)
                if (
                    self.site_id == "lncrawl_ar_kolnovel"
                    and urlparse(chapter_url).path.rstrip("/").lower().endswith("/pdf")
                ):
                    continue
                if chapter_url in seen:
                    continue
                seen.add(chapter_url)
                source_chapters[chapter_url] = chapter
                title = _as_text(_get_value(chapter, "title", "name"))
                volume_value = _get_value(
                    chapter, "volume_serial", "volume_number", "volume"
                )
                volume_serial = None
                if volume_value is not None:
                    normalized_volume = _get_value(
                        volume_value, "serial", "number", "id", default=volume_value
                    )
                    match = re.search(r"\d+", str(normalized_volume))
                    if match:
                        volume_serial = int(match.group())
                volume_title = _as_text(
                    _get_value(chapter, "volume_title", "volume_name")
                )
                refs.append(ChapterRef(
                    index=len(refs),
                    title=title or f"Chapter {len(refs) + 1}",
                    source_url=chapter_url,
                    volume_serial=volume_serial,
                    volume_title=volume_title,
                ))
            with self._cache_lock:
                for ref in refs:
                    self._chapter_novel_urls[ref.source_url] = novel_url
                self._source_chapters.update(source_chapters)
                while len(self._chapter_novel_urls) > 50_000:
                    expired_url = next(iter(self._chapter_novel_urls))
                    self._chapter_novel_urls.pop(expired_url)
                    self._source_chapters.pop(expired_url, None)
            return result, refs
        finally:
            self._close_crawler(crawler)

    @staticmethod
    def _initialize_crawler(crawler: Any) -> None:
        initialize = getattr(crawler, "initialize", None)
        if callable(initialize):
            initialize()

    @staticmethod
    def _close_crawler(crawler: Any) -> None:
        close = getattr(crawler, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:
            logger.warning("Failed to close lncrawl crawler %s", type(crawler).__name__, exc_info=True)

    def _metadata_and_chapters(self, novel_url: str) -> tuple[NovelMeta, list[ChapterRef]]:
        novel, chapters = self._read_novel(novel_url)
        title = _as_text(_get_value(novel, "title")) or _as_text(
            getattr(novel, "novel_title", None)
        )
        if not title:
            raise ValueError(f"lncrawl crawler {self.crawler_cls.__name__} returned no novel title")

        author = _as_text(_get_value(novel, "author", "authors")) or _as_text(
            getattr(novel, "novel_author", None)
        )
        cover_url = _as_text(_get_value(novel, "cover_url", "novel_cover"))
        synopsis = _as_text(_get_value(novel, "synopsis", "description", "novel_synopsis"))
        metadata = NovelMeta(
            title=title,
            author=author,
            cover_url=urljoin(novel_url, cover_url) if cover_url else None,
            description=synopsis,
            source_url=novel_url,
            source_site=self.site_id,
        )
        return metadata, chapters

    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        metadata, chapters = await asyncio.to_thread(self._metadata_and_chapters, novel_url)
        with self._cache_lock:
            self._metadata_chapters[novel_url] = chapters
            while len(self._metadata_chapters) > 64:
                self._metadata_chapters.pop(next(iter(self._metadata_chapters)))
        return metadata

    async def get_chapter_list(self, novel_url: str) -> list[ChapterRef]:
        with self._cache_lock:
            cached = self._metadata_chapters.pop(novel_url, None)
        if cached is not None:
            return cached
        _, chapters = await asyncio.to_thread(self._read_novel, novel_url)
        return chapters

    def _download_chapter(self, chapter_url: str, novel_url: Optional[str]) -> str:
        crawler = self.crawler_cls()
        try:
            if novel_url:
                crawler.novel_url = novel_url
            self._initialize_crawler(crawler)
            with self._cache_lock:
                chapter = self._source_chapters.get(chapter_url)
            if chapter is None:
                chapter = self._chapter_cls(id=1, url=chapter_url, title="")
            elif _get_value(chapter, "url") != chapter_url:
                if hasattr(chapter, "url"):
                    setattr(chapter, "url", chapter_url)
                elif isinstance(chapter, Mapping):
                    chapter = dict(chapter)
                    chapter["url"] = chapter_url
            download = getattr(crawler, "download_chapter", None)
            if callable(download):
                result = download(chapter)
                body = _get_value(chapter, "body", default=result)
            else:
                download_body = getattr(crawler, "download_chapter_body", None)
                if not callable(download_body):
                    raise TypeError(
                        f"{self.crawler_cls.__name__} has no chapter download method"
                    )
                body = download_body(chapter)

            if body is None:
                return ""
            return _html_to_paragraph_text(str(body))
        finally:
            self._close_crawler(crawler)

    async def get_chapter_text(self, chapter_url: str) -> str:
        with self._cache_lock:
            novel_url = self._chapter_novel_urls.get(chapter_url)
        return await asyncio.to_thread(self._download_chapter, chapter_url, novel_url)


def _load_source_module(root: Path, path: Path) -> ModuleType:
    relative_path = path.relative_to(root)
    module_hash = hashlib.sha256(str(relative_path).encode("utf-8")).hexdigest()[:16]
    module_name = f"novelbridge_lncrawl_source_{module_hash}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create an import spec for lncrawl source {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _default_lncrawl_root() -> Optional[Path]:
    project_root = Path(__file__).resolve().parents[2]
    executable_dir = Path(sys.executable).resolve().parent
    frozen_data_dir = Path(
        getattr(sys, "_MEIPASS", executable_dir)
    ).resolve()
    candidates = (
        project_root / "lightnovel-crawler-dev",
        project_root.parent / "lightnovel-crawler-dev",
        frozen_data_dir / "lightnovel-crawler-dev",
        executable_dir / "lightnovel-crawler-dev",
        executable_dir / "_internal" / "lightnovel-crawler-dev",
    )
    return next(
        (
            candidate
            for candidate in candidates
            if (candidate / "lncrawl").is_dir()
        ),
        None,
    )


def _configure_lncrawl_sources(checkout: Path, sources_dir: Path) -> None:
    """Point upstream crawler configuration at the source tree used by the bridge."""
    standard_source_dirs = {
        (checkout / "lncrawl" / "sources").resolve(),
        (checkout / "sources").resolve(),
    }
    if sources_dir.resolve() in standard_source_dirs:
        return

    config_module = importlib.import_module("lncrawl.config")
    crawler_config = getattr(config_module, "CrawlerConfig", None)
    descriptor = (
        vars(crawler_config).get("local_sources")
        if inspect.isclass(crawler_config)
        else None
    )
    if not isinstance(descriptor, cached_property):
        raise RuntimeError(
            "The installed lncrawl config does not expose the expected "
            "CrawlerConfig.local_sources setting"
        )

    root_dir = Path(
        getattr(config_module, "ROOT_DIR", checkout / "lncrawl")
    ).resolve()
    upstream_source_dirs = {
        (root_dir / "sources").resolve(),
        (root_dir.parent / "sources").resolve(),
    }
    if sources_dir.resolve() not in upstream_source_dirs:
        replacement = cached_property(lambda _config: sources_dir)
        replacement.__set_name__(crawler_config, "local_sources")
        crawler_config.local_sources = replacement


def _configure_pyease_grpc_protobuf_compat() -> None:
    """Keep pyease-grpc working when protobuf has removed float_precision."""
    if importlib.util.find_spec("pyease_grpc") is None:
        return
    protocol = importlib.import_module("pyease_grpc._protocol")
    message_to_dict = protocol.MessageToDict
    if "float_precision" in inspect.signature(message_to_dict).parameters:
        return
    if getattr(message_to_dict, "_novelbridge_float_precision_compat", False):
        return

    def compatible_message_to_dict(
        message: Any, *args: Any, float_precision: Optional[int] = None, **kwargs: Any
    ) -> dict:
        if float_precision is not None:
            raise TypeError(
                "The installed protobuf version does not support float_precision"
            )
        return message_to_dict(message, *args, **kwargs)

    compatible_message_to_dict._novelbridge_float_precision_compat = True
    protocol.MessageToDict = compatible_message_to_dict


def register_lncrawl_adapters(
    root: str | Path | None = None,
    sources_root: str | Path | None = None,
) -> int:
    """Discover and register source crawlers from an external lncrawl checkout.

    Set ``LNCRAWL_ROOT`` to the checkout containing ``lncrawl``. Sources can
    stay in that checkout or live in a separate directory configured through
    ``LNCRAWL_SOURCES_ROOT``. Missing configuration is optional; explicitly
    configured invalid paths raise an actionable error.
    """
    environment_root = os.getenv("LNCRAWL_ROOT")
    environment_sources_root = os.getenv("LNCRAWL_SOURCES_ROOT")
    explicitly_configured = any(
        value is not None
        for value in (root, environment_root, sources_root, environment_sources_root)
    )
    configured_root = root or environment_root
    if configured_root is None:
        local_checkout = _default_lncrawl_root()
        if local_checkout is not None:
            configured_root = local_checkout
        else:
            logger.info("lncrawl bridge is disabled; no local checkout was found")
            return 0

    checkout = Path(configured_root).expanduser().resolve()
    package_dir = checkout / "lncrawl"
    if not checkout.is_dir() or not package_dir.is_dir():
        raise ValueError(
            f"Invalid LNCRAWL_ROOT {checkout}: expected an 'lncrawl' directory"
        )
    configured_sources = sources_root or environment_sources_root
    if configured_sources is not None:
        sources_dir = Path(configured_sources).expanduser().resolve()
    else:
        sources_candidates = (
            checkout / "sources",
            checkout.parent / "other sources" / "lightnovel-crawler-sources",
            checkout.parent / "lightnovel-crawler-sources",
            checkout.parent.parent / "other sources" / "lightnovel-crawler-sources",
            checkout.parent.parent / "lightnovel-crawler-sources",
        )
        sources_dir = next(
            (candidate for candidate in sources_candidates if candidate.is_dir()),
            checkout / "sources",
        )
    if not sources_dir.is_dir():
        if explicitly_configured:
            raise ValueError(
                f"Invalid LNCRAWL_SOURCES_ROOT {sources_dir}: expected a 'sources' "
                "directory containing 'en' and/or 'ar'"
            )
        logger.info("lncrawl bridge is disabled; no source directory was found")
        return 0

    loaded_package = sys.modules.get("lncrawl")
    loaded_path = getattr(loaded_package, "__file__", None)
    if loaded_path and checkout not in Path(loaded_path).resolve().parents:
        if not explicitly_configured:
            logger.warning(
                "Skipping optional lncrawl checkout at %s; lncrawl is already loaded from %s",
                checkout,
                loaded_path,
            )
            return 0
        raise RuntimeError(
            f"lncrawl is already imported from {loaded_path}; cannot load sources from {checkout}"
        )
    checkout_str = str(checkout)
    previously_loaded = set(sys.modules)
    was_in_path = checkout_str in sys.path
    if checkout_str in sys.path:
        sys.path.remove(checkout_str)
    sys.path.insert(0, checkout_str)

    try:
        crawler_base = importlib.import_module("lncrawl.core.crawler").Crawler
        models = importlib.import_module("lncrawl.core.models")
        novel_cls = models.Novel
        chapter_cls = models.Chapter
    except Exception as exc:
        if not explicitly_configured:
            for name in set(sys.modules) - previously_loaded:
                module_path = getattr(sys.modules.get(name), "__file__", None)
                if module_path:
                    try:
                        Path(module_path).resolve().relative_to(checkout)
                    except ValueError:
                        continue
                    sys.modules.pop(name, None)
            if not was_in_path and checkout_str in sys.path:
                sys.path.remove(checkout_str)
            logger.warning(
                "Ignoring incompatible optional lncrawl checkout at %s: %s",
                checkout,
                exc,
            )
            return 0
        raise RuntimeError(f"Could not load the lncrawl API from {checkout}") from exc

    _configure_lncrawl_sources(checkout, sources_dir)

    source_roots = [sources_dir / language for language in ("en", "ar")]
    source_files = sorted(
        path
        for source_root in source_roots
        if source_root.is_dir()
        for path in source_root.rglob("*.py")
        if path.name != "__init__.py" and "__pycache__" not in path.parts
    )
    if not source_files:
        raise RuntimeError(
            f"No English or Arabic source modules were found under {sources_dir}"
        )
    _configure_pyease_grpc_protobuf_compat()

    registered = 0
    import_failures = 0
    for path in source_files:
        try:
            module = _load_source_module(sources_dir, path)
        except Exception:
            import_failures += 1
            logger.warning("Could not import lncrawl source %s", path, exc_info=True)
            continue

        for value in vars(module).values():
            if (
                not inspect.isclass(value)
                or value.__module__ != module.__name__
                or value is crawler_base
                or inspect.isabstract(value)
                or not issubclass(value, crawler_base)
            ):
                continue

            base_urls = _base_urls(getattr(value, "base_url", None))
            if not base_urls:
                continue
            relative_parts = path.relative_to(sources_dir).with_suffix("").parts
            site_id = "lncrawl_" + re.sub(r"[^a-z0-9_]+", "_", "_".join(relative_parts).lower()).strip("_")
            language = relative_parts[0]
            adapter = LNCrawlBridgeAdapter(
                value,
                site_id,
                novel_cls,
                chapter_cls,
                source_language={"en": "English", "ar": "Arabic"}[language],
            )
            adapter.can_search = (
                callable(getattr(value, "search", None))
                and getattr(value, "search") is not getattr(crawler_base, "search", None)
            )
            AdapterRegistry.register(adapter)
            registered += 1

    if not registered:
        raise RuntimeError(
            f"No compatible lncrawl source crawlers were found under {sources_dir}"
        )
    logger.info(
        "Registered %d lncrawl source adapters from %s (%d source import failures)",
        registered,
        checkout,
        import_failures,
    )
    return registered
