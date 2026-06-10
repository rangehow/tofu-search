"""tofu_search.search.browser_fallback — Host-browser fallback for web search.

When all server-side engines fail (network blocked), a host that registered a
:class:`tofu_search.providers.BrowserProvider` can run the search through the
user's own browser. No-op (returns []) when no provider is registered.
"""

from tofu_search.log import get_logger
from tofu_search.providers import get_browser_provider

logger = get_logger(__name__)

__all__ = ['search_via_browser']


def search_via_browser(query, max_results=8):
    """Fall back to the host browser provider for web search.

    Returns a list of ``{title, snippet, url, source}`` dicts, or [] when no
    browser provider is registered / connected or the search yields nothing.
    """
    provider = get_browser_provider()
    if provider is None:
        return []
    try:
        if not provider.is_connected():
            logger.info('[Search] Browser search fallback skipped (provider not connected) query=%r',
                        query[:80])
            return []
        logger.info('[Search] Browser search fallback TRIGGERED (all engines failed) query=%r',
                    query[:80])
        results = provider.search(query, max_results=max_results) or []
        logger.info('[Search] Browser search got %d results', len(results))
        return results
    except Exception as e:
        logger.error('[Search] Browser search fallback failed: %s', e, exc_info=True)
        return []
