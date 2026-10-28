"""Local gateway quotas explain package-manager errors without exposing request data."""
import json
import socket
import threading
import time

import pytest

from agent_core import config
from agent_core.tools.network_proxy import NetworkProxy
from agent_core.tools import network_proxy


@pytest.mark.parametrize('kwargs', [{'max_bytes': 0}, {'max_bytes': float('inf')},
                                  {'max_connections': -1}, {'max_connections': True},
                                  {'timeout': 0}, {'timeout': float('nan')}])
def test_proxy_rejects_invalid_override(kwargs):
    with pytest.raises(ValueError):
        NetworkProxy(**kwargs)


def test_lifetime_connection_rejection_is_visible_and_audited(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_NETWORK', 'allowlist')
    monkeypatch.setattr(config, 'COMMAND_NETWORK_ALLOWLIST', ['registry.npmjs.org'])
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('198.18.0.11', 443))])
    with NetworkProxy(max_connections=1, max_bytes=4096, timeout=1) as proxy:
        for _ in range(2):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(proxy.path))
                client.sendall(b'CONNECT registry.npmjs.org:443 HTTP/1.1\r\nAuthorization: secret\r\n\r\n')
                try:
                    client.recv(1024)
                except ConnectionResetError:
                    pass
        deadline = time.monotonic() + 2
        while '连接' not in proxy.denial_summary() and time.monotonic() < deadline:
            time.sleep(.01)
        assert '连接' in proxy.denial_summary()
        assert proxy.connections == 1
    records = [json.loads(line) for line in config.AUDIT_LOG.read_text().splitlines()]
    record = next(row for row in records if row.get('denial_code') == 'connection_limit')
    assert record['max_connections'] == record['connections'] == 1
    assert record['max_bytes'] == 4096 and record['timeout_seconds'] == 1
    assert 'secret' not in json.dumps(records)


def test_proxy_defaults_are_not_changed_by_one_explicit_profile():
    custom = NetworkProxy(max_bytes=128 * 1024 * 1024, max_connections=32, timeout=30)
    generic = NetworkProxy()
    assert custom.max_bytes == 128 * 1024 * 1024
    assert generic.max_bytes == config.COMMAND_NETWORK_MAX_BYTES
    assert generic.max_connections == config.COMMAND_NETWORK_MAX_CONNECTIONS
    assert generic.timeout == config.COMMAND_NETWORK_TIMEOUT


@pytest.mark.parametrize('send_bytes,denial_code', [(9, 'byte_limit'), (0, 'connection_timeout')])
def test_tunnel_byte_and_absolute_timeout_denials_have_fixed_evidence(
        sandbox_env, monkeypatch, send_bytes, denial_code):
    client, accepted = socket.socketpair()
    upstream, remote = socket.socketpair()
    client.settimeout(2)
    connected_to = []

    class PinnedSocket:
        def __getattr__(self, name):
            return getattr(upstream, name)
        def connect(self, address):
            connected_to.append(address)

    monkeypatch.setattr(network_proxy, 'resolve_destination', lambda authority: (
        'registry.npmjs.org', [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                               '', ('93.184.216.34', 443))]))
    monkeypatch.setattr(socket, 'socket', lambda *args, **kw: PinnedSocket())
    proxy = NetworkProxy(max_bytes=8, max_connections=1, timeout=.05)
    proxy.connections = 1
    # Directly exercise the worker with an accepted socket and its matching DNS slot.
    assert network_proxy._dns_slots.acquire(False)
    worker = threading.Thread(target=proxy._serve, args=(accepted,))
    worker.start()
    try:
        client.sendall(b'CONNECT registry.npmjs.org:443 HTTP/1.1\r\nAuthorization: secret\r\n\r\n')
        assert b'200 Connection Established' in client.recv(1024)
        if send_bytes:
            remote.sendall(b'x' * send_bytes)
        assert client.recv(1024) == b''
        worker.join(2)
        assert not worker.is_alive()
    finally:
        proxy.stop.set()
        client.close()
        remote.close()
        worker.join(2)
    assert connected_to == [('93.184.216.34', 443)]
    records = [json.loads(line) for line in config.AUDIT_LOG.read_text().splitlines()]
    denial = next(row for row in records if row.get('denial_code') == denial_code)
    assert denial['bytes'] == 0 and denial['max_bytes'] == 8
    assert denial['connections'] == denial['max_connections'] == 1
    assert denial['timeout_seconds'] == .05
    assert 'secret' not in json.dumps(records)


def test_background_job_persists_gateway_diagnostics(sandbox_env, monkeypatch, tmp_path):
    from agent_core.core.command_jobs import CommandJobs
    from agent_core.core.storage import SessionStore
    from agent_core.tools import command, sandbox

    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', True)
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'full_access')
    monkeypatch.setattr(config, 'COMMAND_NETWORK', 'allowlist')
    monkeypatch.setattr(config, 'COMMAND_NETWORK_ALLOWLIST', ['registry.npmjs.org'])
    store = SessionStore(tmp_path / 'state')
    jobs = CommandJobs(store)
    context = sandbox.ToolContext(root=sandbox_env, audit_path=tmp_path / 'jobs-audit.jsonl',
                                  job_manager=jobs, session_id='test-network-quota',
                                  confirm=lambda *args: True, cancelled=threading.Event())

    class DeniedProxy:
        path = tmp_path / 'proxy.sock'
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def denial_summary(self):
            return '命令网络累计字节超限（累计字节 8/8）'

    monkeypatch.setattr(network_proxy, 'NetworkProxy', DeniedProxy)
    monkeypatch.setattr(command, '_execute', lambda *args, **kw: '[退出码] 1\nERESOLVE')
    try:
        with sandbox.tool_context(context):
            identifier = jobs.start('printf test', network=True, timeout=1)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            record = jobs.status(context.session_id, identifier)
            if record['status'] not in {'queued', 'running'}:
                break
            time.sleep(.01)
        assert record['status'] == 'failed'
        assert 'ERESOLVE' in record['result'] and '网络边界诊断' in record['result']
        persisted = json.loads((jobs.directory / (identifier + '.json')).read_text())
        assert persisted['result'] == record['result']
    finally:
        jobs.close()
