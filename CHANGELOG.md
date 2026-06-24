# Changelog

## 0.3.0

### Added
- `fetch_url_bytes(url, timeout=None, max_bytes=None)` — download the raw bytes
  of a (binary) file asset, returning `(bytes, content_type)` or `None`.
  Enforces the same scheme / SSRF / size-cap policy as the text pipeline.
- `looks_like_text_asset(url)` — classify a URL by extension as a source/markup
  file (`.svg`, `.json`, `.py`, `.css`, …) vs. a prose web page.

### Changed
- **`fetch_page_content` now returns text-based file assets as their raw
  source** instead of `None`. SVG, JSON, XML, YAML, CSS, JS and source-code
  URLs (matched by Content-Type) are returned verbatim, bypassing the
  HTML/article extraction and the article-oriented bot/SPA/min-length gates so
  small-but-complete files (e.g. a 40-char JSON) are not dropped.
- `_should_fetch` no longer rejects `.svg` URLs (SVG is text). Binary media
  (`.jpg/.jpeg/.png/.gif/.mp4/.mp3/.zip/.tar/.gz/.exe`) is still skipped.

### Migration note
This changes public fetch behavior: SVG/JSON/source URLs that previously
returned `None` now return their source text. Callers that relied on `None`
for those content types should branch on `looks_like_text_asset()` instead.

## 0.2.0

- Initial standalone release: multi-engine search, vertical/structured search,
  concurrent fetching, optional LLM content filter, BM25 rerank, provider seams.
