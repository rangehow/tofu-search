"""Live stdio smoke test: does the MCP server actually start and answer?

★ Drives the server as a real SUBPROCESS over stdio, not via in-process calls.

That is the whole point. An in-process test can confirm the tools are
registered but cannot catch the failure that actually breaks a stdio plugin:
something -- a library, an import side effect, a stray print -- writing to
stdout and corrupting the JSON-RPC stream. Only a subprocess whose stdout we
read byte-for-byte can prove the channel is clean.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytest.importorskip('mcp', reason='MCP SDK not installed (pip install "tofu-search[mcp]")')

PROTOCOL_VERSION = '2025-06-18'


def _frame(payload: dict) -> str:
    return json.dumps(payload) + '\n'


def _handshake_script(*requests: dict) -> str:
    """initialize -> initialized -> the given requests."""
    script = _frame({
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {
            'protocolVersion': PROTOCOL_VERSION,
            'capabilities': {},
            'clientInfo': {'name': 'smoke-test', 'version': '0'},
        },
    })
    script += _frame({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    for req in requests:
        script += _frame(req)
    return script


def _run_server(*requests: dict):
    """Run one real server subprocess; return (responses_by_id, raw_stdout).

    ★ Each call gets its OWN subprocess, and the request list is kept short.
    The server exits as soon as stdin closes, so batching many requests into a
    single run races the shutdown: the last one can be dropped before it is
    answered. Splitting the checks across runs removes the race instead of
    papering over it with a sleep.
    """
    proc = subprocess.run(
        [sys.executable, '-m', 'tofu_search.mcp_server', '--transport', 'stdio'],
        input=_handshake_script(*requests),
        capture_output=True,
        text=True,
        timeout=120,
    )

    responses = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)          # raises if stdout carries non-protocol noise
        if 'id' in msg:
            responses[msg['id']] = msg
    return responses, proc.stdout


@pytest.fixture(scope='module')
def handshake():
    """initialize + tools/list against a live server subprocess."""
    return _run_server({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})


@pytest.fixture(scope='module')
def resources():
    """initialize + resources/list against a live server subprocess."""
    return _run_server({'jsonrpc': '2.0', 'id': 3, 'method': 'resources/list'})


def test_stdout_carries_only_json_rpc(handshake):
    """Every line on stdout must parse as JSON-RPC.

    The parse happens in the fixture; this test states the contract explicitly
    so a failure reads as "stdout was polluted" rather than a JSONDecodeError
    from an unrelated test.
    """
    responses, raw = handshake
    assert responses, f'server produced no JSON-RPC responses; stdout was: {raw[:400]!r}'
    for line in raw.splitlines():
        if line.strip():
            assert line.lstrip().startswith('{'), (
                f'non-protocol output on stdout would corrupt the stream: {line[:200]!r}')


def test_initialize_returns_server_info(handshake):
    responses, _ = handshake
    result = responses[1]['result']
    assert result['protocolVersion']
    assert result['serverInfo']['name'] == 'tofu-search'


def test_all_four_tools_are_advertised(handshake):
    responses, _ = handshake
    names = {t['name'] for t in responses[2]['result']['tools']}
    assert names == {'web_search', 'fetch_page', 'search_vertical',
                     'verify_citations'}, f'unexpected tool surface: {sorted(names)}'


def test_no_registration_or_config_tool_is_exposed(handshake):
    """configure() and the register_* seams must never reach a model.

    configure() mutates process-wide state -- one client could change another
    client's search behaviour. The register_* seams take Python objects a model
    cannot construct.
    """
    responses, _ = handshake
    names = {t['name'] for t in responses[2]['result']['tools']}
    for forbidden in ('configure', 'get_config', 'register_browser_provider',
                      'register_auth_source_provider', 'register_reader'):
        assert forbidden not in names, f'{forbidden} must not be exposed as a tool'


def test_every_tool_has_a_substantial_description(handshake):
    """A tool description IS the model's usage contract, not documentation."""
    responses, _ = handshake
    for tool in responses[2]['result']['tools']:
        desc = tool.get('description') or ''
        assert len(desc) > 200, (
            f'{tool["name"]} has a {len(desc)}-char description; too thin to '
            'steer tool selection')


def test_vertical_description_is_generated_from_the_live_registry(handshake):
    """search_vertical's description must list domains the registry reports now.

    Pinned because the alternative -- a hand-written domain list -- went stale
    the moment a new vertical was added, which happened during development.
    """
    from tofu_search.search.vertical import list_domains

    responses, _ = handshake
    desc = next(t['description'] for t in responses[2]['result']['tools']
                if t['name'] == 'search_vertical')
    for domain in list_domains():
        assert f'`{domain}`' in desc, (
            f'domain {domain!r} is registered but missing from the generated '
            'description -- it is being hand-maintained again')


def test_health_is_a_resource_not_a_tool(handshake, resources):
    """Operational telemetry should not compete with real actions."""
    tool_responses, _ = handshake
    tool_names = {t['name'] for t in tool_responses[2]['result']['tools']}
    assert 'get_search_health' not in tool_names

    resource_responses, _ = resources
    uris = {str(r['uri']) for r in resource_responses[3]['result']['resources']}
    assert 'health://status' in uris, f'health resource missing: {sorted(uris)}'


def test_multi_worker_start_is_refused():
    """The single-process guard must fail loudly, not warn.

    Per-process throttle and circuit-breaker state means N workers multiply the
    real request rate to every engine by N.
    """
    proc = subprocess.run(
        [sys.executable, '-m', 'tofu_search.mcp_server', '--workers', '2'],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0, 'server started with --workers=2'
    assert 'refuses to start' in proc.stderr, proc.stderr[:400]
