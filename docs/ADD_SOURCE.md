# Recipe: Adding a New Novel Source

NovelBridge uses a declarative selector-based template layer (`SoupAdapter` and `backend/adapters/templates/`) inspired by `lightnovel-crawler`.
Adding a new source requires **~15-30 lines of declarative Python code**, without having to re-implement pagination, HTTP sessions, bot checks, chapter body extraction, or health tallies.

---

## 1. Choose the Nearest Template

| Source Type | Base Class | Example Sites |
|---|---|---|
| **Madara / WordPress Manga** | `WordpressAdapter` | `ranovel.com`, `manhuaplus.org` |
| **NovelFull style** | `NovelFullAdapter` | `novelfull.com`, `allnovelupdates.com` |
| **Standard / Flat HTML** | `GenericAdapter` or `SoupAdapter` | `novelfire.net`, `freewebnovel.com` |
| **Complex SPA / Custom JS** | `SiteAdapter` | `wtrlab.com` |

---

## 2. Create the Adapter File

Create `backend/adapters/mysite.py`:

```python
"""
NovelBridge — MySite adapter.
"""
from __future__ import annotations

from backend.adapters.templates.generic import GenericAdapter


class MySiteAdapter(GenericAdapter):
    site_id = "mysite"
    base_url = "https://mysite.com"
    version = "1"
    tier = "legacy"

    novel_title_selectors = ["h1.novel-title", "meta[property='og:title']"]
    novel_author_selectors = [".author a", "meta[name='author']"]
    novel_cover_selectors = [".cover img", "meta[property='og:image']"]
    novel_description_selectors = [".synopsis", ".description"]

    chapter_list_selectors = [".chapter-list li a", ".chapters a"]
    chapter_body_selectors = [".chapter-content", "#chapter-body"]

    def can_handle(self, url: str) -> bool:
        return "mysite.com" in url.lower()
```

---

## 3. Register in `backend/adapters/__init__.py`

Add your class to `register_standard_adapters()` in `backend/adapters/__init__.py`.

---

## 4. Validate Offline

Run the offline validator to ensure all surface rules and imports pass:

```bash
python scripts/check_adapters.py
```

Regenerate the adapter index and documentation:

```bash
python scripts/gen_adapter_index.py
```

---

## 5. Test Live Diagnosis with `explain`

Inspect what NovelBridge detects for a live URL from the site:

```bash
python scripts/explain.py https://mysite.com/novel/my-novel-slug --probe
```

## Optional: Load all `lightnovel-crawler` sources

NovelBridge can load the English and Arabic crawler classes from a separate
[`lightnovel-crawler`](https://github.com/lncrawl/lightnovel-crawler) checkout
without copying its GPL-licensed source files into this MIT-licensed project.
Install its optional runtime dependencies into the same Python environment as
NovelBridge:

```powershell
pip install -r requirements-lncrawl.txt
```

If the checkout is at `lightnovel-crawler-dev/` under the project root, it is
detected automatically. Keep external site adapters in
`other sources/lightnovel-crawler-sources/` at the project root. This folder is
not ignored by Git, so you can review and commit those source files if desired.
Retain their upstream license and attribution files when redistributing them.
Alternatively, set `LNCRAWL_ROOT` to the checkout root (the directory
containing `lncrawl/`) and `LNCRAWL_SOURCES_ROOT` to a directory containing
`en/` and `ar/`:

```powershell
$env:LNCRAWL_ROOT = "C:\path\to\lightnovel-crawler-dev"
$env:LNCRAWL_SOURCES_ROOT = "C:\path\to\lightnovel-crawler-sources"
python -m backend.main
```

When `LNCRAWL_SOURCES_ROOT` is not set, NovelBridge checks
`LNCRAWL_ROOT\sources\`, then the project-level
`other sources\lightnovel-crawler-sources\`, then compatible sibling locations.
The desktop app can use the same folder layout; keep the crawler checkout,
source directory, and optional crawler dependencies available on the machine.

The Windows installer and portable release bundle the LightNovelCrawler 4.14.0
runtime, its optional Python dependencies, and the source catalog in the folder
layout above. The GPL-3.0 license and source-catalog license/attribution files
are included with the installed files. For local builds, provide the
LightNovelCrawler `v4.14.0` checkout and the source directory above before
running PyInstaller.

English and Arabic sources are discovered and registered automatically; source
modules that fail to import are reported in the application log. Each scrape
operation gets an isolated crawler instance, and the bridge supports both
`read_novel(novel)` and older no-argument `read_novel()` crawler interfaces.
When the source tree is outside the crawler checkout, NovelBridge also points
the crawler's own local-source setting at that same directory so crawler
initialization can find the moved sources.
The upstream NovelLive crawler handles the shared `lightnovelpub.me` and
`novellive.app` mirrors; these URLs are intentionally left to the external
LightNovelCrawler adapter rather than a competing built-in adapter.
