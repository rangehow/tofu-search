"""Shared infrastructure for vertical-search handlers.

Every per-vertical module imports this module (``from ... import base``) and
calls ``base.http_get`` / ``base._fetch_json`` so the HTTP seam stays in one
place and is uniformly patchable in tests.
"""

import time

from tofu_search.http_client import http_get
from tofu_search.log import get_logger

logger = get_logger(__name__)

_TIMEOUT = 10
_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; TofuBot/1.0)'}

# Sentinel distinguishing "request/parse failed" from "API said no data".
_FETCH_FAILED = object()


def _fetch_json(url, *, params=None, headers=None, timeout=_TIMEOUT,
                label='', retry_on_429=True):
    """GET ``url`` and return parsed JSON, or ``_FETCH_FAILED`` on any error.

    Centralises the ``http_get → check .ok → .json() → except`` boilerplate
    repeated across every vertical handler, plus a single bounded retry on
    HTTP 429 (rate-limited). Returns the sentinel ``_FETCH_FAILED`` — NOT
    ``None`` — so callers can tell a transport/parse failure apart from a
    successful response that simply carried no useful data.
    """
    hdrs = headers or _HEADERS
    for attempt in (0, 1):
        try:
            resp = http_get(url, params=params, headers=hdrs, timeout=timeout)
        except Exception as e:
            logger.warning('[Vertical] %s request failed for %s: %s', label or 'fetch', url[:80], e)
            return _FETCH_FAILED
        if resp.status_code == 429 and retry_on_429 and attempt == 0:
            logger.info('[Vertical] %s rate-limited (429), retry in 1s', label or 'fetch')
            time.sleep(1.0)
            continue
        if not resp.ok:
            logger.warning('[Vertical] %s returned HTTP %d for %s',
                           label or 'fetch', resp.status_code, url[:80])
            return _FETCH_FAILED
        try:
            return resp.json()
        except Exception as e:
            logger.warning('[Vertical] %s JSON parse failed for %s: %s', label or 'fetch', url[:80], e)
            return _FETCH_FAILED
    return _FETCH_FAILED
