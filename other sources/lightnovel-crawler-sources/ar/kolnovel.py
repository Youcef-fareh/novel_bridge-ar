import logging
from typing import Iterable, Optional
from urllib.parse import urlsplit

from lncrawl.templates.mangastream import MangaStreamTemplate
from lncrawl.core import Novel, PageSoup, Volume

logger = logging.getLogger(__name__)


class Kolnovel(MangaStreamTemplate):
    language = "Arabic"
    has_mtl = False
    has_manga = False
    base_url = ["https://kolnovel.com/"]

    def select_chapter_tags(
        self,
        tag: PageSoup,
        novel: Novel,
        volume: Optional[Volume] = None,
    ) -> Iterable[PageSoup]:
        chapter_tags = super().select_chapter_tags(tag, novel, volume)
        return [
            chapter
            for chapter in chapter_tags
            if not urlsplit(str(chapter.get_attr("href", "")))
            .path.rstrip("/")
            .lower()
            .endswith("/pdf")
        ]
