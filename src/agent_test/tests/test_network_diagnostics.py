"""保留私网 DNS 拒绝，并将真正原因提供给环境准备工具。"""
import socket

from agent_core import config
from agent_core.tools.network_proxy import NetworkProxy


def test_fake_ip_denial_is_not_reported_as_package_registry_failure(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_NETWORK', 'allowlist')
    monkeypatch.setattr(config, 'COMMAND_NETWORK_ALLOWLIST', ['registry.npmjs.org'])
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('198.18.0.11', 443))])
    with NetworkProxy() as proxy:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(proxy.path))
            client.sendall(b'CONNECT registry.npmjs.org:443 HTTP/1.1\r\n\r\n')
            assert b'403 Forbidden' in client.recv(1024)
        summary = proxy.denial_summary()
    assert '非公网' in summary and 'DNS' in summary
