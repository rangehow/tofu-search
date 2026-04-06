"""tofu_search.log — Standalone logging utilities.

Provides get_logger() for consistent logging across the package.
No dependency on chatui's lib/log.py.
"""

import logging
import sys

__all__ = ['get_logger']

# ── Package-level handler (console) ──
# Configured once at import time. Users can override by configuring the
# 'tofu_search' logger in their own logging setup.
_root_logger = logging.getLogger('tofu_search')

if not _root_logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-7s [%(name)s] %(message)s',
        datefmt='%H:%M:%S',
    ))
    _root_logger.addHandler(_handler)
    _root_logger.setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """Get a logger for the given module name.

    Args:
        name: Typically ``__name__`` from the calling module.

    Returns:
        A logging.Logger instance under the 'tofu_search' hierarchy.
    """
    return logging.getLogger(name)
