"""tofu_search.providers — Optional capability seams for host integration.

The standalone library runs fully without any of these. A host application
(e.g. chatui) can register implementations to unlock the capabilities that
were previously chatui-only:

  * **Browser provider** — fetch a URL / run a search through a real browser
    the user controls (e.g. a WebSocket browser extension). Used as a last
    resort when server-side HTTP is blocked (403/429/timeout) or an engine
    pool returns nothing.
  * **Auth-source provider** — supply stored cookies/proxy for login-walled
    domains (e.g. Xiaohongshu) so the Playwright pool can replay a logged-in
    session.

Design mirrors the trading-plugin ``tofu.providers`` seam: dependency points
INWARD (host → library); the library never imports the host. Every hook is
optional and defended so a missing/raising provider degrades to the
anonymous pipeline rather than crashing it.
"""

from __future__ import annotations

import threading
from typing import Optional

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'BrowserProvider',
    'AuthSourceProvider',
    'register_browser_provider',
    'register_auth_source_provider',
    'get_browser_provider',
    'get_auth_source_provider',
]


class BrowserProvider:
    """Interface for host-supplied browser automation.

    Subclass and override the methods you support; the defaults are inert
    (``is_connected`` → False, fetches → None), so a partial implementation
    is safe. Register an instance via :func:`register_browser_provider`.
    """

    def is_connected(self) -> bool:
        """Return True when the browser channel is live and usable."""
        return False

    def fetch_url(self, url: str, *, max_chars: int | None = None,
                  timeout: int = 15) -> Optional[str]:
        """Fetch ``url`` through the host browser; return extracted text or None."""
        return None

    def fetch_html(self, url: str, *, timeout: int = 20) -> Optional[str]:
        """Fetch the RAW HTML of ``url`` through the host browser.

        Unlike :meth:`fetch_url` (which returns host-extracted *text*), this
        returns the unparsed HTML so the library can run its own engine-grade
        parser on it. Used by :func:`tofu_search.search.browser_fallback` to
        scrape a search-results page through the user's browser while keeping
        the parsing logic inside the library (not duplicated in the host).

        Default returns None — hosts that can't supply raw HTML simply don't
        unlock the browser search fallback. Returning None makes
        ``search_via_browser`` fall back to the host's own :meth:`search`.
        """
        return None

    def search(self, query: str, *, max_results: int = 8) -> list[dict]:
        """Run a web search through the host browser; return result dicts or [].

        This is the LAST-resort hook: it asks the host to perform the entire
        search (fetch + parse). Prefer implementing :meth:`fetch_html` instead,
        which lets the library own the result parsing. ``search_via_browser``
        only calls this when :meth:`fetch_html` returns None.
        """
        return []


class AuthSourceProvider:
    """Interface for host-supplied authenticated-source lookups.

    A "source" is a dict carrying at least ``domain`` and ``cookies``
    (Playwright cookie dicts), optionally ``proxy`` and ``enabled``.
    """

    def match_source(self, url: str) -> Optional[dict]:
        """Return the auth-source row whose domain matches ``url``, or None."""
        return None

    def get_source(self, domain: str) -> Optional[dict]:
        """Return the auth-source row for ``domain``, or None."""
        return None


_lock = threading.Lock()
_browser_provider: Optional[BrowserProvider] = None
_auth_source_provider: Optional[AuthSourceProvider] = None


def register_browser_provider(provider: Optional[BrowserProvider]) -> None:
    """Install (or clear, with ``None``) the global browser provider."""
    global _browser_provider
    with _lock:
        _browser_provider = provider
    logger.info('[Providers] browser provider %s',
                'registered' if provider else 'cleared')


def register_auth_source_provider(provider: Optional[AuthSourceProvider]) -> None:
    """Install (or clear, with ``None``) the global auth-source provider."""
    global _auth_source_provider
    with _lock:
        _auth_source_provider = provider
    logger.info('[Providers] auth-source provider %s',
                'registered' if provider else 'cleared')


def get_browser_provider() -> Optional[BrowserProvider]:
    """Return the registered browser provider, or None."""
    with _lock:
        return _browser_provider


def get_auth_source_provider() -> Optional[AuthSourceProvider]:
    """Return the registered auth-source provider, or None."""
    with _lock:
        return _auth_source_provider
