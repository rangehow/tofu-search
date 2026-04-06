"""tofu_search.fetch.core — Main fetch_page_content and batch fetching.

Standalone version — browser extension fallback removed (chatui-specific).
Uses tofu_search.config instead of lib.__init__ for all settings.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from tofu_search.config import get_config
from tofu_search.fetch.html_extract import extract_html_publish_date
from tofu_search.fetch.html_extract import extract_html_text as _extract_html_text
from tofu_search.fetch.http import HttpError as _HttpError
from tofu_search.fetch.http import do_request as _do_request
from tofu_search.fetch.http import try_playwright_fallback as _try_playwright_fallback
from tofu_search.fetch.utils import (
    _CACHE_EXTRACT_LIMIT,
    _HAS_LEGACY_SSL,
    _URL_RE,
    _circuit,
    _decode_bytes,
    _fetch_cache,
    _html_head_cache,
    _is_bot_extracted_text,
    _is_bot_protection,
    _is_known_spa,
    _looks_like_spa_shell,
    _normalize_code_hosting_url,
    _session,
    _should_fetch,
)
from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'fetch_page_content',
    'get_publish_date_from_url',
    'fetch_contents_for_results',
    'fetch_urls',
    'extract_urls_from_text',
]


def fetch_page_content(url, max_chars=None, pdf_max_chars=None, timeout=None):
    """Fetch and extract text content from a URL.

    Args:
        url: URL to fetch.
        max_chars: Max chars for HTML content. None = config default.
        pdf_max_chars: Max chars for PDF content. None = config default.
        timeout: Request timeout. None = config default.

    Returns:
        Extracted text string, or None on failure.
    """
    cfg = get_config()
    if max_chars is None: max_chars = cfg.fetch_max_chars_search
    if pdf_max_chars is None: pdf_max_chars = cfg.fetch_max_chars_pdf
    if timeout is None: timeout = cfg.fetch_timeout

    url = _normalize_code_hosting_url(url)
    if not _should_fetch(url):
        return None
    url_is_pdf = url.lower().rstrip('/').endswith('.pdf')
    cached = _fetch_cache.get(url)
    if cached is not None:
        logger.debug('Cache hit (%s chars) — %s', f'{len(cached):,}', url[:80])
        if max_chars and not url_is_pdf and len(cached) > max_chars:
            return cached[:max_chars] + '\n[...truncated]'
        return cached

    # Known SPA: skip requests, go straight to Playwright
    if _is_known_spa(url):
        logger.debug('Known SPA domain, using Playwright — %s', url[:80])
        return _try_playwright_fallback(url, max_chars, timeout)

    result = None
    is_pdf = False
    html_for_spa_check = None

    try:
        resp, raw = _do_request(url, timeout, verify=True)
    except _HttpError as e:
        if e.status_code in (401, 403, 404, 410, 413):
            logger.debug('HTTP %d — %s', e.status_code, url[:120])
        else:
            _circuit.record_failure(url)
            logger.warning('HTTP %d — %s', e.status_code, url[:120])
        return None
    except requests.exceptions.SSLError as e:
        is_legacy = 'UNSAFE_LEGACY_RENEGOTIATION' in str(e)
        if is_legacy and _HAS_LEGACY_SSL:
            logger.warning('SSL legacy renegotiation error, retrying — %s', urlparse(url).netloc)
            try:
                resp, raw = _do_request(url, timeout, legacy_ssl=True)
            except Exception as e2:
                _circuit.record_failure(url)
                logger.warning('SSL-legacy-fallback failed — %s: %s', url[:80], e2)
                return None
        else:
            logger.warning('SSL failed, retrying without verify — %s', urlparse(url).netloc)
            try:
                resp, raw = _do_request(url, timeout, verify=False)
            except Exception as e2:
                _circuit.record_failure(url)
                logger.warning('SSL-fallback failed — %s: %s', url[:80], e2)
                return None
    except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout):
        _circuit.record_failure(url)
        logger.warning('Timeout (%ds) — %s', timeout, url[:80])
        return None
    except requests.exceptions.ConnectionError as e:
        _circuit.record_failure(url)
        logger.warning('ConnectionError — %s: %s', url[:80], e)
        return None
    except requests.exceptions.ContentDecodingError as e:
        _circuit.record_failure(url)
        logger.warning('ContentDecodingError — %s: %s', url[:80], e)
        return None
    except Exception as e:
        _circuit.record_failure(url)
        logger.warning('%s — %s: %s', type(e).__name__, url[:80], e, exc_info=True)
        return None

    _circuit.record_success(url)

    try:
        ct = resp.headers.get('Content-Type', '').lower()
        is_pdf = ('application/pdf' in ct or url.lower().rstrip('/').endswith('.pdf')
                  or raw[:5] == b'%PDF-')
        if is_pdf:
            from tofu_search.fetch.pdf_extract import extract_pdf_text
            pdf_lim = pdf_max_chars if pdf_max_chars > 0 else 999999999
            result = extract_pdf_text(raw, max(pdf_lim, _CACHE_EXTRACT_LIMIT), url)
        elif 'text/plain' in ct:
            text = _decode_bytes(raw, resp.encoding).strip()
            result = (text[:_CACHE_EXTRACT_LIMIT] if len(text) > _CACHE_EXTRACT_LIMIT
                      else text) if len(text) > 30 else None
        else:
            html = _decode_bytes(raw, resp.encoding)
            if _is_bot_protection(html):
                logger.debug('Bot protection detected, trying Playwright — %s', url[:80])
                return _try_playwright_fallback(url, max_chars, timeout)
            html_for_spa_check = html
            _html_head_cache.put(url, html[:20480])
            result = _extract_html_text(html, _CACHE_EXTRACT_LIMIT, url=url)
    except Exception as e:
        logger.error('Parse error — %s: %s', url[:80], e, exc_info=True)
        return None

    # Post-extraction bot-protection check
    if result and _is_bot_extracted_text(result):
        pw_result = _try_playwright_fallback(url, max_chars, timeout)
        if pw_result:
            return pw_result
        return None

    # SPA shell detection
    if not is_pdf and html_for_spa_check and _looks_like_spa_shell(html_for_spa_check, result):
        pw_result = _try_playwright_fallback(url, max_chars, timeout)
        if pw_result:
            return pw_result
        if result and len(result) > 50:
            _fetch_cache.put(url, result)
            if max_chars and len(result) > max_chars:
                return result[:max_chars] + '\n[...truncated]'
            return result
        return None

    if result and len(result) > 50:
        _fetch_cache.put(url, result)
        logger.debug('OK (%s chars%s) — %s', f'{len(result):,}', ', PDF' if is_pdf else '', url[:80])
        if max_chars and not is_pdf and len(result) > max_chars:
            return result[:max_chars] + '\n[...truncated]'
        return result
    logger.debug('Empty result (len=%d) — %s', len(result) if result else 0, url[:80])
    return None


def get_publish_date_from_url(url, timeout=8):
    """Try to extract publication date from a URL's HTML meta tags."""
    if not url:
        return ''
    cached_html = _html_head_cache.get(url)
    if cached_html:
        return extract_html_publish_date(cached_html)

    try:
        sess = _session
        resp = sess.get(url, timeout=(5, timeout), stream=True,
                        allow_redirects=True, verify=True,
                        headers={'Range': 'bytes=0-20479'})
        try:
            if not resp.ok and resp.status_code != 206:
                return ''
            ct = resp.headers.get('Content-Type', '').lower()
            if 'html' not in ct and 'text' not in ct:
                return ''
            chunks = []
            total = 0
            for chunk in resp.iter_content(4096):
                chunks.append(chunk)
                total += len(chunk)
                if total >= 20480:
                    break
            html_head = b''.join(chunks).decode('utf-8', errors='replace')
        finally:
            resp.close()
        _html_head_cache.put(url, html_head)
        return extract_html_publish_date(html_head)
    except Exception as e:
        logger.debug('[Fetch] publish date request failed for %s: %s', url[:80], e)
        return ''


def fetch_contents_for_results(results, max_fetch=None,
                               max_chars=None, target_ok=None):
    """Fetch page content for search results concurrently (race-to-N)."""
    cfg = get_config()
    if max_chars is None: max_chars = cfg.fetch_max_chars_search
    if not results: return results
    if max_fetch is None:
        max_fetch = len(results)
    if target_ok is None:
        target_ok = cfg.fetch_top_n * 2
    to_fetch = results[:max_fetch]
    logger.info('[Fetch] starting %d URLs, target_ok=%d', len(to_fetch), target_ok)
    t0 = time.time()
    ok_count = 0
    def _do(r):
        return r, fetch_page_content(r['url'], max_chars=max_chars,
                                     pdf_max_chars=cfg.fetch_max_chars_pdf)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_do, r): r for r in to_fetch}
        pending = set(futs.keys())
        try:
            for fut in as_completed(futs, timeout=90):
                pending.discard(fut)
                try:
                    result, content = fut.result()
                    if content and len(content) > 50:
                        result['full_content'] = content
                        ok_count += 1
                except Exception as e:
                    logger.warning('[Fetch] thread error: %s', e, exc_info=True)
                if ok_count >= target_ok and pending:
                    elapsed_so_far = time.time() - t0
                    logger.info('[Fetch] Race-to-N: got %d/%d pages in %.1fs, cancelling %d slow',
                                ok_count, len(to_fetch), elapsed_so_far, len(pending))
                    for p in pending:
                        p.cancel()
                    break
        except TimeoutError:
            logger.warning('[Fetch] as_completed timeout (90s)')
    elapsed = time.time() - t0
    logger.info('[Fetch] done: %d/%d got content in %.1fs', ok_count, len(to_fetch), elapsed)
    return results


def fetch_urls(urls, max_chars=None, pdf_max_chars=None, timeout=None):
    """Fetch multiple URLs concurrently. Returns {url: content}."""
    cfg = get_config()
    if max_chars is None: max_chars = cfg.fetch_max_chars_direct
    if pdf_max_chars is None: pdf_max_chars = cfg.fetch_max_chars_pdf
    if timeout is None: timeout = cfg.fetch_timeout
    logger.debug('fetch_urls: starting %d URL(s)', len(urls))
    t0 = time.time()
    results = {}
    failed_urls = []
    def _do(u):
        return u, fetch_page_content(u, max_chars=max_chars,
                                     pdf_max_chars=pdf_max_chars, timeout=timeout)
    deadline = max(timeout * 4, 120)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(_do, u): u for u in urls}
        done_count = 0
        try:
            for fut in as_completed(futs, timeout=deadline):
                try:
                    url, content = fut.result()
                    if content and len(content) > 50:
                        results[url] = content
                    else:
                        failed_urls.append(url)
                except Exception as e:
                    logger.warning('[Fetch] fetch_urls thread error: %s', e, exc_info=True)
                    failed_urls.append(futs.get(fut, '?'))
                done_count += 1
        except TimeoutError:
            logger.warning('as_completed timeout: %d/%d done', done_count, len(futs))
    elapsed = time.time() - t0
    logger.debug('fetch_urls done: %d/%d succeeded in %.1fs', len(results), len(urls), elapsed)
    return results


def extract_urls_from_text(text):
    """Extract URLs from text string."""
    if not text: return []
    urls = _URL_RE.findall(text)
    seen, unique = set(), []
    for u in urls:
        u = u.rstrip('.,;:!?')
        if u not in seen and len(u) > 10: seen.add(u); unique.append(u)
    return unique[:5]
