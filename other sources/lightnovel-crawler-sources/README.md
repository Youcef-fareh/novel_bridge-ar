# LightNovelCrawler source adapters

These source adapters are from the LightNovelCrawler project:

- Upstream: https://github.com/lncrawl/lightnovel-crawler
- Upstream project license: GPL-3.0-only (see [LICENSE](./LICENSE))
- Source catalog metadata: [`_index.json`](./_index.json)

This local copy includes NovelBridge-specific edits in selected adapters,
including Arabic language declarations for Kolnovel and Rewayat Club,
Kolnovel PDF-link filtering, and NovelLive mirror handling. Other files are
retained as an upstream source snapshot.

The source adapters are loaded by NovelBridge from this directory when the
optional LightNovelCrawler runtime dependencies are installed.
