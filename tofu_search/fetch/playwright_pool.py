"""tofu_search.fetch.playwright_pool — Lazy-loaded singleton Playwright browser pool.

Standalone version — replaces lib.compat dependency with inline platform check.
"""

import atexit
import queue as _queue_mod
import re
import sys
import threading
import time

from tofu_search.fetch.utils import HAS_PLAYWRIGHT
from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = ['PlaywrightPool']

IS_LINUX = sys.platform == 'linux'


class PlaywrightPool:
    """Lazy-loaded singleton Playwright browser instance."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._task_q = None
        self._ready = False
        self._started = False
        self._last_fail_ts = 0

    def _worker_loop(self, task_q):
        from playwright.sync_api import sync_playwright

        pw = None
        browser = None
        try:
            pw = sync_playwright().start()
            _launch_args = [
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
            ]
            if IS_LINUX:
                _launch_args.append('--disable-setuid-sandbox')
            try:
                browser = pw.chromium.launch(headless=True, args=_launch_args)
            except Exception as _launch_err:
                if 'executable' in str(_launch_err).lower():
                    logger.info('Playwright browser not installed — attempting auto-install...')
                    import subprocess
                    try:
                        subprocess.run(
                            ['python', '-m', 'playwright', 'install', 'chromium'],
                            timeout=120, capture_output=True, check=True,
                        )
                        browser = pw.chromium.launch(headless=True, args=_launch_args)
                    except Exception as _install_err:
                        logger.warning('Playwright auto-install failed: %s', _install_err, exc_info=True)
                        raise _launch_err from _install_err
                else:
                    raise
            logger.info('Playwright browser launched (dedicated thread)')
            self._ready = True
        except Exception as e:
            logger.warning('Playwright launch failed: %s', e, exc_info=True)
            self._ready = False
            while True:
                try:
                    _, result_q = task_q.get_nowait()
                    result_q.put(None)
                except _queue_mod.Empty:
                    break
            return

        while True:
            try:
                item = task_q.get()
            except Exception as e:
                logger.warning('[Fetch] browser worker task queue error: %s', e, exc_info=True)
                break
            if item is None:
                break
            (url, timeout, max_chars), result_q = item
            result = self._do_fetch(browser, url, timeout, max_chars)
            result_q.put(result)

        try:
            browser.close()
        except Exception as e:
            logger.debug('[Fetch] browser close failed: %s', e)
        try:
            pw.stop()
        except Exception as e:
            logger.debug('[Fetch] playwright stop failed: %s', e)

    def _do_fetch(self, browser, url, timeout, max_chars):
        from tofu_search.fetch.html_extract import extract_html_text

        context = None
        try:
            context = browser.new_context(
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/125.0.0.0 Safari/537.36'
                ),
                ignore_https_errors=True,
                java_script_enabled=True,
            )
            page = context.new_page()
            page.route('**/*.{png,jpg,jpeg,gif,svg,webp,ico,woff,woff2,ttf,otf,mp4,mp3,webm}',
                        lambda route: route.abort())
            page.route('**/analytics*', lambda route: route.abort())
            page.route('**/tracking*', lambda route: route.abort())

            t0 = time.time()
            page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')

            _max_render_wait = min(timeout, 12)
            try:
                page.wait_for_function(
                    'document.body && document.body.innerText.trim().length > 200',
                    timeout=_max_render_wait * 1000,
                )
            except Exception as e:
                logger.debug('[Fetch] page render wait timed out: %s: %s', url[:100], e)

            _prev_len = 0
            _stable_count = 0
            for _ in range(8):
                try:
                    _cur_len = page.evaluate('document.body.innerText.trim().length')
                except Exception:
                    break
                if _cur_len == _prev_len and _cur_len > 200:
                    _stable_count += 1
                    if _stable_count >= 2:
                        break
                else:
                    _stable_count = 0
                _prev_len = _cur_len
                page.wait_for_timeout(500)

            elapsed = time.time() - t0

            body_text = page.inner_text('body')
            body_text = re.sub(r'\n{3,}', '\n\n', body_text).strip()

            if body_text and len(body_text) > 50:
                if max_chars and len(body_text) > max_chars:
                    body_text = body_text[:max_chars] + '\n[...truncated]'
                logger.debug('Playwright OK: %s chars in %.1fs — %s', f'{len(body_text):,}', elapsed, url[:80])
                return body_text

            html = page.content()
            text = extract_html_text(html, max_chars or 0, url=url)
            if text and len(text) > 50:
                return text

            return None

        except Exception as e:
            logger.warning('Playwright error (%s) — %s: %s', type(e).__name__, url[:80], e, exc_info=True)
            return None
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass

    def _ensure_thread(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._ready
            if self._last_fail_ts and (time.time() - self._last_fail_ts < 60):
                return False
            if self._started and self._thread is not None:
                logger.info('Playwright thread died, restarting...')
            if not HAS_PLAYWRIGHT:
                if not self._started:
                    logger.info('Playwright not installed — SPA fallback disabled')
                self._started = True
                return False

            self._started = True
            self._task_q = _queue_mod.Queue()
            self._ready = False
            self._thread = threading.Thread(
                target=self._worker_loop,
                args=(self._task_q,),
                daemon=True,
                name='pw-worker',
            )
            self._thread.start()
            for _ in range(150):
                if self._ready or not self._thread.is_alive():
                    break
                time.sleep(0.1)
            if not self._ready:
                logger.error('Playwright thread failed to start browser')
                self._last_fail_ts = time.time()
            else:
                self._last_fail_ts = 0
                atexit.register(self._shutdown)
            return self._ready

    def _shutdown(self):
        if self._task_q:
            try:
                self._task_q.put(None)
            except Exception:
                pass

    def fetch(self, url, timeout=20, max_chars=None):
        if not self._ensure_thread():
            return None

        result_q = _queue_mod.Queue()
        self._task_q.put(((url, timeout, max_chars), result_q))
        try:
            return result_q.get(timeout=timeout + 30)
        except _queue_mod.Empty:
            logger.warning('Playwright worker timeout — %s', url[:80])
            return None


_pw_pool = PlaywrightPool()
