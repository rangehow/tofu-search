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

import random
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

from tofu_search.config import get_config
from tofu_search.log import get_logger
from tofu_search.search.proxy_mode import proxy_mode_manager

logger = get_logger(__name__)

__all__ = [
    'HEADERS', 'clean_text', 'http_search_get',
    'soup_of', 'make_result', 'search_session', 'engine_circuit',
    'host_throttle',
]

# A 200 response whose body is at least this large but parses to ZERO result
# blocks is treated as a soft block (consent wall / bot interstitial / locale
# redirect served 200), NOT a genuine "no matches" — worth retrying the other
# network path when a proxy is available. Matches the per-engine parse-health
# threshold used by the Bing/Brave parsers.
_SOFT_BLOCK_BODY_BYTES = 20_000


def _is_connection_failure(exc: Exception) -> bool:
    """True for connect/proxy/DNS-level failures worth retrying the OTHER path.

    A read-timeout (the endpoint accepted the connection but couldn't answer in
    time) is deliberately EXCLUDED — switching network path won't make a slow
    endpoint fast, and a full second attempt would blow the time budget.
    """
    if isinstance(exc, requests.exceptions.ProxyError):
        return True
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return False
    # ConnectionError covers DNS failure, connection refused/reset — but
    # ConnectTimeout (already handled) also subclasses it, so this is the
    # residual "couldn't establish the connection" bucket.
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    return False


# Blocking HTTP statuses that justify retrying the other network path: a proxy
# auth demand (407), an egress-IP block (403), rate-limit (429), or a 5xx that
# is often an interstitial served by a blocking edge.
_RETRYABLE_STATUSES = frozenset({403, 407, 429, 500, 502, 503, 504})

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
#  Per-engine request throttle (self-inflicted rate-limit guard)
# ═══════════════════════════════════════════════════════

class _HostThrottle:
    """Space out requests to the SAME engine to a minimum interval.

    Process-global and keyed by engine name (``'DDG-HTML'``, ``'Bing'`` …),
    exactly like :class:`_EngineCircuit`. The bug it fixes is two CONCURRENT
    ``perform_web_search`` calls (e.g. two parallel recommend batches) hitting
    one engine within the same second and tripping its rate-limit (the observed
    DDG-HTML ``202``). Because the state is a module global both calls consult,
    the second caller's request is delayed until the interval has elapsed.

    Per-engine locking: each engine has its OWN lock, so a wait on a busy
    engine never serializes a request to a DIFFERENT engine — the engine +
    fetch overlap the orchestrator relies on is preserved.

    Jitter is upward-only ([0, +JITTER_FRAC] of the interval): the realized
    spacing is always >= the configured interval, while two threads that would
    otherwise re-collide on the next tick desynchronize.
    """
    JITTER_FRAC = 0.30

    def __init__(self):
        self._guard = threading.Lock()        # guards _locks / _last mutation
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}     # engine -> last-request monotonic ts

    def _lock_for(self, name: str) -> threading.Lock:
        with self._guard:
            lk = self._locks.get(name)
            if lk is None:
                lk = self._locks[name] = threading.Lock()
            return lk

    def _interval(self) -> float:
        try:
            return max(0.0, get_config().min_request_interval_ms / 1000.0)
        except Exception:
            # Fail-open: a config error must never stall search.
            return 0.0

    def wait(self, name: str, *, max_wait: float | None = None) -> float:
        """Block until at least ``interval`` has elapsed since this engine's last
        request, then stamp the new request time. Returns the seconds actually
        slept (0.0 when the throttle is disabled or the interval already passed).

        ``max_wait`` clamps the sleep to the caller's remaining budget (the
        per-request timeout), so the throttle never pushes a query past its
        deadline.
        """
        interval = self._interval()
        if interval <= 0:
            return 0.0
        lk = self._lock_for(name)
        with lk:
            now = time.monotonic()
            last = self._last.get(name)
            slept = 0.0
            if last is not None:
                gap = now - last
                if gap < interval:
                    jitter = random.uniform(0.0, interval * self.JITTER_FRAC)
                    delay = (interval - gap) + jitter
                    if max_wait is not None:
                        delay = min(delay, max_wait)
                    if delay > 0:
                        time.sleep(delay)
                        slept = delay
            self._last[name] = time.monotonic()
            return slept

    def reset(self):
        """Drop all per-engine state (test isolation — this global is NOT reset
        by the shared conftest, mirroring engine_circuit)."""
        with self._guard:
            self._locks.clear()
            self._last.clear()


host_throttle = _HostThrottle()


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
    # (A benched engine returns here BEFORE the throttle, so it spends zero
    #  interval budget.)
    if engine_circuit.is_open(name):
        logger.info('%s skipped (circuit open) query=%r', tag, query[:60])
        return []

    # ── Per-engine request throttle: two concurrent search calls hitting this
    #    same engine within the interval serialize to >= it (self-inflicted
    #    rate-limit guard). Clamped to the request timeout so the wait spends
    #    budget the caller already has, never pushing past its deadline. Only
    #    the HTML-engine envelope is throttled — the JSON vertical path uses a
    #    separate http_get and stays unthrottled. ──
    host_throttle.wait(name, max_wait=float(timeout))

    hdrs = headers or HEADERS

    def _get(proxies_kwarg):
        kw = {'params': params, 'headers': hdrs, 'timeout': timeout}
        if proxies_kwarg is not None:
            kw['proxies'] = proxies_kwarg
        return search_session.get(url, **kw)

    # ── Adaptive proxy plan: one attempt when no proxy is configured (identical
    #    to the historical env-only path), else BOTH network paths in
    #    sticky-learned order. See search/proxy_mode.py. ──
    plan = proxy_mode_manager.attempt_plan(name, get_config())

    t0 = time.time()
    results: list = []
    failed = True   # until an attempt genuinely succeeds

    for idx, (mode, proxies_kwarg) in enumerate(plan):
        is_last = idx == len(plan) - 1
        try:
            resp = _get(proxies_kwarg)

            # Rate-limit retry (DDG-specific, opt-in) — same network path.
            if on_ratelimit_retry and resp.status_code == 202:
                logger.info('%s 202 (rate-limited), retry in 0.6s: %s', tag, query[:80])
                time.sleep(0.6)
                resp = _get(proxies_kwarg)

            if not resp.ok:
                # A blocking status (proxy-auth / egress-IP block / rate-limit /
                # 5xx interstitial) is worth trying the other network path.
                if not is_last and resp.status_code in _RETRYABLE_STATUSES:
                    logger.info('%s HTTP %d via %s — retrying alternate network path',
                                tag, resp.status_code, mode)
                    proxy_mode_manager.record_failure(name, mode)
                    continue
                logger.warning('%s returned HTTP %d via %s for query: %s',
                               tag, resp.status_code, mode, query[:80])
                break

            results = parser(resp) or []
            if len(results) > max_results:
                results = results[:max_results]

            # Soft block: a substantial 200 body that parses to ZERO results is
            # a consent wall / bot interstitial / locale redirect served 200,
            # not a genuine "no matches" — retry the other path if we have one.
            if (not results and not is_last
                    and len(getattr(resp, 'text', '') or '') > _SOFT_BLOCK_BODY_BYTES):
                logger.info('%s 200 but parsed 0 results (%d bytes) via %s — '
                            'likely soft block, retrying alternate network path',
                            tag, len(resp.text), mode)
                proxy_mode_manager.record_failure(name, mode)
                continue

            # Genuine success (even 0 matches on a small body = real no-match).
            failed = False
            proxy_mode_manager.record_success(name, mode)
            break

        except requests.RequestException as e:
            if not is_last and _is_connection_failure(e):
                logger.info('%s connect failure via %s (%s) — retrying alternate network path',
                            tag, mode, type(e).__name__)
                proxy_mode_manager.record_failure(name, mode)
                continue
            if isinstance(e, requests.Timeout):
                logger.warning('%s timeout via %s for query: %s', tag, mode, query[:80])
            else:
                logger.warning('%s request failed via %s for query %r: %s', tag, mode, query[:80], e)
            break
        except Exception as e:
            logger.error('%s error via %s: %s', tag, mode, e, exc_info=True)
            break

    if failed:
        engine_circuit.record_failure(name)
    else:
        # A successful HTTP round-trip resets the breaker even when the parse
        # yields 0 results — "no matches" is not an engine fault.
        engine_circuit.record_success(name)

    elapsed = time.time() - t0
    logger.info('%s: %d results in %.1fs  query=%r', tag, len(results), elapsed, query[:60])
    return results
