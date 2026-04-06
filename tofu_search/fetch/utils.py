"""tofu_search.fetch.utils — Fetch infrastructure: sessions, SSL, circuit breaker, helpers.

Standalone version — replaces all `import lib as _lib` references with
tofu_search.config.get_config().
"""

import logging as _logging
import re
import ssl
import threading
import time
from urllib.parse import urlparse

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from tofu_search.config import get_config
from tofu_search.log import get_logger

# Suppress InsecureRequestWarning for SSL-fallback retries
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_logging.getLogger('urllib3.connectionpool').setLevel(_logging.ERROR)

logger = get_logger(__name__)

__all__: list[str] = []


# ═══════════════════════════════════════════════════════
#  Lazy third-party imports
# ═══════════════════════════════════════════════════════

def _get_bs4():
    from bs4 import BeautifulSoup
    return BeautifulSoup


try:
    import trafilatura  # noqa: F401
    HAS_TRAFILATURA = True
except ImportError as e:
    trafilatura = None  # type: ignore[assignment]
    HAS_TRAFILATURA = False
    logger.warning('[Fetch] trafilatura not installed — advanced text extraction disabled: %s', e)

try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    HAS_PLAYWRIGHT = True
except ImportError:
    sync_playwright = None  # type: ignore[assignment]
    HAS_PLAYWRIGHT = False

try:
    import pymupdf  # noqa: F401
    HAS_FITZ = True
except ImportError:
    pymupdf = None  # type: ignore[assignment]
    HAS_FITZ = False

try:
    from PIL import Image  # noqa: F401
    HAS_PIL = True
except ImportError:
    Image = None  # type: ignore[assignment]
    HAS_PIL = False

HAS_PYPDF2 = False


# ═══════════════════════════════════════════════════════
#  Constants & compiled patterns
# ═══════════════════════════════════════════════════════

_URL_RE = re.compile(r'https?://[^\s<>"\')\]，。！？、）】}]+[^\s<>"\')\]，。！？、）】}.,:;!?]')

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}


# ═══════════════════════════════════════════════════════
#  Connection pools (with retry strategy)
# ═══════════════════════════════════════════════════════

_retry_strategy = Retry(
    total=2, read=0, connect=2, backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=['GET', 'HEAD'],
    raise_on_status=False,
)

_session = requests.Session()
_session.headers.update(_HEADERS)
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=_retry_strategy)
_session.mount('https://', _adapter)
_session.mount('http://', _adapter)

_session_no_ssl = requests.Session()
_session_no_ssl.headers.update(_HEADERS)
_session_no_ssl.verify = False
_adapter_no_ssl = HTTPAdapter(pool_connections=5, pool_maxsize=10, max_retries=_retry_strategy)
_session_no_ssl.mount('https://', _adapter_no_ssl)
_session_no_ssl.mount('http://', _adapter_no_ssl)

# Legacy TLS renegotiation session
try:
    _legacy_ctx = ssl.create_default_context()
    _legacy_ctx.check_hostname = False
    _legacy_ctx.verify_mode = ssl.CERT_NONE
    _legacy_ctx.options |= getattr(ssl, 'OP_LEGACY_SERVER_CONNECT', 0x4)

    class _LegacySSLAdapter(HTTPAdapter):
        def init_poolmanager(self, *a, **kw):
            kw['ssl_context'] = _legacy_ctx
            return super().init_poolmanager(*a, **kw)
        def proxy_manager_for(self, proxy, **kw):
            kw['ssl_context'] = _legacy_ctx
            return super().proxy_manager_for(proxy, **kw)

    _session_legacy_ssl = requests.Session()
    _session_legacy_ssl.headers.update(_HEADERS)
    _session_legacy_ssl.verify = False
    _session_legacy_ssl.mount('https://', _LegacySSLAdapter(
        pool_connections=5, pool_maxsize=10, max_retries=_retry_strategy))
    _session_legacy_ssl.mount('http://', _adapter_no_ssl)
    _HAS_LEGACY_SSL = True
except Exception as e:
    _logging.getLogger(__name__).warning('Legacy SSL adapter unavailable: %s', e, exc_info=True)
    _session_legacy_ssl = _session_no_ssl
    _HAS_LEGACY_SSL = False


# ═══════════════════════════════════════════════════════
#  Domain circuit breaker
# ═══════════════════════════════════════════════════════

class _DomainCircuitBreaker:
    FAIL_THRESHOLD = 5
    COOLDOWN       = 120
    WINDOW         = 120

    def __init__(self):
        self._lock = threading.Lock()
        self._domains: dict = {}

    def _get_domain(self, url):
        try:
            return urlparse(url).netloc.lower()
        except Exception as e:
            logger.debug('Failed to parse domain from URL: %.80s: %s', url, e)
            return ''

    def is_open(self, url):
        domain = self._get_domain(url)
        if not domain:
            return False
        with self._lock:
            state = self._domains.get(domain)
            if not state or state['tripped_at'] is None:
                return False
            if time.time() - state['tripped_at'] > self.COOLDOWN:
                del self._domains[domain]
                return False
            return True

    def record_failure(self, url):
        domain = self._get_domain(url)
        if not domain:
            return
        now = time.time()
        with self._lock:
            state = self._domains.get(domain)
            if not state:
                self._domains[domain] = {'fails': 1, 'first_fail': now, 'tripped_at': None}
                return
            if now - state['first_fail'] > self.WINDOW:
                self._domains[domain] = {'fails': 1, 'first_fail': now, 'tripped_at': None}
                return
            state['fails'] += 1
            if state['fails'] >= self.FAIL_THRESHOLD and state['tripped_at'] is None:
                state['tripped_at'] = now
                logger.warning('Circuit OPEN for %s — %d failures in %.0fs',
                               domain, state['fails'], now - state['first_fail'])

    def record_success(self, url):
        domain = self._get_domain(url)
        if not domain:
            return
        with self._lock:
            self._domains.pop(domain, None)

_circuit = _DomainCircuitBreaker()


# ═══════════════════════════════════════════════════════
#  URL cache
# ═══════════════════════════════════════════════════════

def _compute_cache_limit():
    cfg = get_config()
    return max(cfg.fetch_max_chars_direct, cfg.fetch_max_chars_search) * 2

_CACHE_EXTRACT_LIMIT = _compute_cache_limit()


class _FetchCache:
    def __init__(self, ttl=600, max_size=200):
        self._data, self._lock = {}, threading.Lock()
        self._ttl, self._max = ttl, max_size
    def get(self, url):
        with self._lock:
            e = self._data.get(url)
            if e and e[1] > time.time(): return e[0]
            self._data.pop(url, None); return None
    def put(self, url, content):
        if not content: return
        with self._lock:
            if len(self._data) >= self._max:
                del self._data[min(self._data, key=lambda k: self._data[k][1])]
            self._data[url] = (content, time.time() + self._ttl)
    @property
    def size(self):
        with self._lock: return len(self._data)

_fetch_cache = _FetchCache(ttl=600, max_size=200)
_html_head_cache = _FetchCache(ttl=600, max_size=300)


# ═══════════════════════════════════════════════════════
#  Bot protection detection
# ═══════════════════════════════════════════════════════

def _is_bot_protection(html_text):
    if not html_text:
        return False
    lower = html_text[:51200].lower()
    indicators = (
        'probe.js', '/challenge', 'captcha', 'cf-browser-verification',
        'just a moment', 'checking your browser', 'ddos-guard',
        '_cf_chl', 'cf_chl_opt', 'turnstile', 'bot detection',
        'security check', 'security verification',
        'please wait', 'verify you are human', 'access denied',
        'attention required', 'enable javascript and cookies',
        'ray id:', 'performance &amp; security by',
        'performance & security by cloudflare',
        'checking if the site connection is secure',
    )
    matched = sum(1 for s in indicators if s in lower)
    if matched >= 1:
        m = re.search(r'<body[^>]*>(.*?)</body>', html_text, re.DOTALL | re.I)
        if not m or len(m.group(1).strip()) < 200:
            return True
        if matched >= 2 and len(m.group(1).strip()) < 3000:
            return True
    m = re.search(r'<body[^>]*>(.*?)</body>', html_text, re.DOTALL | re.I)
    if m and len(m.group(1).strip()) < 30 and '<script' in lower:
        return True
    return False


_BOT_TEXT_PATTERNS = (
    'ray id:', 'security verification', 'verify you are human',
    'checking your browser', 'please complete the security check',
    'enable javascript and cookies',
    'checking if the site connection is secure',
    'this process is automatic',
    'performance & security by cloudflare',
    'performance and security by cloudflare',
    'attention required', 'ddos protection by',
    'just a moment', 'access denied',
    'cf-browser-verification',
)


def _is_bot_extracted_text(text):
    if not text or len(text) > 600:
        return False
    lower = text.lower()
    return any(p in lower for p in _BOT_TEXT_PATTERNS)


# ═══════════════════════════════════════════════════════
#  Encoding detection
# ═══════════════════════════════════════════════════════

def _decode_bytes(raw, hint):
    meta_enc = None
    try:
        m = re.search(rb'charset\s*=\s*["\']?\s*([A-Za-z0-9_-]+)', raw[:4096], re.I)
        if m: meta_enc = m.group(1).decode('ascii', errors='ignore')
    except Exception as e:
        logger.debug('[Fetch] charset extraction failed: %s', e)
    CATCHALL = frozenset({'iso-8859-1','latin-1','latin1','ascii','us-ascii'})
    cands = []
    if meta_enc: cands.append(meta_enc)
    cands.append('utf-8')
    if hint and hint.lower().replace('_','-') not in CATCHALL: cands.append(hint)
    cands.extend(['gb18030','gbk','big5','latin-1'])
    seen = set()
    for enc in cands:
        if not enc: continue
        k = enc.lower().replace('_','-')
        if k in seen: continue
        seen.add(k)
        try: return raw.decode(enc)
        except Exception as e:
            logger.debug('[Fetch] decode attempt failed for encoding=%s: %s', enc, e)
    return raw.decode('utf-8', errors='replace')


# ═══════════════════════════════════════════════════════
#  SPA detection helpers
# ═══════════════════════════════════════════════════════

SPA_DOMAINS = frozenset({
    'feishu.cn', 'open.feishu.cn', 'larksuite.com',
    'notion.so', 'notion.site',
    'yuque.com',
    'docs.google.com',
    'airtable.com',
    'figma.com',
    'miro.com',
    'app.slack.com',
    'web.telegram.org',
    'trello.com',
    'canva.com',
    'v0.dev',
})

_SPA_MIN_TEXT_LEN = 150


def _is_known_spa(url):
    try:
        host = urlparse(url).netloc.lower()
        return any(host == d or host.endswith('.' + d) for d in SPA_DOMAINS)
    except Exception as e:
        logger.debug('[Fetch] SPA domain check failed: %s', e)
        return False


def _looks_like_spa_shell(raw_html, extracted_text):
    if not raw_html:
        return False
    html_str = raw_html if isinstance(raw_html, str) else raw_html.decode('utf-8', errors='ignore')
    html_len = len(html_str)
    text_len = len(extracted_text) if extracted_text else 0

    _SPA_MARKERS = (
        'id="root"', 'id="app"', 'id="__next"', 'id="__nuxt"',
        'id="main-app"', 'id="react-root"', 'id="vue-app"',
        'data-reactroot', 'ng-app=', 'ng-version=',
    )
    html_lower = html_str[:8000].lower()
    has_spa_marker = any(m.lower() in html_lower for m in _SPA_MARKERS)

    if has_spa_marker and text_len < _SPA_MIN_TEXT_LEN:
        return True
    if html_len > 2000 and text_len < 100:
        return True
    return False


# ═══════════════════════════════════════════════════════
#  Code-hosting URL normalization
# ═══════════════════════════════════════════════════════

_GITLAB_BLOB_RE = re.compile(
    r'^(?P<base>https?://[^/]*gitlab[^/]*/'
    r'(?:[^/]+/)+)-/blob/(?P<rest>.+)$'
)
_BITBUCKET_SRC_RE = re.compile(
    r'^(?P<base>https?://bitbucket\.org/'
    r'[^/]+/[^/]+)/src/(?P<rest>.+)$'
)
_ARXIV_WRAPPER_RE = re.compile(
    r'^https?://(?:www\.)?'
    r'(?P<host>alphaxiv\.org|arxiv-vanity\.com)'
    r'/(?P<path_type>abs|pdf|html|overview|resources)'
    r'/(?P<paper_id>\d{4}\.\d{4,5}(?:v\d+)?)\s*$'
)


def _normalize_code_hosting_url(url):
    clean = url.split('#')[0]

    m = _ARXIV_WRAPPER_RE.match(clean.strip())
    if m:
        paper_id = m.group('paper_id')
        path_type = m.group('path_type')
        if path_type in ('overview', 'resources'):
            path_type = 'abs'
        return f'https://arxiv.org/{path_type}/{paper_id}'

    m = _GITLAB_BLOB_RE.match(clean)
    if m:
        raw_url = f'{m.group("base")}-/raw/{m.group("rest")}'.split('?')[0]
        return raw_url

    m = _BITBUCKET_SRC_RE.match(clean)
    if m:
        raw_url = f'{m.group("base")}/raw/{m.group("rest")}'.split('?')[0]
        return raw_url

    return url


def _should_fetch(url):
    try:
        cfg = get_config()
        p = urlparse(url)
        if any(s in p.netloc.lower() for s in cfg.skip_domains):
            return False
        if any(p.path.lower().endswith(e) for e in
               ('.jpg','.jpeg','.png','.gif','.svg','.mp4','.mp3','.zip','.tar','.gz','.exe')):
            return False
        if _circuit.is_open(url):
            logger.warning('Skipped (circuit open): %s', url[:80])
            return False
        return True
    except Exception as e:
        logger.warning('Exception in URL filter: %s', e, exc_info=True)
        return False
