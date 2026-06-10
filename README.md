# 🔍 tofu-search

**Multi-engine web search + content fetching with optional LLM filtering** — a
standalone Python library extracted from the [Tofu AI assistant](https://github.com/rangehow/tofu-search).

This is a full re-extraction that keeps **100% of Tofu's current search/fetch
capabilities**: every engine, the structured "vertical" lookups, one-hop
deepening, the SPA/bot-protection Playwright fallback, authenticated-source
fetching, and the host-browser fallback — the last two exposed through
optional [provider seams](#host-integration-provider-seams) so the library
stays dependency-free when used standalone.

## Features

- **Multi-engine search (parallel)**: DuckDuckGo (HTML + API), Brave, Bing,
  SearXNG, Marginalia — plus Xiaohongshu when an auth-source provider supplies
  a logged-in session.
- **Vertical / structured search**: auto-detects CVE IDs, arXiv IDs, DOIs,
  stock tickers, PyPI/npm packages, GitHub repos, IP addresses, Hugging Face
  daily papers, and Semantic Scholar related-work — answered from the relevant
  free API alongside web results.
- **Content deduplication**: Jaccard similarity on shingles (CJK + Latin aware).
- **Concurrent page fetching**: Race-to-N strategy with SSL fallback + a
  per-domain circuit breaker.
- **One-hop deepening** *(opt-in)*: follow the best query-relevant outbound
  links one hop deeper, bounded like a crawl budget.
- **LLM content filter** *(optional)*: relevance verdict + noise removal. When
  no LLM is configured the step is silently skipped (raw text returned as-is).
- **BM25 reranking**: pure-Python, no external API calls.
- **SPA / bot-protection support**: optional Playwright fallback for
  JS-rendered and challenge pages.
- **PDF extraction**: optional pymupdf / pymupdf4llm integration.
- **Host integration seams**: register a browser provider (fetch/search via a
  real browser the user controls) and an auth-source provider (cookies/proxy
  for login-walled domains) — both no-ops by default.

## Quick Start

```bash
pip install tofu-search
```

### Basic search (no LLM required)

```python
from tofu_search import search

results = search("Python asyncio tutorial")
for r in results:
    print(f"{r['title']}: {r['url']}")
    if r.get('full_content'):
        print(f"  {r['full_content'][:200]}...")
```

### With OpenAI content filtering

```python
from tofu_search import search, configure

configure(
    llm_api_key="sk-...",
    llm_base_url="https://api.openai.com/v1",
    llm_model="gpt-4o-mini",
)

results = search("Python asyncio tutorial")
```

### With a custom LLM callable

```python
from tofu_search import search, configure

def my_llm(messages, **kwargs):
    # Your LLM call — receives OpenAI-format messages.
    # kwargs may include: stop, temperature, timeout
    return "response text"

configure(llm_function=my_llm)
results = search("Python asyncio tutorial")
```

### Fetch a single URL

```python
from tofu_search import fetch_url

content = fetch_url("https://example.com")
if content:
    print(f"Got {len(content)} characters")
```

### Vertical (structured-identifier) search

```python
from tofu_search import detect_vertical_intent, search_vertical

domain, identifier, params = detect_vertical_intent("CVE-2021-44228")
record = search_vertical(domain, identifier, params)
print(record['content'])   # CVSS score, description, references from NVD

# Or force a domain-level fan-out (free-text → Hugging Face + Semantic Scholar):
from tofu_search import search_vertical_domain
print(search_vertical_domain('academic', 'mamba state space models')['content'])
```

## Host integration (provider seams)

The standalone library never imports a host application. To unlock the two
host-only capabilities, register a provider — dependency points inward (host →
library), exactly like a plugin.

```python
from tofu_search import (
    BrowserProvider, AuthSourceProvider,
    register_browser_provider, register_auth_source_provider,
)

class MyBrowser(BrowserProvider):
    def is_connected(self): return True
    def fetch_url(self, url, *, max_chars=None, timeout=15): ...
    def search(self, query, *, max_results=8): ...

class MyAuth(AuthSourceProvider):
    def match_source(self, url): ...      # → {'domain','cookies','proxy',...} | None
    def get_source(self, domain): ...

register_browser_provider(MyBrowser())       # last-resort fetch/search fallback
register_auth_source_provider(MyAuth())      # cookies for login-walled domains
```

When no provider is registered, the browser fallback and authenticated fetch
paths are inert no-ops — the anonymous HTTP + Playwright pipeline runs as normal.

## Configuration

```python
from tofu_search import configure

configure(
    # Search / fetch settings
    fetch_top_n=6,                 # Max results to return
    fetch_timeout=15,              # HTTP timeout per request (seconds)
    fetch_max_chars_search=60000,  # Max chars per page in search results
    fetch_max_chars_direct=200000, # Max chars for direct fetch_url()

    # LLM settings (for content filter)
    llm_api_key="sk-...",
    llm_base_url="https://api.openai.com/v1",
    llm_model="gpt-4o-mini",
    # Or a custom callable instead:
    # llm_function=my_callable,

    # Filter settings
    filter_enabled=True,           # Enable/disable LLM filter
    filter_min_chars=3000,         # Min chars to trigger LLM filter
)
```

Many settings also read from environment variables: `FETCH_TOP_N`,
`FETCH_TIMEOUT`, `FETCH_MAX_CHARS_SEARCH`, `FETCH_MAX_CHARS_DIRECT`,
`FETCH_MAX_CHARS_PDF`, `FETCH_MAX_BYTES`. One-hop deepening is enabled with
`SEARCH_DEEPEN_HOPS=1` (or per call: `perform_web_search(..., deepen=True)`).
Semantic Scholar raises its rate limit with `SEMANTIC_SCHOLAR_API_KEY`.

## Pipeline

`perform_web_search` runs an overlapping streaming pipeline:

1. **Multi-engine search**: engines fire in parallel; each engine's URLs are
   deduped and submitted to the fetch pool the moment they arrive (the first
   page fetch starts before slow engines finish).
2. **URL dedup**: scheme/trailing-slash-insensitive keys.
3. **Content dedup**: Jaccard similarity on title+snippet shingles.
4. **Page fetch**: concurrent HTTP with race-to-N; SSL retry, circuit breaker,
   Playwright fallback for SPA/bot-protection pages.
   - **4b. Deepen** *(opt-in)*: one hop along the best query-relevant links.
5. **LLM content filter** *(optional)*: relevance verdict + noise removal.
6. **BM25 rerank**: score documents against the query, select top-N.

Step 5 is automatically skipped when no LLM is configured.

## Optional Dependencies

```bash
# SPA / JS-rendered page support
pip install tofu-search[playwright]
python -m playwright install chromium

# PDF extraction
pip install tofu-search[pdf]

# Everything
pip install tofu-search[all]
```

Or just run `./install.sh` (see below).

## Install script

```bash
./install.sh            # core deps
./install.sh --all      # core + playwright + pdf, and installs chromium
./install.sh --playwright
./install.sh --pdf
```

## License

MIT
