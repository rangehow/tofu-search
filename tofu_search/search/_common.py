"""lib/search/_common.py — Shared constants and helpers for search engines.

Exposes:
  HEADERS          — standard User-Agent / Accept-Encoding headers
  clean_text       — HTML-strip + entity-decode + control-char cleanup
  http_search_get  — the shared "timed requests.get + error-handling + elapsed
                     logging" skeleton used by every engine. Accepts a
                     *parser* callable that converts the successful response
                     into a list of result dicts.

Engine modules under ``tofu_search/search/engines/`` use ``http_search_get`` so they
only own their parser and URL quirks — the HTTP envelope is DRY.
"""

import re
import threading
import time
import unicodedata
from collections.abc import Callable
from html import unescape

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'HEADERS', 'clean_text', 'http_search_get',
    'soup_of', 'make_result', 'search_session', 'engine_circuit',
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/121.0.0.0 Safari/537.36',
    'Accept-Encoding': 'gzip, deflate',      # avoid brotli decode issues
    'Accept-Language': 'en-US,en;q=0.9',
}


# ═══════════════════════════════════════════════════════
#  Shared HTTP session — connection pooling + retry
# ═══════════════════════════════════════════════════════
# A single Session reused by every engine amortises the TCP/TLS handshake
# across the 6 engines (and their retries) instead of opening a fresh
# connection per requests.get(). Retry covers transient connect failures and
# the rate-limit / 5xx status codes; read-timeout retries are disabled (a
# search endpoint that can't answer within the timeout won't on a retry).
_retry = Retry(
    total=2,
    connect=2,
    read=0,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=['GET'],
    raise_on_status=False,
)
search_session = requests.Session()
search_session.headers.update(HEADERS)
_adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=_retry)
search_session.mount('https://', _adapter)
search_session.mount('http://', _adapter)


# ═══════════════════════════════════════════════════════
#  Per-engine circuit breaker
# ═══════════════════════════════════════════════════════

class _EngineCircuit:
    """Skip an engine for a cooldown after consecutive failures.

    Keyed by engine name (``'Bing'``, ``'Brave'`` …). A run of
    ``FAIL_THRESHOLD`` failures (timeout / network error / non-2xx) trips the
    breaker; the engine is skipped for ``COOLDOWN`` seconds, then given another
    chance. Any success resets the counter. This stops a hard-down or
    IP-blocking engine from costing every query its full timeout budget.
    """
    FAIL_THRESHOLD = 3
    COOLDOWN = 120

    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}  # name -> {fails, tripped_at}

    def is_open(self, name: str) -> bool:
        with self._lock:
            st = self._state.get(name)
            if not st or st['tripped_at'] is None:
                return False
            if time.time() - st['tripped_at'] > self.COOLDOWN:
                del self._state[name]
                return False
            return True

    def record_failure(self, name: str):
        with self._lock:
            st = self._state.setdefault(name, {'fails': 0, 'tripped_at': None})
            st['fails'] += 1
            if st['fails'] >= self.FAIL_THRESHOLD and st['tripped_at'] is None:
                st['tripped_at'] = time.time()
                logger.warning('[Search] Circuit OPEN for engine %s — %d consecutive '
                               'failures, cooling down %ds', name, st['fails'], self.COOLDOWN)

    def record_success(self, name: str):
        with self._lock:
            self._state.pop(name, None)


engine_circuit = _EngineCircuit()


# ═══════════════════════════════════════════════════════
#  HTML parsing helpers
# ═══════════════════════════════════════════════════════

def soup_of(html: str) -> BeautifulSoup:
    """Parse HTML with the stdlib ``html.parser``.

    NOTE: ``html.parser`` is used deliberately, NOT ``lxml``. lxml/libxml2 is
    thread-unsafe under the orchestrator's concurrent worker pools and has
    been observed to segfault (see fetch/html_extract.py). html.parser is
    pure-Python and GIL-safe.
    """
    return BeautifulSoup(html, 'html.parser')


def make_result(title: str, snippet: str, url: str, source: str,
                *, title_max: int = 200, snippet_max: int = 500) -> dict:
    """Build a cleaned, length-capped search-result dict."""
    return {
        'title': clean_text(title)[:title_max],
        'snippet': clean_text(snippet)[:snippet_max],
        'url': url,
        'source': source,
    }


def clean_text(s):
    """Clean a search result string: strip HTML, decode entities, remove junk chars."""
    if not s:
        return ''
    s = re.sub(r'<[^>]+>', ' ', s)
    s = unescape(s)
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', s)
    s = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad]', '', s)
    s = unicodedata.normalize('NFC', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def http_search_get(
    *,
    name: str,
    url: str,
    params: dict,
    query: str,
    parser: Callable[[requests.Response], list],
    max_results: int = 6,
    timeout: int = 12,
    headers: dict | None = None,
    on_ratelimit_retry: bool = False,
) -> list:
    """Shared HTTP envelope for scraping search engines.

    Parameters
    ----------
    name : str
        Engine name (``'Bing'``, ``'Brave'``, ``'DDG-HTML'``…) — used in log
        prefixes only.
    url : str
        Full endpoint URL.
    params : dict
        Query-string parameters passed to ``requests.get``.
    query : str
        Original user query — logged for diagnostics only.
    parser : callable
        ``parser(response) -> list[dict]``. Only invoked for a successful
        (``resp.ok``) response. Parser owns all format-specific regex / HTML
        handling.
    max_results : int
        Cap on number of results — enforced after parse, trimming overflow.
    timeout : int
        Per-request timeout in seconds.
    headers : dict, optional
        Override headers. Defaults to module-level ``HEADERS``.
    on_ratelimit_retry : bool
        If ``True`` and HTTP 202 is returned, sleep 0.6 s and retry once
        (DDG rate-limit behavior). Other engines set it to ``False``.

    Returns
    -------
    list
        Up to ``max_results`` parsed result dicts. Empty list on any
        failure — every error path already logs with the correct level.
    """
    tag = f'[Search] {name}'

    # ── Circuit breaker: skip an engine that has been failing repeatedly ──
    if engine_circuit.is_open(name):
        logger.info('%s skipped (circuit open) query=%r', tag, query[:60])
        return []

    t0 = time.time()
    failed = False
    try:
        resp = search_session.get(url, params=params, headers=headers or HEADERS, timeout=timeout)

        # Rate-limit retry (DDG-specific, opt-in)
        if on_ratelimit_retry and resp.status_code == 202:
            logger.info('%s 202 (rate-limited), retry in 0.6s: %s', tag, query[:80])
            time.sleep(0.6)
            resp = search_session.get(url, params=params, headers=headers or HEADERS, timeout=timeout)

        if not resp.ok:
            logger.warning('%s returned HTTP %d for query: %s', tag, resp.status_code, query[:80])
            engine_circuit.record_failure(name)
            return []

        results = parser(resp) or []
        if len(results) > max_results:
            results = results[:max_results]

    except requests.Timeout:
        logger.warning('%s timeout for query: %s', tag, query[:80])
        results, failed = [], True
    except requests.RequestException as e:
        logger.warning('%s request failed for query %r: %s', tag, query[:80], e)
        results, failed = [], True
    except Exception as e:
        logger.error('%s error: %s', tag, e, exc_info=True)
        results, failed = [], True

    if failed:
        engine_circuit.record_failure(name)
    else:
        # A successful HTTP round-trip resets the breaker even when the parse
        # yields 0 results — "no matches" is not an engine fault.
        engine_circuit.record_success(name)

    elapsed = time.time() - t0
    logger.info('%s: %d results in %.1fs  query=%r', tag, len(results), elapsed, query[:60])
    return results
