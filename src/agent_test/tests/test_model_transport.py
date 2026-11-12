import pytest
import asyncio
import os
import shutil
import ssl
import subprocess
import threading

from agent_core.core import llm
from agent_core.core import model_transport


def test_system_proxy_selected_and_environment_ignored(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setattr(model_transport, "_windows_settings", lambda: (True, "http=proxy.example:8080;https=secure.example:8081", ""))
    assert model_transport.select_system_proxy("https://api.example/v1") == "http://secure.example:8081"


def test_system_exclusions_and_no_proxy(monkeypatch):
    monkeypatch.setattr(model_transport, "_windows_settings", lambda: (True, "proxy.example:8080", "*.internal;<local>"))
    assert model_transport.select_system_proxy("https://service.internal") is None
    assert model_transport.select_system_proxy("https://intranet") is None
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "example.com:8443")
    assert model_transport.select_system_proxy("https://sub.example.com:8443") is None
    assert model_transport.select_system_proxy("https://sub.example.com:443") == "http://proxy.example:8080"


def test_unavailable_proxy_is_safe(monkeypatch):
    monkeypatch.setattr(model_transport, "_windows_settings", lambda: (False, "secret", ""))
    with pytest.raises(model_transport.ModelProxyError) as exc:
        model_transport.select_system_proxy("https://api.example")
    assert "secret" not in str(exc.value)


def test_runtime_system_does_not_validate_environment(monkeypatch):
    monkeypatch.setattr(llm.config, "validate_proxy_environment", lambda: pytest.fail("environment proxy consulted"))
    monkeypatch.setattr(model_transport, "_windows_settings", lambda: (True, "proxy.example:8080", ""))
    client = llm.endpoint_client({"base_url": "https://api.example", "api_key": "key", "proxy_mode": "windows_system"})
    asyncio.run(client.close())
    assert client.is_closed()


def test_missing_system_proxy_and_bad_proxy_are_safe(monkeypatch):
    for settings in ((True, '', ''), (True, 'http://user:secret@bad:invalid', '')):
        monkeypatch.setattr(model_transport, '_windows_settings', lambda: settings)
        with pytest.raises(model_transport.ModelProxyError) as exc:
            model_transport.select_system_proxy('https://api.example')
        assert 'secret' not in str(exc.value)
    if os.name != 'nt':
        monkeypatch.undo()
        with pytest.raises(model_transport.ModelProxyError):
            model_transport.select_system_proxy('https://api.example')


@pytest.mark.parametrize('entry,url', [
    ('*', 'https://api.example'),
    ('192.0.2.1', 'https://192.0.2.1'),
    ('::1', 'https://[::1]'),
    ('.example.com', 'https://sub.example.com'),
])
def test_no_proxy_httpx_semantics(monkeypatch, entry, url):
    monkeypatch.setattr(model_transport, '_windows_settings', lambda: (True, 'proxy.example:8080', ''))
    monkeypatch.delenv('no_proxy', raising=False)
    monkeypatch.setenv('NO_PROXY', entry)
    assert model_transport.select_system_proxy(url) is None


def test_ipv6_cidr_no_proxy_does_not_break_other_hosts(monkeypatch):
    monkeypatch.setattr(model_transport, '_windows_settings', lambda: (True, 'proxy.example:8080', ''))
    monkeypatch.delenv('no_proxy', raising=False)
    monkeypatch.setenv('NO_PROXY', '::1/128')
    assert model_transport.select_system_proxy('https://[::1]') is None
    assert model_transport.select_system_proxy('https://api.example') == 'http://proxy.example:8080'


@pytest.mark.skipif(shutil.which('openssl') is None, reason='需要 openssl 生成受控测试证书')
@pytest.mark.parametrize('trust_setting', ['SSL_CERT_FILE', 'SSL_CERT_DIR'])
def test_system_transport_tls_respects_environment_ca_and_rejects_untrusted(tmp_path, monkeypatch, trust_setting):
    key = tmp_path / 'key.pem'
    cert = tmp_path / 'cert.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if trust_setting == 'SSL_CERT_DIR':
        cert_dir = tmp_path / 'ca'
        cert_dir.mkdir()
        digest = subprocess.check_output(['openssl', 'x509', '-hash', '-noout', '-in', str(cert)], text=True).strip()
        shutil.copyfile(cert, cert_dir / f'{digest}.0')
    monkeypatch.setattr(model_transport, '_windows_settings', lambda: (True, 'proxy.example:8080', '<local>'))
    monkeypatch.setenv('ALL_PROXY', 'http://127.0.0.1:1')

    async def exercise():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)

        async def serve(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(serve, '127.0.0.1', 0, ssl=context)
        port = server.sockets[0].getsockname()[1]
        url = f'https://localhost:{port}/'
        try:
            monkeypatch.delenv('SSL_CERT_FILE', raising=False)
            monkeypatch.delenv('SSL_CERT_DIR', raising=False)
            monkeypatch.setenv(trust_setting, str(cert if trust_setting == 'SSL_CERT_FILE' else cert_dir))
            async with model_transport.system_http_client(url) as client:
                assert (await client.get(url)).text == 'ok'
            assert client.is_closed
            monkeypatch.delenv('SSL_CERT_FILE', raising=False)
            monkeypatch.delenv('SSL_CERT_DIR', raising=False)
            async with model_transport.system_http_client(url) as client:
                with pytest.raises(model_transport.httpx.ConnectError):
                    await client.get(url)
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(exercise())


def test_system_transport_reaches_selected_proxy_despite_bad_environment(monkeypatch):
    monkeypatch.setenv('ALL_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY', '')
    monkeypatch.setenv('no_proxy', '')

    async def exercise():
        received = []

        async def serve(reader, writer):
            received.append(await reader.readuntil(b'\r\n\r\n'))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(model_transport, '_windows_settings', lambda: (True, f'127.0.0.1:{port}', ''))
        try:
            async with model_transport.system_http_client('http://example.test') as client:
                assert (await client.get('http://example.test/path')).text == 'ok'
            assert received[0].startswith(b'GET http://example.test/path ')
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(exercise())


def test_mixed_system_protocols_and_invalid_selected_proxy(monkeypatch):
    monkeypatch.setenv('NO_PROXY', '')
    monkeypatch.setenv('no_proxy', '')
    monkeypatch.setattr(model_transport, '_windows_settings', lambda: (
        True, 'http=web.example:8080;https=secure.example:8081;ftp=ftp.example:21;socks=socks.example:1080', ''))
    assert model_transport.select_system_proxy('https://api.example') == 'http://secure.example:8081'
    for server in ('http=proxy.example:0', 'http=proxy.example:65536',
                   'http=proxy.example:8080;http=other:8080', 'http=proxy.example:8080;unknown=host:8'):
        monkeypatch.setattr(model_transport, '_windows_settings', lambda: (True, server, ''))
        with pytest.raises(model_transport.ModelProxyError):
            model_transport.select_system_proxy('http://api.example')


def test_close_drains_all_transports_after_failure():
    closed = []

    class Transport:
        def __init__(self, name):
            self.name = name

        async def aclose(self):
            closed.append(self.name)
            if self.name == 'first':
                raise RuntimeError('controlled failure')

    transport = object.__new__(model_transport._SystemTransport)
    transport._transports = {'first': Transport('first'), 'second': Transport('second')}
    with pytest.raises(RuntimeError):
        asyncio.run(transport.aclose())
    assert closed == ['first', 'second']


def test_environment_mode_does_not_fallback_to_system(monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
    monkeypatch.setenv('ALL_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY', '')
    monkeypatch.setenv('no_proxy', '')
    monkeypatch.setattr(model_transport, '_windows_settings', lambda: pytest.fail('系统代理不应被读取'))

    async def exercise():
        client = llm.endpoint_client({'base_url': 'http://example.test/v1', 'api_key': 'key'})
        try:
            with pytest.raises(model_transport.httpx.ConnectError):
                await client._client.get('http://example.test/v1')
        finally:
            await client.close()
    asyncio.run(exercise())


def test_cancel_during_system_client_creation_closes_client(monkeypatch):
    monkeypatch.setattr(model_transport, '_windows_settings', lambda: (True, 'proxy.example:8080', ''))
    created = threading.Event()
    release = threading.Event()
    clients = []
    original = model_transport.system_http_client

    def delayed(base_url):
        client = original(base_url)
        clients.append(client)
        created.set()
        assert release.wait(5)
        return client

    monkeypatch.setattr(model_transport, 'system_http_client', delayed)

    async def exercise():
        task = asyncio.create_task(llm._runtime_client({'base_url': 'https://example.test', 'api_key': 'key',
                                                       'proxy_mode': 'windows_system'}))
        assert await asyncio.to_thread(created.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert clients[0].is_closed
    asyncio.run(exercise())
