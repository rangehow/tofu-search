"""tofu_search.search.vertical — Vertical domain search via free public APIs.

Detects structured identifiers in queries (stock tickers, CVE IDs, DOIs, arXiv
IDs — modern & legacy, package names, GitHub repos, IPv4/IPv6 addresses, HF
daily papers, Semantic Scholar related-work) and queries specialized free APIs
to provide structured data alongside regular web search.

All APIs used are free and require no API keys for basic usage (Semantic
Scholar's ceiling is raised by SEMANTIC_SCHOLAR_API_KEY).

The ``travel`` domain is the exception: it is backed by the RollingGo MCP
endpoints and its ``hotel`` type needs ``ROLLINGGO_API_KEY``. Availability is
tracked per TYPE, and :func:`describe_domains` reports which sub-capabilities
are usable right now so a host never advertises one that cannot answer.

This package was split out of a single 1000+ line module: one submodule per
vertical (``cve``, ``arxiv``, …), each exposing ``TYPE``, ``DOMAIN``,
``detect(query)`` and ``search(identifier, params)``; :mod:`registry` wires
them into the ordered detection chain + dispatch.
"""

from tofu_search.search.vertical.registry import (
    DOMAIN_META,
    available_types,
    describe_domains,
    detect_vertical_intent,
    list_domains,
    search_vertical,
    search_vertical_domain,
)

# NOTE: the shared HTTP seam (``base.http_get`` / ``base._fetch_json`` /
# ``base._post_json``) is deliberately NOT re-exported here. Handlers import
# ``base`` directly so tests patch exactly one module, and a leading underscore
# in a package ``__all__`` would advertise private names as public API.
__all__ = [
    'detect_vertical_intent',
    'search_vertical',
    'search_vertical_domain',
    'list_domains',
    'describe_domains',
    'available_types',
    'DOMAIN_META',
]
