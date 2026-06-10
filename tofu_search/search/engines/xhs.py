"""tofu_search.search.engines.xhs — Xiaohongshu (小红书 / RED) search.

Xiaohongshu has no usable public search API; real keyword search needs a
logged-in cookie. This engine reuses the **auth-source provider** seam
(:mod:`tofu_search.providers`): when a host has registered a provider and the
user has connected ``xiaohongshu.com`` (cookies stored + enabled), we drive
the headless Playwright pool to the logged-in search-results page and scrape
the note cards from the rendered DOM.

If no provider is registered, or the source isn't connected, the engine
returns ``[]`` immediately — it never blocks the multi-engine pipeline.
"""

from __future__ import annotations

import time
from urllib.parse import quote

from tofu_search.log import get_logger
from tofu_search.providers import get_auth_source_provider
from tofu_search.search._common import clean_text

logger = get_logger(__name__)

__all__ = ['search_xhs', 'xhs_search_available']

_DOMAIN = 'xiaohongshu.com'
_SEARCH_URL = 'https://www.xiaohongshu.com/search_result?keyword={kw}&source=web_search_result_notes'

_WAIT_SELECTOR = 'section.note-item, div.note-item, a[href*="/search_result/"], a[href*="/explore/"]'

_EXTRACTOR_JS = r"""
(() => {
  const out = [];
  const seen = new Set();
  const anchors = Array.from(document.querySelectorAll(
    'a[href*="/explore/"], a[href*="/search_result/"]'));
  for (const a of anchors) {
    let href = a.href || '';
    if (!href) continue;
    try { href = new URL(href, location.origin).href; } catch (e) { continue; }
    if (!/\/(explore|search_result)\//.test(href)) continue;
    const key = href.split('?')[0];
    if (seen.has(key)) continue;
    let card = a.closest('section.note-item, div.note-item, section, div');
    let title = '';
    const titleNode = (card && (card.querySelector('.title, .note-title, span.title')))
      || a.querySelector('.title, span');
    if (titleNode) title = (titleNode.innerText || titleNode.textContent || '').trim();
    if (!title && card) {
      const txt = (card.innerText || '').trim().split('\n').map(s => s.trim()).filter(Boolean);
      if (txt.length) title = txt[0];
    }
    if (!title) title = (a.innerText || a.textContent || '').trim();
    if (!title) continue;
    let snippet = '';
    if (card) {
      const author = card.querySelector('.author, .name, .user-name');
      const count = card.querySelector('.count, .like-wrapper, .like');
      const parts = [];
      if (author) parts.push((author.innerText || '').trim());
      if (count) parts.push((count.innerText || '').trim());
      snippet = parts.filter(Boolean).join(' · ');
    }
    seen.add(key);
    out.push({ title: title.slice(0, 200), snippet: snippet.slice(0, 300), url: href });
    if (out.length >= 30) break;
  }
  return out;
})()
"""


def _get_source():
    provider = get_auth_source_provider()
    if provider is None:
        return None
    try:
        return provider.get_source(_DOMAIN)
    except Exception as e:
        logger.debug('[Search] XHS: auth-source lookup failed: %s', e)
        return None


def xhs_search_available() -> bool:
    """True when ``xiaohongshu.com`` is connected (enabled + has cookies)."""
    src = _get_source()
    return bool(src and src.get('enabled') and src.get('cookies'))


def search_xhs(query, max_results=10, freshness=''):
    """Search Xiaohongshu via the user's logged-in session.

    Returns ``{title, snippet, url, source}`` dicts, or ``[]`` when the source
    isn't connected or the scrape yields nothing. ``freshness`` is accepted
    for signature uniformity but unused.
    """
    t0 = time.time()
    src = _get_source()
    if not (src and src.get('enabled') and src.get('cookies')):
        logger.debug('[Search] XHS not connected — skipping')
        return []

    from tofu_search.fetch.playwright_pool import _pw_pool

    url = _SEARCH_URL.format(kw=quote(query))
    items = _pw_pool.search_authenticated(
        url,
        cookies=src.get('cookies') or [],
        proxy=src.get('proxy') or '',
        timeout=20,
        extractor_js=_EXTRACTOR_JS,
        wait_selector=_WAIT_SELECTOR,
    )
    if not items:
        logger.info('[Search] XHS: 0 results in %.1fs query=%r', time.time() - t0, query[:60])
        return []

    results = []
    for it in items:
        if not isinstance(it, dict):
            continue
        u = (it.get('url') or '').strip()
        title = clean_text(it.get('title') or '')
        if not u or not title or not u.startswith('http'):
            continue
        results.append({
            'title': title[:200],
            'snippet': clean_text(it.get('snippet') or '')[:300],
            'url': u,
            'source': 'Xiaohongshu',
        })
        if len(results) >= max_results:
            break

    logger.info('[Search] XHS: %d results in %.1fs query=%r',
                len(results), time.time() - t0, query[:60])
    return results
