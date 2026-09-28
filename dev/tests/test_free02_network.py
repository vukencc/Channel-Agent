import socket

import pytest

import config
from tools import sandbox
from tools.command import run_command


def test_network_request_is_explicit_and_disabled_by_default(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_NETWORK', 'off', raising=False)
    assert '拦截' in run_command('printf no', network=True)


def test_private_dns_and_unlisted_hosts_are_rejected_before_connect(monkeypatch):
    from tools.network_proxy import resolve_destination
    monkeypatch.setattr(config, 'COMMAND_NETWORK_ALLOWLIST', ['example.com'], raising=False)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(PermissionError):
        resolve_destination('example.com:443')
    with pytest.raises(PermissionError):
        resolve_destination('not-allowed.example:443')
    with pytest.raises(PermissionError):
        resolve_destination('example.com:80')


def test_trusted_rule_cannot_replace_per_network_confirmation(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_NETWORK', 'allowlist', raising=False)
    monkeypatch.setattr(config, 'COMMAND_NETWORK_ALLOWLIST', ['example.com'], raising=False)
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'trusted')
    monkeypatch.setattr(config, 'TOOL_PERMISSION_RULES', [
        {'tool': 'run_command', 'path_prefix': None, 'command_prefix': ['printf']}])
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: False)
    assert '取消' in run_command('printf network', network=True)


@pytest.mark.parametrize('address', ['224.0.0.1', 'ff0e::1', '2002:7f00:1::1'])
def test_non_unicast_and_transition_dns_addresses_are_not_public_destinations(monkeypatch, address):
    from tools.network_proxy import resolve_destination
    monkeypatch.setattr(config, 'COMMAND_NETWORK_ALLOWLIST', ['example.com'])
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (family, socket.SOCK_STREAM, 6, '', (address, 443))])
    with pytest.raises(PermissionError):
        resolve_destination('example.com:443')
