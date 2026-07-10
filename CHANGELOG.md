# Changelog

## 0.5.1

### Added
- **Per-engine request throttle (self-inflicted rate-limit guard).** Two
  CONCURRENT `perform_web_search()` calls (e.g. two parallel paper-recommend
  batches) could fire the same query at one HTML engine within the same second
  and trip its rate-limit — the observed DuckDuckGo `202 (rate-limited)` that
  emptied a whole batch. New process-global `search/_common.py::host_throttle`
  (`_HostThrottle`, mirroring `engine_circuit`) enforces a minimum interval
  between requests to the SAME engine, consulted inside `http_search_get` right
  after the circuit-breaker skip and just before the GET:
  - Per-engine locking — a wait on a busy engine never serializes a request to
    a DIFFERENT engine, so the orchestrator's engine+fetch overlap is preserved.
  - Upward-only jitter (`[0, +30%]` of the interval) so two colliding threads
    desynchronize instead of re-colliding on the next tick; realized spacing is
    always ≥ the configured interval.
  - The wait is clamped to the per-request `timeout`, so it consumes budget the
    caller already has and never pushes a query past its wall-clock deadline. A
    circuit-open engine returns BEFORE the throttle and spends zero interval.
  - Only the HTML-engine envelope is throttled. The arXiv / Semantic Scholar
    JSON vertical path uses a separate `http_get` and stays UNTHROTTLED — it is
    the breaker-independent fast path.
- `SearchConfig.min_request_interval_ms` (default 400, env
  `TOFU_SEARCH_MIN_REQUEST_INTERVAL_MS`). Set to 0 to disable the throttle
  (byte-identical to the old unthrottled path).
- Tests: `tests/test_host_throttle.py` (8) — same-engine spacing, different
  engines not serialized, jitter present + never below the interval floor,
  clamp-to-timeout, `http_search_get` ordering (throttle consulted for a
  healthy engine, skipped for a breaker-open one), plus two NEUTER bites
  (interval=0 removes spacing; removing the wiring fails the ordering test).

## 0.5.0

### Added
- **Hard wall-clock deadlines (robustness against wedged/dead hosts).** Two new
  `SearchConfig` knobs, both env-gated with safe defaults so a single-box
  install is unchanged (set to 0 to restore the old unbounded behaviour):
  - `search_deadline_secs` (default 45, env `TOFU_SEARCH_DEADLINE_SECS`) —
    total budget for one `perform_web_search()`. The ONLY prior caps were a 20s
    engine `as_completed` and a 90s fetch `as_completed`, and the 90s only
    short-circuits once `kept_ok >= target_ok` — a count a niche-domain query
    (mostly paywalled/dead hosts) never reaches, so the call hung the full 90s
    plus the LLM-filter/deepen/rerank tail. The deadline now bounds the
    fetch-wait loop (`min(90, budget_left)`), does NOT `shutdown(wait=True)` on
    a hit (that would re-introduce the hang), and short-circuits the
    filter/deepen/rerank stages. Force-returns partial results tagged
    `SearchResultList._deadline_hit=True`; a zero-result deadline sets
    `_search_diag['reason']='deadline'`. Emits a `[Fetch] ⏱ DEADLINE` /
    `[Search] ⏱ returned PARTIAL` log line.
  - `fetch_url_deadline_secs` (default 25, env
    `TOFU_SEARCH_FETCH_URL_DEADLINE_SECS`) — per-URL cap bounding the WHOLE
    fallback chain (HTTP body-download via a new `do_request(deadline_ts=…)`
    arg + browser + Playwright), so one dead host can't stack per-hop timeouts
    (body ≈ timeout×3, +browser 15-25s, +Playwright 15s) into 60s+. Soft bound:
    once blown, remaining fallback hops are skipped (not killed mid-flight), so
    worst case ≈ deadline + one in-flight hop.
  - Tests: `tests/test_deadline.py` (4) — deadline forces partial return within
    budget + per-URL cap skips the slow chain, each with a NEUTER-BITE sibling
    (knob=0 → the call provably exceeds the budget).

## 0.4.3

### Added
- **Entity-diversified rerank top-K for multi-entity comparison queries.**
  `search/rerank.py::_diversify_by_entity` (wired into `rerank_by_bm25`): when a
  query names ≥2 distinct entities that are actually present in the candidate
  hosts, the selected top-K is guaranteed to cover each named entity—its best
  BM25+authority candidate is picked first—before remaining slots are filled by
  global score. Fixes the "deep on 1 of N entities" weakness where the
  highest-scoring entity's pages could monopolise the whole top-K (e.g.
  comparing Cloudflare/Fastly/CloudFront). Within one entity the existing
  authority boost still decides the winner (official/primary over aggregator).
  Single-entity queries are unaffected (falls back to plain global top-K).
- New shared helper `search/authority.py::host_brand_labels(url)` — non-generic
  host brand labels, now the single source of truth for both OFFICIAL detection
  in `classify_authority` and entity attribution in rerank.

## 0.4.2

### Changed
- **Docs/packaging hygiene only — no behavioural change.** Documented in
  `pyproject.toml` that `lxml` is a *transitive* dependency of `trafilatura`,
  NOT imported by `tofu_search` directly: all BeautifulSoup parsing uses the
  `html.parser` backend (`search/_common.py:soup_of`) because lxml 6.x/libxml2
  can segfault under concurrent threads, and the sole lxml consumer
  (`trafilatura.extract`) is already serialized under `_TRAFILATURA_LOCK` in
  `fetch/html_extract.py`. The `lxml>=5.3` floor is retained to keep a
  known-good version resolved. This is a clarifying comment so future
  contributors do not switch any bs4 construction to the `lxml` parser.

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
