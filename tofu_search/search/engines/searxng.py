"""tofu_search.search.engines.searxng — SearXNG public meta-search instances."""

import random
import re
from html import unescape

import requests

from tofu_search.log import get_logger
from tofu_search.search._common import HEADERS, clean_text

logger = get_logger(__name__)

__all__ = ['search_searxng']

_SEARXNG_INSTANCES = [
    'https://search.indst.eu',
    'https://search.einfachzocken.eu',
    'https://priv.au',
    'https://paulgo.io',
    'https://search.charliewhiskey.net',
    'https://search.freestater.org',
    'https://search.catboy.house',
    'https://search.hbubli.cc',
    'https://opnxng.com',
]


def _searxng_parse_html(html, max_results=6):
    """Parse SearXNG HTML search results page."""
    results = []
    article_blocks = re.split(r'<article[^>]*class="[^"]*result[^"]*result-default[^"]*"', html)
    if len(article_blocks) <= 1:
        article_blocks = re.split(r'<div[^>]*class="[^"]*result[^"]*result-default[^"]*"', html)
    for block in article_blocks[1:]:
        if len(results) >= max_results:
            break
        link_m = re.search(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        if not link_m:
            continue
        url = unescape(link_m.group(1))
        title = re.sub(r'<[^>]+>', '', link_m.group(2)).strip()
        if not title or not url.startswith('http'):
            continue

        snippet = ''
        snip_m = re.search(r'class="content"[^>]*>(.*?)</(?:p|span|div)>', block, re.DOTALL)
        if snip_m:
            snippet = re.sub(r'<[^>]+>', '', unescape(snip_m.group(1))).strip()

        results.append({
            'title': clean_text(title)[:200],
            'snippet': clean_text(snippet)[:500],
            'url': url,
            'source': 'SearXNG',
        })
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
        results.append({
            'title': clean_text(title)[:200],
            'snippet': clean_text(item.get('content', ''))[:500],
            'url': url,
            'source': 'SearXNG',
        })
    return results


def search_searxng(query, max_results=6):
    """Query public SearXNG instances with automatic failover."""
    shuffled = list(_SEARXNG_INSTANCES)
    random.shuffle(shuffled)
    for inst in shuffled[:3]:
        try:
            resp = requests.get(
                f'{inst}/search',
                params={'q': query, 'format': 'json', 'engines': 'google,bing,duckduckgo'},
                headers=HEADERS, timeout=5,
            )
            json_results = []
            if resp.ok and 'json' in resp.headers.get('content-type', ''):
                json_results = _searxng_parse_json(resp.json(), max_results)
                if json_results:
                    logger.info('[Search] SearXNG JSON from %s: %d results', inst, len(json_results))
                    return json_results

            if resp.status_code == 429:
                logger.debug('[Search] SearXNG 429 from %s, trying next instance', inst)
                continue

            if resp.status_code == 403 or not json_results:
                resp = requests.get(
                    f'{inst}/search',
                    params={'q': query},
                    headers=HEADERS, timeout=5,
                )
                if resp.ok and len(resp.text) > 500:
                    results = _searxng_parse_html(resp.text, max_results)
                    if results:
                        logger.info('[Search] SearXNG HTML from %s: %d results', inst, len(results))
                        return results

        except requests.Timeout:
            logger.debug('[Search] SearXNG timeout: %s', inst)
        except requests.RequestException as e:
            logger.debug('[Search] SearXNG %s failed: %s', inst, e)
        except Exception as e:
            logger.warning('[Search] SearXNG %s unexpected error: %s', inst, e, exc_info=True)

    logger.debug('[Search] SearXNG: all instances failed for query: %s', query[:80])
    return []
