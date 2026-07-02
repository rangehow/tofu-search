"""tofu_search/search/engines/searxng.py — SearXNG public meta-search instances."""

import random

import requests

from tofu_search.config import get_config
from tofu_search.log import get_logger
from tofu_search.search._common import (
    HEADERS,
    make_result,
    search_session,
    soup_of,
)
from tofu_search.search.proxy_mode import proxy_mode_manager

logger = get_logger(__name__)

__all__ = ['search_searxng']


def _searxng_parse_html(html, max_results=6):
    """Parse SearXNG HTML search results page (bs4 selectors)."""
    results = []
    soup = soup_of(html)
    # SearXNG renders each result as <article class="result result-default">;
    # older / themed instances use <div class="result ...">.
    blocks = soup.select('article.result, div.result')
    for block in blocks:
        if len(results) >= max_results:
            break
        a = block.select_one('h3 a[href^="http"], a.url_header[href^="http"], a[href^="http"]')
        if not a:
            continue
        url = a['href']
        title = a.get_text(' ', strip=True)
        if not title or not url.startswith('http'):
            continue
        snip = block.select_one('.content, p.content')
        snippet = snip.get_text(' ', strip=True) if snip else ''
        results.append(make_result(title, snippet, url, 'SearXNG'))
    return results


def _searxng_parse_json(data, max_results=6):
    """Parse SearXNG JSON API response."""
    results = []
    for item in data.get('results', []):
        if len(results) >= max_results:
            break
        url = item.get('url', '')
        title = item.get('title', '')
        if not url or not title:
            continue
        results.append(make_result(title, item.get('content', ''), url, 'SearXNG'))
    return results


def search_searxng(query, max_results=6, freshness=''):
    """Query public SearXNG instances with automatic failover.

    Tries JSON API first (fast, structured), falls back to HTML scraping.
    Rotates across instances to spread load and survive rate-limits.

    Optimised for speed: 2 instances max, 2s timeout per request.
    Most public instances block datacenter IPs via 302→homepage redirect;
    we detect this and skip immediately.
    """
    import time as _time
    t0 = _time.time()
    cfg = get_config()
    shuffled = list(cfg.searxng_instances)
    random.shuffle(shuffled)
    _TIMEOUT = 2  # seconds — if SearXNG can't respond in 2s, it won't
    _MAX_INSTANCES = 2  # try at most 2 instances (was 3)
    # Resolve the preferred network path once (honours an explicit
    # config.proxy_url / the DIRECT env-bypass marker). SearXNG's own per-
    # instance rotation already provides path diversity, so we take just the
    # first planned attempt rather than looping both paths per instance.
    _proxies = proxy_mode_manager.attempt_plan('SearXNG', cfg)[0][1]
    _pkw = {'proxies': _proxies} if _proxies is not None else {}
    # SearXNG supports time_range param: day, week, month, year
    _FRESHNESS_MAP = {'day': 'day', 'week': 'week', 'month': 'month', 'year': 'year'}
    time_range = _FRESHNESS_MAP.get(freshness, '')

    for inst in shuffled[:_MAX_INSTANCES]:
        try:
            # Try JSON first (don't follow redirects — detect 302→homepage)
            json_params = {'q': query, 'format': 'json', 'engines': 'google,bing,duckduckgo'}
            if time_range:
                json_params['time_range'] = time_range
            resp = search_session.get(
                f'{inst}/search',
                params=json_params,
                headers=HEADERS, timeout=_TIMEOUT, allow_redirects=False,
                **_pkw,
            )

            # 302/301 → homepage redirect = bot block, skip immediately
            if resp.status_code in (301, 302):
                logger.debug('[Search] SearXNG %s redirected (%d) — bot block, skipping',
                             inst, resp.status_code)
                continue

            json_results = []
            if resp.ok and 'json' in resp.headers.get('content-type', ''):
                json_results = _searxng_parse_json(resp.json(), max_results)
                if json_results:
                    logger.info('[Search] SearXNG JSON from %s: %d results', inst, len(json_results))
                    return json_results

            # Rate-limited — skip to next instance immediately
            if resp.status_code == 429:
                logger.debug('[Search] SearXNG 429 from %s, trying next instance', inst)
                continue

            # JSON blocked (403) or empty — try HTML on same instance
            if resp.status_code == 403 or not json_results:
                html_params = {'q': query}
                if time_range:
                    html_params['time_range'] = time_range
                resp = search_session.get(
                    f'{inst}/search',
                    params=html_params,
                    headers=HEADERS, timeout=_TIMEOUT, allow_redirects=False,
                    **_pkw,
                )
                # Detect redirect again
                if resp.status_code in (301, 302):
                    logger.debug('[Search] SearXNG %s HTML redirected (%d) — bot block',
                                 inst, resp.status_code)
                    continue
                if resp.ok and len(resp.text) > 500:
                    results = _searxng_parse_html(resp.text, max_results)
                    if results:
                        logger.info('[Search] SearXNG HTML from %s: %d results', inst, len(results))
                        return results

        except requests.Timeout:
            logger.debug('[Search] SearXNG timeout (%ds): %s', _TIMEOUT, inst)
        except requests.RequestException as e:
            logger.debug('[Search] SearXNG %s failed: %s', inst, e)
        except Exception as e:
            logger.warning('[Search] SearXNG %s unexpected error: %s', inst, e, exc_info=True)

    elapsed = _time.time() - t0
    logger.info('[Search] SearXNG: all instances failed in %.1fs  query=%r', elapsed, query[:60])
    return []
