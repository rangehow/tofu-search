"""tofu_search.fetch.interactive_login — Headful login → cookie capture.

Drives a *non-headless* Chromium so the user can log in to a site (scan a QR
code, enter a password, …) once; we then capture the resulting session
cookies via ``storage_state``. The captured cookies are returned to the
caller, who can persist them into whatever auth-source store the host uses.

Why a separate, short-lived browser (not the pool)? The fetch pool launches
``headless=True`` and is shared across fetches — it can't be shown to the
user. Interactive login is a rare, user-initiated, one-at-a-time action.

Availability: requires a display on the host. On a headless box the launch
raises and we return ``{'ok': False, 'reason': 'unavailable'}``. Set
``TOFU_INTERACTIVE_LOGIN=0`` to force-disable.
"""

from __future__ import annotations

import os
import threading
import time

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = ['capture_login_cookies', 'is_interactive_login_available']

# Only one interactive login at a time — a visible browser is inherently a
# single-user, single-window action.
_login_lock = threading.Lock()

# Cookie names that signal a completed login per site.
_LOGIN_COOKIE_HINTS = {
    'xiaohongshu.com': ('web_session', 'customerClientId', 'galaxy_creator_session_id'),
}


def is_interactive_login_available() -> bool:
    """Best-effort check: is headful login plausibly usable here?"""
    if os.environ.get('TOFU_INTERACTIVE_LOGIN', '').strip() == '0':
        return False
    try:
        from tofu_search.fetch.utils import HAS_PLAYWRIGHT
        return bool(HAS_PLAYWRIGHT)
    except Exception as e:
        logger.debug('[Login] availability check failed: %s', e)
        return False


def capture_login_cookies(domain: str, login_url: str, timeout_s: int = 180) -> dict:
    """Open a visible browser at ``login_url`` and capture cookies on login.

    Blocks (up to ``timeout_s``) while the user signs in. On success returns
    ``{'ok': True, 'cookie_count': N, 'cookies': [...]}`` — the caller decides
    where to persist them (e.g. via an AuthSourceProvider-backed store).

    On failure returns ``{'ok': False, 'reason': ..., 'error': ...}`` where
    ``reason`` is ``'unavailable'`` / ``'timeout'`` / ``'busy'`` / ``'error'``.
    """
    if not is_interactive_login_available():
        return {'ok': False, 'reason': 'unavailable',
                'error': 'Interactive login disabled or Playwright not installed'}

    if not _login_lock.acquire(blocking=False):
        return {'ok': False, 'reason': 'busy',
                'error': 'Another interactive login is already in progress'}
    try:
        return _run_capture(domain, login_url, timeout_s)
    finally:
        _login_lock.release()


def _run_capture(domain: str, login_url: str, timeout_s: int) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger.warning('[Login] Playwright import failed: %s', e)
        return {'ok': False, 'reason': 'unavailable', 'error': str(e)}

    hints = _LOGIN_COOKIE_HINTS.get(domain, ())
    logger.info('[Login] launching headful browser for %s → %s (timeout=%ds)',
                domain, login_url, timeout_s)

    pw = None
    browser = None
    context = None
    try:
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=False, args=['--no-sandbox'])
        except Exception as e:
            logger.warning('[Login] headful launch failed (no display?) for %s: %s', domain, e)
            return {'ok': False, 'reason': 'unavailable', 'error': str(e)}

        context = browser.new_context(locale='zh-CN')
        page = context.new_page()
        page.goto(login_url, wait_until='domcontentloaded', timeout=30_000)

        deadline = time.time() + timeout_s
        captured = []
        while time.time() < deadline:
            if not context.pages:
                logger.info('[Login] window closed by user for %s', domain)
                break
            try:
                cookies = context.cookies()
            except Exception as e:
                logger.debug('[Login] cookies() read failed (window closing?): %s', e)
                break
            names = {c.get('name') for c in cookies}
            if hints and any(h in names for h in hints):
                captured = cookies
                logger.info('[Login] login cookie detected for %s (%d cookies)',
                            domain, len(cookies))
                break
            time.sleep(1.5)
        else:
            try:
                captured = context.cookies()
            except Exception as e:
                logger.debug('[Login] final cookies() read failed: %s', e)
                captured = []

        if not captured:
            try:
                captured = context.cookies()
            except Exception:
                captured = []

        if not captured:
            return {'ok': False, 'reason': 'timeout',
                    'error': 'No session cookies captured (login not completed?)'}

        logger.info('[Login] captured %d cookies for %s', len(captured), domain)
        return {'ok': True, 'cookie_count': len(captured), 'cookies': captured}
    except Exception as e:
        logger.error('[Login] capture failed for %s: %s', domain, e, exc_info=True)
        return {'ok': False, 'reason': 'error', 'error': str(e)}
    finally:
        for closer, label in ((context, 'context'), (browser, 'browser')):
            if closer is not None:
                try:
                    closer.close()
                except Exception as e:
                    logger.debug('[Login] %s close failed: %s', label, e)
        if pw is not None:
            try:
                pw.stop()
            except Exception as e:
                logger.debug('[Login] playwright stop failed: %s', e)
