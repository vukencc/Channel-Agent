"""运行时模型的显式 Windows 手动代理；不修改进程环境。"""
import fnmatch
from importlib import import_module
import os
from urllib.request import getproxies_environment

from openai import _base_client
from openai._constants import DEFAULT_CONNECTION_LIMITS

httpx = getattr(_base_client, 'httpx2', None) or _base_client.httpx
_utils = import_module(f'{httpx.__name__}._utils')
URLPattern = _utils.URLPattern
is_ipv4_hostname = _utils.is_ipv4_hostname
is_ipv6_hostname = _utils.is_ipv6_hostname


class ModelProxyError(ValueError):
    """系统代理不可用或配置无效，不包含代理详情。"""


_PROXY_ERROR = 'Windows 系统手动代理不可用或配置无效，请检查系统代理设置。'


def _windows_settings() -> tuple[bool, str, str]:
    if os.name != 'nt':
        raise ModelProxyError(_PROXY_ERROR)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            def read(name, default):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    return default
            return bool(read('ProxyEnable', 0)), read('ProxyServer', ''), read('ProxyOverride', '')
    except (OSError, ValueError, TypeError):
        raise ModelProxyError(_PROXY_ERROR) from None


def _no_proxy_matches(url: httpx.URL) -> bool:
    for entry in getproxies_environment().get('no', '').split(','):
        host = entry.strip()
        if not host:
            continue
        if host == '*':
            return True
        if '://' in host:
            pattern = host
        elif is_ipv4_hostname(host):
            pattern = f'all://{host}'
        elif is_ipv6_hostname(host):
            if '/' in host:
                address, _, subnet = host.partition('/')
                pattern = f'all://[{address}]/{subnet}'
            else:
                pattern = f'all://[{host}]'
        elif host.lower() == 'localhost':
            pattern = f'all://{host}'
        else:
            pattern = f'all://*{host}'
        if URLPattern(pattern).matches(url):
            return True
    return False


def select_system_proxy(base_url: str) -> str | None:
    """固定代理存在且有效时返回选中地址；排除规则返回 None。"""
    try:
        enabled, server, exclusions = _windows_settings()
        if not enabled or not isinstance(server, str) or not server.strip() or not isinstance(exclusions, str):
            raise ValueError
        choices = {}
        for part in server.split(';'):
            part = part.strip()
            if not part:
                continue
            if '=' in part:
                scheme, value = part.split('=', 1)
                scheme = scheme.strip().lower()
                if scheme not in {'http', 'https', 'ftp', 'socks'} or scheme in choices:
                    raise ValueError
                choices[scheme] = value.strip()
            else:
                if 'all' in choices:
                    raise ValueError
                choices['all'] = part
        url = httpx.URL(base_url)
        selected = choices.get(url.scheme, choices.get('all'))
        if not selected:
            raise ValueError
        proxy = selected if '://' in selected else 'http://' + selected
        parsed = httpx.URL(proxy)
        if (parsed.scheme not in {'http', 'https'} or not parsed.host or
                parsed.port is not None and not 1 <= parsed.port <= 65535 or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError
        host = url.host.lower()
        authority = f'[{host}]' if ':' in host else host
        if url.port is not None:
            authority += f':{url.port}'
        for item in exclusions.split(';'):
            item = item.strip().lower()
            if item == '<local>' and '.' not in host and ':' not in host:
                return None
            if item and item != '<local>' and (fnmatch.fnmatchcase(host, item) or
                    fnmatch.fnmatchcase(authority, item) or
                    fnmatch.fnmatchcase(f'{url.scheme}://{authority}', item)):
                return None
        if _no_proxy_matches(url):
            return None
        return proxy
    except ModelProxyError:
        raise
    except Exception:
        raise ModelProxyError(_PROXY_ERROR) from None


class _SystemTransport(httpx.AsyncBaseTransport):
    """每次请求按目标重新匹配，重定向也遵守协议和排除规则。"""

    def __init__(self, base_url: str):
        self._transports: dict[str | None, httpx.AsyncHTTPTransport] = {}
        self._transport_for(base_url)

    def _transport_for(self, url: str) -> httpx.AsyncHTTPTransport:
        try:
            proxy = select_system_proxy(url)
            if proxy not in self._transports:
                self._transports[proxy] = httpx.AsyncHTTPTransport(
                    proxy=proxy, trust_env=True, limits=DEFAULT_CONNECTION_LIMITS)
            return self._transports[proxy]
        except ModelProxyError:
            raise
        except Exception:
            raise ModelProxyError(_PROXY_ERROR) from None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transport_for(str(request.url)).handle_async_request(request)

    async def aclose(self) -> None:
        error = None
        for transport in self._transports.values():
            try:
                await transport.aclose()
            except Exception as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error


def system_http_client(base_url: str) -> httpx.AsyncClient:
    try:
        transport = _SystemTransport(base_url)
        from openai import DefaultAsyncHttpxClient
        return DefaultAsyncHttpxClient(transport=transport, trust_env=True)
    except ModelProxyError:
        raise
    except Exception:
        raise ModelProxyError(_PROXY_ERROR) from None
