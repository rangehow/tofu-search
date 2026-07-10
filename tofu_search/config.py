"""tofu_search.config — Configuration management.

Replaces chatui's lib/__init__.py config constants with a standalone
dataclass-based configuration. Supports both global defaults (via
configure()) and per-call overrides.
"""

import os
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

__all__ = ['SearchConfig', 'get_config', 'configure']


@dataclass
class SearchConfig:
    """Configuration for the tofu-search pipeline.

    All values have sane defaults matching chatui's production settings.
    Users can override via configure() or per-call kwargs.
    """

    # ── Fetch settings ──
    fetch_top_n: int = 6
    fetch_timeout: int = 15
    fetch_max_chars_search: int = 60_000
    fetch_max_chars_direct: int = 200_000
    fetch_max_chars_pdf: int = 0  # 0 = unlimited
    fetch_max_bytes: int = 20 * 1024 * 1024  # 20 MB

    # ── Wall-clock deadlines (robustness against wedged/dead hosts) ──
    # Total budget for ONE perform_web_search() call. When exceeded, the
    # pipeline force-returns whatever it has gathered so far (partial results +
    # a ``_deadline_hit`` marker) instead of blocking on slow hosts: it caps the
    # fetch-wait loop and short-circuits the LLM-filter / deepen / rerank stages.
    # The ONLY prior caps were a 20s engine timeout and a 90s fetch timeout, and
    # the 90s only exits early once ``kept_ok >= target_ok`` — a count a
    # niche-domain query never reaches, so it hung the full 90s (and then some).
    # 0 disables the cap (legacy unbounded behaviour). Env: TOFU_SEARCH_DEADLINE_SECS.
    search_deadline_secs: int = 45
    # Total budget for ONE fetch_page_content() URL, bounding the whole fallback
    # chain (HTTP body-download + browser + Playwright) so a single dead host
    # can't stack per-hop timeouts (do_request body deadline is timeout*3=45s,
    # then a 15-25s browser fallback, then a 15s Playwright render) into 60s+.
    # Soft bound: it clamps the HTTP hop and SKIPS any further fallback once the
    # budget is blown, so worst case ≈ deadline + one in-flight hop.
    # 0 disables the cap. Env: TOFU_SEARCH_FETCH_URL_DEADLINE_SECS.
    fetch_url_deadline_secs: int = 25

    # ── Per-engine request throttle (self-inflicted rate-limit guard) ──
    # Minimum interval (ms) between two requests to the SAME search engine,
    # enforced process-globally in http_search_get just before the GET. Two
    # CONCURRENT search calls (e.g. two parallel recommend batches) that would
    # otherwise hit one engine within the same second — the cause of the
    # observed DDG-HTML ``202 (rate-limited)`` — serialize to >= this interval
    # instead. Per-engine (a busy engine never blocks a different one), with a
    # small upward jitter so two colliding threads desynchronize. The wait is
    # clamped to the request timeout, so it spends budget the caller already
    # has and never pushes a query past its deadline. 0 disables the throttle
    # (byte-identical to the old unthrottled path). Only wraps the HTML-engine
    # envelope — the arXiv/Semantic-Scholar JSON vertical path is NOT throttled.
    # Env: TOFU_SEARCH_MIN_REQUEST_INTERVAL_MS.
    min_request_interval_ms: int = 400

    # ── Proxy ──
    # Explicit proxy URL (e.g. 'http://10.0.0.1:8080'). A host that resolves a
    # proxy from its own Settings (e.g. chatui) injects it here; when empty the
    # standard https_proxy / http_proxy / all_proxy env vars are used instead.
    proxy_url: str = ''
    # When a proxy IS available, try BOTH network paths (proxied ↔ direct) per
    # engine and remember which one worked (see search/proxy_mode.py). This is
    # why "search works on one machine but not another" — a container behind a
    # proxy with no env var, or a host with a stale/dead proxy env var, or a
    # soft-blocked egress IP. With no proxy configured this is a no-op (single
    # direct attempt). Set False to force one attempt on the proxy path only.
    proxy_dual_attempt: bool = True

    # ── Security ──
    # Block fetches whose host resolves to a private / loopback / link-local /
    # reserved address (SSRF guard). Applies to the initial URL *and* every
    # redirect hop. Leave on unless you deliberately fetch internal hosts.
    block_private_addresses: bool = True
    # When a TLS certificate cannot be verified, retry with verification
    # DISABLED. Off by default — enabling exposes those fetches to MITM.
    allow_insecure_ssl_fallback: bool = False

    # ── Domains to skip (media, social, etc.) ──
    skip_domains: set = field(default_factory=lambda: {
        'youtube.com', 'youtu.be', 'twitter.com', 'x.com',
        'facebook.com', 'instagram.com', 'tiktok.com',
        'linkedin.com', 'discord.com',
    })

    # ── SearXNG public instances (rotated to spread load / survive blocks) ──
    # Public instances churn — override this list when the defaults go stale.
    searxng_instances: list = field(default_factory=lambda: [
        'https://search.indst.eu',
        'https://search.einfachzocken.eu',
        'https://priv.au',
        'https://paulgo.io',
        'https://search.charliewhiskey.net',
        'https://search.freestater.org',
        'https://search.catboy.house',
        'https://search.hbubli.cc',
        'https://opnxng.com',
    ])

    # ── LLM configuration for content filter ──
    # Option A: OpenAI-compatible endpoint
    llm_api_key: str = ''
    llm_base_url: str = 'https://api.openai.com/v1'
    llm_model: str = 'gpt-4o-mini'
    llm_temperature: float = 0.0

    # Option B: Custom callable — takes (messages, **kwargs) -> str
    # If set, this overrides the OpenAI endpoint config.
    llm_function: Optional[Callable] = None

    # ── Content filter settings ──
    filter_enabled: bool = True
    filter_min_chars: int = 3000
    filter_timeout: int = 300

    # ── Pre-fetch relevance gate ──
    # A cheap, no-LLM lexical check (title+snippet vs query terms) that runs
    # BEFORE a result is fetched, so obviously off-topic SERP junk (e.g. a
    # health page returned for an academic query) is never fetched. Fail-open
    # by design — see search/prefetch_gate.py.
    prefetch_gate_enabled: bool = True
    # Below this many meaningful query terms the gate is a no-op (fetch all).
    prefetch_gate_min_query_terms: int = 2
    # Always fetch at least this many leading candidates (recall floor).
    prefetch_gate_min_fetch: int = 3

    def has_llm(self) -> bool:
        """Return True if an LLM is configured (either callable or API key)."""
        return bool(self.llm_function) or bool(self.llm_api_key)

    def copy(self, **overrides) -> 'SearchConfig':
        """Create a copy with specific fields overridden."""
        import dataclasses
        return dataclasses.replace(self, **overrides)


# ── Global singleton ──
_lock = threading.Lock()
_global_config = SearchConfig()


def get_config() -> SearchConfig:
    """Get the current global configuration (thread-safe)."""
    with _lock:
        return _global_config


def configure(**kwargs) -> SearchConfig:
    """Set global configuration values.

    Accepts any SearchConfig field name as a keyword argument.
    Returns the updated config.

    Example::

        from tofu_search import configure
        configure(
            llm_api_key='sk-...',
            llm_base_url='https://api.openai.com/v1',
            llm_model='gpt-4o-mini',
            fetch_top_n=10,
        )
    """
    global _global_config
    with _lock:
        def _as_bool(v: str) -> bool:
            return v.strip().lower() in ('1', 'true', 'yes', 'on')

        # Also support env var overrides
        env_mapping = {
            'FETCH_TOP_N': ('fetch_top_n', int),
            'FETCH_TIMEOUT': ('fetch_timeout', int),
            'FETCH_MAX_CHARS_SEARCH': ('fetch_max_chars_search', int),
            'FETCH_MAX_CHARS_DIRECT': ('fetch_max_chars_direct', int),
            'FETCH_MAX_CHARS_PDF': ('fetch_max_chars_pdf', int),
            'FETCH_MAX_BYTES': ('fetch_max_bytes', int),
            'TOFU_SEARCH_DEADLINE_SECS': ('search_deadline_secs', int),
            'TOFU_SEARCH_FETCH_URL_DEADLINE_SECS': ('fetch_url_deadline_secs', int),
            'TOFU_SEARCH_PROXY_URL': ('proxy_url', str),
            'TOFU_SEARCH_PROXY_DUAL_ATTEMPT': ('proxy_dual_attempt', _as_bool),
            'TOFU_SEARCH_MIN_REQUEST_INTERVAL_MS': ('min_request_interval_ms', int),
        }

        # Apply env var defaults (only for fields not explicitly set by user)
        for env_key, (field_name, cast) in env_mapping.items():
            if field_name not in kwargs:
                env_val = os.environ.get(env_key)
                if env_val is not None:
                    kwargs[field_name] = cast(env_val)

        _global_config = _global_config.copy(**kwargs)
        return _global_config
