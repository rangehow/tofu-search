"""tofu_search.search.engines.bing — Bing HTML scraping."""

import base64
import re
from html import unescape
from urllib.parse import parse_qs, urlparse

import requests

from tofu_search.log import get_logger
from tofu_search.search._common import HEADERS, clean_text

logger = get_logger(__name__)

__all__ = ['search_bing']


def _bing_decode_url(raw_url):
    """Decode Bing's /ck/a redirect URL to the real destination."""
    try:
        parsed = urlparse(raw_url)
        qs = parse_qs(parsed.query)
        if 'u' in qs:
            encoded = qs['u'][0]
            if encoded.startswith('a1'):
                payload = encoded[2:]
                padding = 4 - len(payload) % 4
                if padding != 4:
                    payload += '=' * padding
                return base64.b64decode(payload).decode('utf-8', errors='replace')
    except Exception as _e:
        logger.debug('[Search] Bing URL decode failed for %s: %s', raw_url[:80], _e)
    return raw_url


def search_bing(query, max_results=6):
    """Scrape Bing HTML search results."""
    results = []
    try:
        resp = requests.get(
            'https://www.bing.com/search',
            params={'q': query},
            headers=HEADERS, timeout=12,
        )
        if not resp.ok:
            logger.warning('[Search] Bing returned HTTP %d for query: %s', resp.status_code, query[:80])
            return results

        blocks = resp.text.split('class="b_algo"')
        for block in blocks[1:]:
            if len(results) >= max_results:
                break

            h2_m = re.search(
                r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                block, re.DOTALL)
            if not h2_m:
                continue
            raw_url = unescape(h2_m.group(1))
            title = re.sub(r'<[^>]+>', '', h2_m.group(2)).strip()

            url = _bing_decode_url(raw_url)
            if not url.startswith('http'):
                continue

            snippet = ''
            sm = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
            if sm:
                snippet = re.sub(r'<[^>]+>', '', unescape(sm.group(1))).strip()

            results.append({
                'title': clean_text(title)[:200],
                'snippet': clean_text(snippet)[:500],
                'url': url,
                'source': 'Bing',
            })
    except requests.Timeout:
        logger.warning('[Search] Bing timeout for query: %s', query[:80])
    except Exception as e:
        logger.error('[Search] Bing error: %s', e, exc_info=True)
    return results
