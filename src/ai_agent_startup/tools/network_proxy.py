"""隔离命令的有界 CONNECT 网关；只允许明确授权的公网域名及 443 端口。"""
from contextvars import copy_context
import ipaddress
from pathlib import Path
import re
import select
import socket
import tempfile
import threading
import time

from ai_agent_startup import config
from ai_agent_startup.tools.sandbox import audit

# DNS 阻塞线程不能强制终止；跨命令限制总数并在解析完成后再次检查关闭状态。
_dns_slots = threading.BoundedSemaphore(16)


def validate_network_config() -> None:
    if config.COMMAND_NETWORK not in {'off', 'allowlist'}:
        raise ValueError('COMMAND_NETWORK 必须为 off 或 allowlist')
    names = config.COMMAND_NETWORK_ALLOWLIST
    if not isinstance(names, list) or any(
        not isinstance(name, str) or len(name) > 253
        or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', name)
        or '.' not in name or '..' in name for name in names
    ):
        raise ValueError('COMMAND_NETWORK_ALLOWLIST 必须为小写 ASCII 精确域名列表（国际域名用 punycode）')
    if config.COMMAND_NETWORK == 'allowlist' and not names:
        raise ValueError('联网模式必须提供非空域名白名单')


def resolve_destination(authority: str) -> tuple[str, list[tuple]]:
    if not re.fullmatch(r'[a-zA-Z0-9.-]+:443', authority):
        raise PermissionError('只支持精确域名:443')
    host = authority[:-4].lower()
    if host not in config.COMMAND_NETWORK_ALLOWLIST:
        raise PermissionError('域名不在白名单内')
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    def public_unicast(address):
        value = ipaddress.ip_address(address)
        if not value.is_global or value.is_multicast or value.is_reserved or value.is_unspecified:
            return False
        # IPv6 过渡地址可能嵌入不同作用域的 IPv4，保守拒绝隧道转换。
        if isinstance(value, ipaddress.IPv6Address) and (value.sixtofour is not None or value.teredo is not None):
            return False
        return True
    if not addresses or any(not public_unicast(row[4][0]) for row in addresses):
        raise PermissionError('拒绝非公网单播或 IPv6 过渡 DNS 结果')
    return host, addresses


class NetworkProxy:
    """一次命令一个代理，连接数、累计双向字节及连接时间均有上限。"""
    def __init__(self):
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.sockets: set[socket.socket] = set()
        self.threads: list[threading.Thread] = []
        self.transferred = 0
        self.connections = 0
        self.context = copy_context()

    def __enter__(self):
        validate_network_config()
        self.directory = tempfile.TemporaryDirectory(prefix='agent-proxy-')
        self.path = Path(self.directory.name) / 'proxy.sock'
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(str(self.path))
            self.path.chmod(0o600)
            self.listener.listen(config.COMMAND_NETWORK_MAX_CONNECTIONS)
            self.listener.settimeout(.1)
            self.acceptor = threading.Thread(target=self._accept, daemon=True)
            self.acceptor.start()
            return self
        except BaseException:
            self.listener.close()
            self.directory.cleanup()
            raise

    def _accept(self):
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                allowed = self.connections < config.COMMAND_NETWORK_MAX_CONNECTIONS and _dns_slots.acquire(False)
                if allowed:
                    self.connections += 1
                    self.sockets.add(client)
            if not allowed:
                client.close()
                continue
            thread = threading.Thread(target=self.context.copy().run, args=(self._serve, client), daemon=True)
            self.threads.append(thread)
            thread.start()

    def _serve(self, client):
        upstream = None
        host = ''
        connected = False
        try:
            client.settimeout(config.COMMAND_NETWORK_TIMEOUT)
            header = bytearray()
            while not header.endswith(b'\r\n\r\n'):
                data = client.recv(1)
                if not data or len(header) >= 8192:
                    raise PermissionError('无效代理请求')
                header.extend(data)
            words = header.split(b'\r\n', 1)[0].decode('ascii').split()
            if len(words) != 3 or words[0] != 'CONNECT' or words[2] not in {'HTTP/1.0', 'HTTP/1.1'}:
                raise PermissionError('只支持 HTTPS CONNECT')
            host, addresses = resolve_destination(words[1])
            if self.stop.is_set():
                return
            # 使用本次已验证数值地址，避免连接阶段重新解析造成 DNS rebinding。
            family, kind, protocol, _, address = addresses[0]
            upstream = socket.socket(family, kind, protocol)
            upstream.settimeout(config.COMMAND_NETWORK_TIMEOUT)
            with self.lock:
                if self.stop.is_set():
                    return
                self.sockets.add(upstream)
            upstream.connect(address)
            client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            connected = True
            deadline = time.monotonic() + config.COMMAND_NETWORK_TIMEOUT
            audit('network_connect', host=host, port=443, peer=address[0])
            while not self.stop.is_set() and time.monotonic() < deadline:
                readable, _, _ = select.select([client, upstream], [], [], .1)
                for source in readable:
                    data = source.recv(16384)
                    if not data:
                        return
                    with self.lock:
                        if self.transferred + len(data) > config.COMMAND_NETWORK_MAX_BYTES:
                            raise PermissionError('命令网络累计字节超限')
                        self.transferred += len(data)
                    (upstream if source is client else client).sendall(data)
        except (OSError, ValueError, UnicodeError) as exc:
            audit('network_closed', host=host, error=type(exc).__name__, connected=connected)
            if not connected:
                try:
                    client.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                except OSError:
                    pass
        finally:
            with self.lock:
                for stream in (client, upstream):
                    if stream is not None:
                        self.sockets.discard(stream)
                        stream.close()
            _dns_slots.release()

    def __exit__(self, *_):
        self.stop.set()
        self.listener.close()
        self.acceptor.join(.5)
        with self.lock:
            for stream in self.sockets:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                stream.close()
        self.directory.cleanup()
        audit('network_finished', bytes=self.transferred, connections=self.connections)
