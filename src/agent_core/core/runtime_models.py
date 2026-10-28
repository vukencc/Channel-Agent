"""Private endpoint registry; session records contain only generated identifiers."""
from copy import deepcopy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat
import threading
from types import MappingProxyType
from typing import Mapping
from collections.abc import Iterable
from urllib.parse import urlsplit
import uuid

from agent_core import config
from agent_core.core.model_settings import ModelParameters, ModelProfile

_UNSET = object()


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class RuntimeModelSnapshot:
    """A request owns this immutable snapshot until completion."""
    id: str
    name: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    parameters: Mapping = field(default_factory=lambda: MappingProxyType({}))
    context_tokens: int | None = None
    response_reserve_tokens: int | None = None

    def endpoint(self) -> dict:
        return {'model': self.model, 'base_url': self.base_url, 'api_key': self.api_key}

    def options(self) -> dict:
        return _thaw(self.parameters)


def _validate_fields(*, name, model, base_url, api_key, parameters=None,
                     context_tokens=None, response_reserve_tokens=None) -> dict:
    for value, label, maximum in ((name, '名称', 120), (model, '模型 ID', 200)):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError(f'模型{label}无效')
    if not isinstance(api_key, str) or len(api_key) > 16384 or any(ord(c) < 32 or ord(c) == 127 for c in api_key):
        raise ValueError('API 密钥格式无效')
    try:
        if not isinstance(base_url, str) or len(base_url) > 2048 or any(c.isspace() for c in base_url):
            raise ValueError
        parts = urlsplit(base_url)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError
        if parts.query or parts.fragment or '?' in base_url or '#' in base_url or '\\' in base_url:
            raise ValueError
        config.validate_http_url(base_url, '模型服务地址')
    except (ValueError, TypeError):
        raise ValueError('模型服务地址必须是 HTTP(S) 地址，不能包含凭据、查询或片段') from None
    try:
        options = ModelParameters.model_validate({} if parameters is None else parameters).model_dump(exclude_none=True)
    except (ValueError, TypeError):
        raise ValueError('模型参数无效') from None
    try:
        ModelProfile(context_tokens=context_tokens, response_reserve_tokens=response_reserve_tokens)
        reserve = max(config.MODEL_RESPONSE_RESERVE_TOKENS if response_reserve_tokens is None else response_reserve_tokens,
                      options.get('max_tokens') or 0)
        if context_tokens is not None and reserve >= context_tokens:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('模型上下文容量必须大于输出预留/max_tokens') from None
    return {'name': name.strip(), 'model': model.strip(), 'base_url': base_url.rstrip('/'),
            'api_key': api_key, 'parameters': options, 'context_tokens': context_tokens,
            'response_reserve_tokens': response_reserve_tokens}


class RuntimeModelRegistry:
    """Serialize atomic private writes, reject linked/unsafe state files."""
    filename = '.runtime-models.json'
    maximum_bytes = 1048576

    def __init__(self, state_root: Path, *, forbidden_roots: Iterable[Path] = ()):
        self.root = Path(state_root).absolute()
        self.path = self.root / self.filename
        self._lock = threading.RLock()
        self._forbidden_roots = tuple(Path(root).resolve() for root in forbidden_roots)
        self._check_root()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._check_root()
        metadata = self.root.stat()
        self._root_identity = (metadata.st_dev, metadata.st_ino)
        self._entries = self._load()

    def _check_root(self):
        for path in (self.root, *self.root.parents):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ValueError('模型设置目录不能是链接或文件')

    def _check_file(self):
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError('模型设置文件必须是独立普通文件')
        if os.name == 'posix' and (stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_uid != os.getuid()):
            raise ValueError('模型设置文件需要私有访问权限')
        return metadata

    def _check_private_location(self):
        if any(self.path.is_relative_to(root) for root in self._forbidden_roots):
            raise ValueError('模型密钥文件不能位于工具可读取的工作区内；请选择独立状态目录')

    def _directory_fd(self):
        self._check_root()
        if os.name == 'posix':
            descriptor = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for component in self.root.parts[1:]:
                    next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = next_fd
                metadata = os.fstat(descriptor)
                if (metadata.st_dev, metadata.st_ino) != self._root_identity:
                    raise ValueError('模型设置目录已被替换')
                return descriptor
            except BaseException:
                os.close(descriptor)
                raise
        metadata = self.root.stat()
        if (metadata.st_dev, metadata.st_ino) != self._root_identity:
            raise ValueError('模型设置目录已被替换')
        return None

    def _load(self):
        descriptor = self._directory_fd()
        file_fd = None
        try:
            if self._check_file() is None:
                return {}
            self._check_private_location()
            file_fd = os.open(self.filename if descriptor is not None else self.path,
                              os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0),
                              **({'dir_fd': descriptor} if descriptor is not None else {}))
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > self.maximum_bytes:
                raise ValueError('模型设置文件无效')
            if os.name == 'posix' and (stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_uid != os.getuid()):
                raise ValueError('模型设置文件需要私有访问权限')
            with os.fdopen(file_fd, 'r', encoding='utf-8') as handle:
                file_fd = None
                data = json.load(handle)
            if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('models'), dict):
                raise ValueError('模型设置文件无效')
            entries = {}
            for identifier, entry in data['models'].items():
                if not self._valid_id(identifier) or not isinstance(entry, dict):
                    raise ValueError('模型设置文件无效')
                entries[identifier] = _validate_fields(**entry)
            return entries
        except (OSError, ValueError, TypeError, UnicodeError):
            raise ValueError('模型设置文件无法安全读取') from None
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _valid_id(identifier):
        return isinstance(identifier, str) and identifier.startswith('runtime-') and len(identifier) == 40 and all(c in '0123456789abcdef' for c in identifier[8:])

    def _save(self, entries):
        self._check_private_location()
        content = json.dumps({'version': 1, 'models': entries}, ensure_ascii=False).encode('utf-8')
        if len(content) > self.maximum_bytes:
            raise ValueError('模型设置已达到容量上限')
        descriptor = self._directory_fd()
        temporary = '.runtime-models-' + uuid.uuid4().hex + '.tmp'
        temporary_path = temporary if descriptor is not None else self.root / temporary
        file_fd = None
        created = False
        try:
            self._check_file()
            file_fd = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600,
                              **({'dir_fd': descriptor} if descriptor is not None else {}))
            created = True
            with os.fdopen(file_fd, 'wb') as handle:
                file_fd = None
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._check_root()
            self._check_file()
            if descriptor is not None:
                os.replace(temporary, self.filename, src_dir_fd=descriptor, dst_dir_fd=descriptor)
                os.fsync(descriptor)
            else:
                os.replace(temporary_path, self.path)
            created = False
        except OSError:
            raise ValueError('模型设置文件无法安全保存') from None
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if created:
                os.unlink(temporary_path, **({'dir_fd': descriptor} if descriptor is not None else {}))
            if descriptor is not None:
                os.close(descriptor)

    def ids(self) -> list[str]:
        with self._lock:
            return list(config.MODEL_PROFILES) + list(self._entries)

    def contains(self, identifier: str | None) -> bool:
        with self._lock:
            return identifier is None or identifier in config.MODEL_PROFILES or identifier in self._entries

    def _public(self, identifier, entry):
        return {'id': identifier, 'name': entry['name'], 'model': entry['model'],
                'base_url': entry['base_url'], 'has_api_key': bool(entry['api_key']),
                'parameters': deepcopy(entry.get('parameters', {})), 'read_only': False,
                'context_tokens': entry.get('context_tokens'),
                'response_reserve_tokens': entry.get('response_reserve_tokens')}

    def list_public(self) -> list[dict]:
        with self._lock:
            rows = [{'id': name, 'name': name, 'model': profile.get('model') or config.MODEL,
                     'base_url': self._public_default_url(), 'has_api_key': bool(config.API_KEY),
                     'parameters': deepcopy(profile.get('parameters', {})), 'read_only': True,
                     'context_tokens': profile.get('context_tokens'),
                     'response_reserve_tokens': profile.get('response_reserve_tokens')}
                    for name, profile in config.MODEL_PROFILES.items()]
            return rows + [self._public(identifier, entry) for identifier, entry in self._entries.items()]

    @staticmethod
    def _public_default_url():
        # Existing environment URLs may contain secrets: expose only sanitized metadata.
        try:
            parts = urlsplit(config.BASE_URL or '')
            if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
                return ''
            return config.BASE_URL or ''
        except ValueError:
            return ''

    def resolve(self, identifier: str | None) -> RuntimeModelSnapshot | None:
        with self._lock:
            if identifier is None or identifier in config.MODEL_PROFILES:
                return None
            entry = self._entries.get(identifier)
            if entry is None:
                raise ValueError('模型设置不存在，请重新选择模型')
            return RuntimeModelSnapshot(id=identifier, name=entry['name'], model=entry['model'],
                                        base_url=entry['base_url'], api_key=entry['api_key'],
                                        parameters=_freeze(deepcopy(entry['parameters'])),
                                        context_tokens=entry.get('context_tokens'),
                                        response_reserve_tokens=entry.get('response_reserve_tokens'))

    def add(self, *, name: str, model: str, base_url: str, api_key: str = '', parameters: dict | None = None,
            context_tokens: int | None = None, response_reserve_tokens: int | None = None) -> dict:
        entry = _validate_fields(name=name, model=model, base_url=base_url, api_key=api_key, parameters=parameters,
                                 context_tokens=context_tokens, response_reserve_tokens=response_reserve_tokens)
        with self._lock:
            identifier = 'runtime-' + uuid.uuid4().hex
            entries = {**self._entries, identifier: entry}
            self._save(entries)
            self._entries = entries
            return self._public(identifier, entry)

    def _editable(self, identifier):
        if identifier in config.MODEL_PROFILES:
            raise PermissionError('环境配置的模型预设为只读')
        if identifier not in self._entries:
            raise FileNotFoundError('模型设置不存在')

    def update(self, identifier: str, *, name: str, model: str, base_url: str,
               api_key: str = '', parameters: dict | None = None,
               context_tokens=_UNSET, response_reserve_tokens=_UNSET) -> dict:
        with self._lock:
            self._editable(identifier)
            previous = self._entries[identifier]
            entry = _validate_fields(name=name, model=model, base_url=base_url,
                                     api_key=previous['api_key'] if api_key == '' else api_key,
                                     parameters=previous['parameters'] if parameters is None else parameters,
                                     context_tokens=previous.get('context_tokens') if context_tokens is _UNSET else context_tokens,
                                     response_reserve_tokens=previous.get('response_reserve_tokens') if response_reserve_tokens is _UNSET else response_reserve_tokens)
            if api_key == '' and previous['api_key'] and entry['base_url'] != previous['base_url']:
                raise ValueError('修改服务地址时必须明确填写该服务的 API 密钥')
            entries = {**self._entries, identifier: entry}
            self._save(entries)
            self._entries = entries
            return self._public(identifier, entry)

    def delete(self, identifier: str) -> None:
        with self._lock:
            self._editable(identifier)
            entries = {key: value for key, value in self._entries.items() if key != identifier}
            self._save(entries)
            self._entries = entries
