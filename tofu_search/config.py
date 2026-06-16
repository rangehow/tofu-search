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
        # Also support env var overrides
        env_mapping = {
            'FETCH_TOP_N': ('fetch_top_n', int),
            'FETCH_TIMEOUT': ('fetch_timeout', int),
            'FETCH_MAX_CHARS_SEARCH': ('fetch_max_chars_search', int),
            'FETCH_MAX_CHARS_DIRECT': ('fetch_max_chars_direct', int),
            'FETCH_MAX_CHARS_PDF': ('fetch_max_chars_pdf', int),
            'FETCH_MAX_BYTES': ('fetch_max_bytes', int),
        }

        # Apply env var defaults (only for fields not explicitly set by user)
        for env_key, (field_name, cast) in env_mapping.items():
            if field_name not in kwargs:
                env_val = os.environ.get(env_key)
                if env_val is not None:
                    kwargs[field_name] = cast(env_val)

        _global_config = _global_config.copy(**kwargs)
        return _global_config
