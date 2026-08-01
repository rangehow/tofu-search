"""tofu_search.search.engines.xhs — Xiaohongshu (小红书 / RED) search.

Xiaohongshu has no usable public search API; real keyword search needs a
logged-in cookie. This engine reuses the **auth-source provider** seam
(:mod:`tofu_search.providers`): when a host has registered a provider and the
user has connected ``xiaohongshu.com`` (cookies stored + enabled), we drive
the headless Playwright pool to the logged-in search-results page and scrape
the note cards from the rendered DOM.

If no provider is registered, or the source isn't connected, the engine
returns ``[]`` immediately — it never blocks the multi-engine pipeline.

Account-risk guard
------------------
XHS polices automated access at the ACCOUNT level (限流 / 滑块 / 封号), and
this engine fires on EVERY web search while connected — without pacing, the
user's chat frequency becomes the request rate against their logged-in
session. :class:`_RiskGuard` applies the three cross-project consensus
mitigations for a read-only search scenario (see sibling MCP/crawler
projects: xiaohongshu-mcp, MediaCrawler, ReaJason/xhs, Spider_XHS):

  1. **Pacing + jitter** — a minimum interval between two real page loads,
     randomized a little, so requests never arrive at chat speed. A wait
     that would blow the engine's latency budget SKIPS the call instead of
     stalling the whole pipeline.
  2. **Same-keyword TTL cache** — a chat assistant re-asks near-identical
     queries constantly; each repeat served from cache is one less
     logged-in hit.
  3. **Consecutive-empty backoff** — a risk-control wall (captcha / forced
     login redirect) scrapes as ZERO note cards, indistinguishable from a
     legit no-hits query in isolation. SEVERAL consecutive empties across
     different queries is not: after ``_BACKOFF_AFTER_EMPTY`` of them the
     engine stops touching XHS for ``xhs_backoff_cooldown_s``. Hammering a
     flagged account escalates the flag; the pause is the mitigation.

All three knobs live on :class:`tofu_search.config.SearchConfig`
(``xhs_min_interval_s`` / ``xhs_cache_ttl_s`` / ``xhs_backoff_cooldown_s``).
"""

from __future__ import annotations

import random
import threading
import time
from urllib.parse import quote

from tofu_search.config import get_config
from tofu_search.log import get_logger
from tofu_search.providers import get_auth_source_provider
from tofu_search.search._common import clean_text

logger = get_logger(__name__)

__all__ = ['search_xhs', 'xhs_search_available']

_DOMAIN = 'xiaohongshu.com'
_SEARCH_URL = 'https://www.xiaohongshu.com/search_result?keyword={kw}&source=web_search_result_notes'

_WAIT_SELECTOR = 'section.note-item, div.note-item, a[href*="/search_result/"], a[href*="/explore/"]'

# Consecutive empty scrapes that trip the risk-control backoff.
_BACKOFF_AFTER_EMPTY = 3
# A pacing wait longer than this would push the engine past the orchestrator's
# per-engine timeout once the page load itself is added — skip instead.
_MAX_THROTTLE_WAIT_S = 6.0
# Same-keyword cache ceiling (FIFO eviction; entries are tiny).
_CACHE_MAX_ENTRIES = 64

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


class _RiskGuard:
    """Process-local XHS request pacing, query cache, and backoff state.

    One instance guards the one XHS account the process holds cookies for;
    the lock serializes real page loads so two concurrent chat searches can
    never hit XHS in the same instant (the pacing decision + wait happen
    under the lock — serializing XHS searches is precisely the point).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last_request_ts = 0.0
        self._consecutive_empty = 0
        self._cooldown_until = 0.0
        self._cache: dict = {}  # normalized query -> (stored_ts, results)

    def reset(self):
        """Test hook: drop all pacing/backoff/cache state."""
        with self._lock:
            self._last_request_ts = 0.0
            self._consecutive_empty = 0
            self._cooldown_until = 0.0
            self._cache.clear()

    def in_cooldown(self) -> bool:
        with self._lock:
            return time.time() < self._cooldown_until

    def cached_results(self, query: str, ttl_s: int):
        """Fresh cached results for ``query``, or None. Defensive copies."""
        if ttl_s <= 0:
            return None
        key = query.strip().lower()
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if not hit:
                return None
            ts, results = hit
            if now - ts > ttl_s:
                self._cache.pop(key, None)
                return None
            return [dict(r) for r in results]

    def wait_turn(self, min_interval_s: float) -> bool:
        """Block until this thread may fire the next page load.

        Returns False when the required wait would exceed
        ``_MAX_THROTTLE_WAIT_S`` — the caller then skips this round rather
        than blowing the engine's latency budget.
        """
        if min_interval_s <= 0:
            return True
        with self._lock:
            now = time.time()
            wait = (self._last_request_ts + min_interval_s
                    + random.uniform(0.0, min(3.0, min_interval_s))) - now
            if wait > _MAX_THROTTLE_WAIT_S:
                return False
            if wait > 0:
                time.sleep(wait)
            self._last_request_ts = time.time()
            return True

    def report_outcome(self, query: str, results: list, *, ttl_s: int,
                       cooldown_s: int) -> None:
        """Fold one real page load's outcome into cache + backoff state."""
        key = query.strip().lower()
        with self._lock:
            if results:
                self._consecutive_empty = 0
                if ttl_s > 0:
                    self._cache[key] = (time.time(), list(results))
                    while len(self._cache) > _CACHE_MAX_ENTRIES:
                        self._cache.pop(next(iter(self._cache)))
                return
            self._consecutive_empty += 1
            if self._consecutive_empty >= _BACKOFF_AFTER_EMPTY and cooldown_s > 0:
                self._cooldown_until = time.time() + cooldown_s
                self._consecutive_empty = 0
                logger.warning(
                    '[Search] XHS: %d consecutive empty scrapes — entering %ds '
                    'cooldown. A risk-control wall (安全验证/滑块) or an expired '
                    'cookie scrapes as zero note cards; continuing to hit a '
                    'flagged account escalates the flag. Re-check the account '
                    'in Settings → 需要登录的来源.',
                    _BACKOFF_AFTER_EMPTY, cooldown_s)


_GUARD = _RiskGuard()


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
    isn't connected, the guard skips the round (pacing / cooldown), or the
    scrape yields nothing. ``freshness`` is accepted for signature uniformity
    but unused.
    """
    t0 = time.time()
    src = _get_source()
    if not (src and src.get('enabled') and src.get('cookies')):
        logger.debug('[Search] XHS not connected — skipping')
        return []

    cfg = get_config()

    if _GUARD.in_cooldown():
        logger.info('[Search] XHS in risk-control cooldown — skipping query=%r', query[:60])
        return []

    hit = _GUARD.cached_results(query, cfg.xhs_cache_ttl_s)
    if hit is not None:
        logger.info('[Search] XHS: %d cached results query=%r', len(hit), query[:60])
        return hit[:max_results]

    if not _GUARD.wait_turn(cfg.xhs_min_interval_s):
        logger.info('[Search] XHS throttled (next slot beyond latency budget) — '
                    'skipping query=%r', query[:60])
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
        _GUARD.report_outcome(query, [], ttl_s=cfg.xhs_cache_ttl_s,
                              cooldown_s=cfg.xhs_backoff_cooldown_s)
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

    _GUARD.report_outcome(query, results, ttl_s=cfg.xhs_cache_ttl_s,
                          cooldown_s=cfg.xhs_backoff_cooldown_s)
    logger.info('[Search] XHS: %d results in %.1fs query=%r',
                len(results), time.time() - t0, query[:60])
    return results
