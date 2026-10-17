# -*- coding: utf-8 -*-
import logging

from lncrawl.templates.wpcategory import WpCategoryTemplate

logger = logging.getLogger(__name__)


class TsundokuCrawler(WpCategoryTemplate):
    base_url = ["https://tsundoku.com.br/"]
    language = "pt"

    can_search = True

    chapter_body_selector = ".entry-content"

    # Every post in a category is a chapter here, so nothing has to be left out.
    landing_link_selector = ""
