"""Shared pytest fixtures for the tofu-search test suite.

All tests here are offline: nothing hits the network. Engine/HTTP paths are
exercised via monkeypatching, and the pure helpers (dedup, rerank, vertical
intent detection, URL guards) run directly.
"""

import pytest

import tofu_search.config as _config


@pytest.fixture(autouse=True)
def _reset_global_config():
    """Snapshot and restore the global SearchConfig around every test.

    configure() mutates a process-global singleton; without this an early
    test could leak settings into a later one.
    """
    saved = _config._global_config
    _config._global_config = _config.SearchConfig()
    try:
        yield
    finally:
        _config._global_config = saved
