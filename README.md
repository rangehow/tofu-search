# 🔍 tofu-search

**Multi-engine web search with LLM content filtering** — a standalone Python library extracted from the [Tofu AI assistant](https://github.com/tofu-ai/chatui).

## Features

- **5 search engines in parallel**: DuckDuckGo (HTML + API), Brave, Bing, SearXNG
- **Content deduplication**: Jaccard similarity on shingles (CJK + Latin aware)
- **Concurrent page fetching**: Race-to-N strategy with SSL fallback
- **LLM content filter** (optional): Relevance verdict + noise removal
- **BM25 reranking**: Pure-Python, no external API calls
- **SPA support**: Optional Playwright fallback for JS-rendered pages
- **PDF extraction**: Optional pymupdf integration

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

### With custom LLM callable

```python
from tofu_search import search, configure

def my_llm(messages, **kwargs):
    # Your LLM call here — receives OpenAI-format messages
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

## Configuration

```python
from tofu_search import configure

configure(
    # Search settings
    fetch_top_n=6,              # Max results to return
    fetch_timeout=15,           # HTTP timeout per request (seconds)
    fetch_max_chars_search=60000,  # Max chars per page in search results
    fetch_max_chars_direct=200000, # Max chars for direct fetch_url()

    # LLM settings (for content filter)
    llm_api_key="sk-...",
    llm_base_url="https://api.openai.com/v1",
    llm_model="gpt-4o-mini",

    # Or use a custom callable instead:
    # llm_function=my_callable,

    # Filter settings
    filter_enabled=True,        # Enable/disable LLM filter
    filter_min_chars=3000,      # Min chars to trigger LLM filter
)
```

All settings can also be set via environment variables:
- `FETCH_TOP_N`, `FETCH_TIMEOUT`, `FETCH_MAX_CHARS_SEARCH`, etc.

## Pipeline

The search pipeline runs 7 steps:

1. **Multi-engine search**: 5 engines (DDG×2 + Brave + Bing + SearXNG) run in parallel, producing ~72 raw results
2. **URL dedup**: Normalize and deduplicate by URL
3. **Content dedup**: Jaccard similarity on title+snippet shingles (CJK bigrams + Latin words)
4. **Page fetch**: Concurrent HTTP requests with race-to-N strategy
5. **LLM content filter** *(optional)*: Relevance verdict + noise removal
6. **BM25 rerank**: Score documents against query, select top-N
7. **Format**: Structure results for consumption

Steps 5 is automatically skipped when no LLM is configured.

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

## License

MIT
