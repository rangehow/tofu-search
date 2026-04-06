"""tofu_search.fetch.http — HTTP request execution with SSL fallback.

Standalone version — browser extension fallback removed (chatui-specific).
"""

import time
from urllib.parse import urlparse

import requests as _requests_mod

from tofu_search.config import get_config
from tofu_search.fetch.utils import (
    _HAS_LEGACY_SSL,
    _fetch_cache,
    _session,
    _session_legacy_ssl,
    _session_no_ssl,
)
from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = ['HttpError', 'do_request', 'try_playwright_fallback']


class HttpError(Exception):
    """HTTP error with status code for caller differentiation."""
    def __init__(self, status_code, url):
        self.status_code = status_code
        self.url = url
        super().__init__(f'HTTP {status_code} for {url[:120]}')


def do_request(url, timeout, verify=True, legacy_ssl=False):
    """Execute a single GET request, return (resp, raw_bytes) or raise."""
    cfg = get_config()
    if legacy_ssl and _HAS_LEGACY_SSL:
        sess = _session_legacy_ssl
    elif verify:
        sess = _session
    else:
        sess = _session_no_ssl
    domain = urlparse(url).netloc[:40]
    logger.debug('-> GET %s  (timeout=%ds, ssl=%s)', url[:100], timeout, 'Y' if verify else 'N')
    t0 = time.time()
    resp = sess.get(url, timeout=(min(timeout, 8), timeout),
                    stream=True, allow_redirects=True, verify=verify)
    conn_ms = int((time.time() - t0) * 1000)
    if not resp.ok:
        status = resp.status_code
        resp.close()
        logger.debug('<- %d in %dms — %s', status, conn_ms, domain)
        raise HttpError(status, url)
    ct = resp.headers.get('Content-Type', '').lower()
    cl = int(resp.headers.get('Content-Length', 0) or 0)
    if cl > cfg.fetch_max_bytes:
        resp.close()
        raise HttpError(413, url)
    total_deadline = timeout * 3
    chunks, dl = [], 0
    oversized = False
    try:
        for chunk in resp.iter_content(65536):
            chunks.append(chunk); dl += len(chunk)
            if dl > cfg.fetch_max_bytes:
                oversized = True
                break
            if time.time() - t0 > total_deadline:
                logger.warning('Download exceeded %ss wall time — %s', total_deadline, url[:80])
                break
    except _requests_mod.exceptions.ContentDecodingError as e:
        resp.close()
        logger.warning('ContentDecodingError, retrying without br — %s: %s', domain, e)
        resp2 = sess.get(
            url, timeout=(min(timeout, 8), timeout),
            stream=True, allow_redirects=True, verify=verify,
            headers={'Accept-Encoding': 'gzip, deflate'},
        )
        if not resp2.ok:
            resp2.close()
            raise HttpError(resp2.status_code, url)
        chunks, dl = [], 0
        for chunk in resp2.iter_content(65536):
            chunks.append(chunk); dl += len(chunk)
            if dl > cfg.fetch_max_bytes:
                oversized = True
                break
            if time.time() - t0 > total_deadline:
                break
        elapsed_ms = int((time.time() - t0) * 1000)
        logger.debug('<- 200 %sB in %dms (no-br retry) ct=%s — %s',
                     f'{dl:,}', elapsed_ms, ct[:40], domain)
        return resp2, b''.join(chunks)
    if oversized:
        resp.close()
        logger.warning('Response body too large (%sB) — %s', f'{dl:,}', url[:80])
        raise HttpError(413, url)
    elapsed_ms = int((time.time() - t0) * 1000)
    logger.debug('<- 200 %sB in %dms  ct=%s — %s', f'{dl:,}', elapsed_ms, ct[:40], domain)
    return resp, b''.join(chunks)


def try_playwright_fallback(url, max_chars, timeout):
    """Try Playwright rendering for SPA / bot-protection fallback."""
    from tofu_search.fetch.playwright_pool import _pw_pool
    pw_text = _pw_pool.fetch(url, timeout=max(timeout, 15), max_chars=max_chars)
    if pw_text and len(pw_text) > 50:
        _fetch_cache.put(url, pw_text)
        if max_chars and len(pw_text) > max_chars:
            return pw_text[:max_chars] + '\n[...truncated]'
        return pw_text
    return None
