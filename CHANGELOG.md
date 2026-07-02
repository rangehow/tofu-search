# Changelog

## 0.4.1

### Added
- **Adaptive per-engine proxy strategy** (`search/proxy_mode.py`). The
  HTML-scraping engines share one `requests.Session` whose proxy behaviour was
  otherwise dictated entirely by ambient `HTTP(S)_PROXY` env vars, with a
  single attempt and no recovery — so "did search work?" depended purely on the
  installer's network topology (a container behind a proxy with no env var; a
  host with a stale/dead proxy env var; a datacenter/proxy egress IP soft-
  blocked by an engine). When a proxy IS available, each engine now tries BOTH
  network paths (proxied ↔ direct) and REMEMBERS which one worked (sticky,
  TTL'd), so steady state stays one request per engine. A fast connect/proxy
  failure, a blocking status (403/407/429/5xx), or a soft block (a substantial
  200 body that parses to 0 results) on the first path transparently retries
  the other; a read-timeout does NOT (switching paths won't make a slow
  endpoint fast, and it would blow the time budget). **With no proxy configured
  this is a no-op** — a single direct attempt, byte-identical to before.
- `SearchConfig.proxy_url` (host-injected proxy; falls back to
  `https_proxy`/`http_proxy`/`all_proxy` env vars) and
  `SearchConfig.proxy_dual_attempt` (default on). Env overrides
  `TOFU_SEARCH_PROXY_URL` / `TOFU_SEARCH_PROXY_DUAL_ATTEMPT`.

## 0.3.2

### Added
- **Pre-fetch relevance gate** (`search/prefetch_gate.py`). The pipeline used
  to submit *every* engine-returned URL to the fetch pool the instant an engine
  responded, and only judge relevance afterwards via the optional LLM content
  filter (which runs AFTER the expensive fetch). A junk SERP result (e.g. a
  consumer-health page returned for an academic query) was therefore fetched in
  full — wasting the fetch budget, flooding a host's browser/transport, and only
  then dropped. The new gate runs a cheap, pure-Python, no-LLM lexical check
  (`title + snippet` vs query terms, reusing the BM25 tokenizer) and declines to
  FETCH results with zero query-term overlap. It is deliberately **fail-open**:
  short queries (`< prefetch_gate_min_query_terms`, default 2) and the leading
  `prefetch_gate_min_fetch` (default 3) candidates always pass, and a skipped
  result is NOT dropped — it stays as a snippet-only candidate so rerank/format
  still see it. Only the page fetch is skipped.
- `SearchConfig.prefetch_gate_enabled` / `prefetch_gate_min_query_terms` /
  `prefetch_gate_min_fetch` knobs (default on; conservative). Set
  `prefetch_gate_enabled=False` to restore the old fetch-everything behaviour.

## 0.3.1

### Changed
- **Logging now defers to the embedding application.** `tofu_search.log` only
  attaches its own stderr handler when the ROOT logger has no handlers (true
  standalone use). When embedded in a host that already configured logging
  (handlers on the root logger), records propagate to the host's handlers
  instead of being double-printed to stderr — so the host controls routing and
  the pipeline diagnostics land in the host's log files.
- Routine Playwright worker timeouts (`queue.Empty`) no longer log a full
  traceback (`exc_info=True`). A network render that exceeds its budget is
  expected, not a crash; the warning now states the timeout value instead.

### Added
- `BrowserProvider.fetch_html(url, *, timeout=20)` seam — a host browser can
  return the RAW HTML of a page so the library parses it. `search_via_browser`
  now prefers this (fetching the DuckDuckGo SERP via the host browser and
  parsing it with the engine-grade bs4 parser) and only falls back to the
  host's `search()` when `fetch_html` is unavailable. This keeps SERP parsing
  inside the library instead of duplicated in every host.
- `tofu_search.search.engines.ddg.parse_ddg_html_text(html, ...)` — parse a raw
  DDG lite HTML string into result dicts, reusing the in-engine selectors.

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
