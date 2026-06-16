"""tofu_search.search.vertical — Vertical domain search via free public APIs.

Detects structured identifiers in queries (stock tickers, CVE IDs, DOIs, arXiv
IDs — modern & legacy, package names, GitHub repos, IPv4/IPv6 addresses, HF
daily papers, Semantic Scholar related-work) and queries specialized free APIs
to provide structured data alongside regular web search.

All APIs used are free and require no API keys for basic usage (Semantic
Scholar's ceiling is raised by SEMANTIC_SCHOLAR_API_KEY).

This package was split out of a single 1000+ line module: one submodule per
vertical (``cve``, ``arxiv``, …), each exposing ``TYPE``, ``DOMAIN``,
``detect(query)`` and ``search(identifier, params)``; :mod:`registry` wires
them into the ordered detection chain + dispatch.
"""

from tofu_search.search.vertical.base import _FETCH_FAILED, _fetch_json
from tofu_search.search.vertical.registry import (
    detect_vertical_intent,
    list_domains,
    search_vertical,
    search_vertical_domain,
)

__all__ = [
    'detect_vertical_intent',
    'search_vertical',
    'search_vertical_domain',
    'list_domains',
    '_fetch_json',
    '_FETCH_FAILED',
]
