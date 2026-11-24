"""按模板整理本地环境配置，保留原始赋值且不输出秘密。"""
import argparse
from contextlib import ExitStack
from io import StringIO
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from dotenv.parser import parse_stream
from dotenv.variables import Variable, parse_variables
from agent_core.project_paths import project_root


LOCAL_SECTION = '# ---- 本地额外设置与注释 ----'
RECORD_NEWLINE = re.compile(r'\r\n|\r|\n')
LEADING_BLANK_LINES = re.compile(r'(?:[^\S\r\n]*(?:\r\n|\r|\n))*')


def _windows_path(path: Path, handles: ExitStack) -> Path:
    """消去父目录引用前，先验证并固定它跨过的目录。"""
    from agent_core.core.files import opened_path

    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        if part == '..':
            handles.enter_context(opened_path(current, current, directory=True))
            current = current.parent
        else:
            current /= part
    return current


def _read(path: Path, *, optional: bool = False) -> tuple[str | None, os.stat_result | None]:
    """同一描述符检查文件身份，拒绝链接及可能阻塞的 FIFO。"""
    if path.is_symlink():
        raise ValueError(f'{path.name} 不能是符号链接')
    with ExitStack() as handles:
        try:
            if os.name == 'nt':
                from agent_core.core.files import opened_path

                descriptor = handles.enter_context(opened_path(path.parent, path))
            else:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                                     | getattr(os, 'O_NONBLOCK', 0))
                handles.callback(os.close, descriptor)
        except FileNotFoundError:
            if optional:
                return None, None
            raise
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f'{path.name} 必须是普通文件')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            try:
                return stream.read().decode('utf-8'), file_stat
            except UnicodeError:
                raise ValueError(f'{path.name} 必须采用 UTF-8 编码') from None


def _parse(text: str, label: str):
    bindings = list(parse_stream(StringIO(text)))
    keys = {}
    for item in bindings:
        if item.error:
            raise ValueError(f'{label} 第 {item.original.line} 行语法无效')
        if item.key is None:
            continue
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', item.key):
            raise ValueError(f'{label} 第 {item.original.line} 行变量名无效')
        if item.key in keys:
            raise ValueError(f'{label} 重复配置键：{item.key}')
        keys[item.key] = item
    return bindings, keys


def _render(template_bindings, local_bindings, local_keys, newline):
    chunks = []
    known = {item.key for item in template_bindings if item.key is not None}
    template_comments = {line.strip() for item in template_bindings if item.key is None
                         for line in item.original.string.splitlines() if line.strip()}
    for item in template_bindings:
        original = item.original.string
        blank_end = LEADING_BLANK_LINES.match(original).end()
        prefix = RECORD_NEWLINE.sub(newline, original[:blank_end])
        body = original[blank_end:]
        if item.key in local_keys:
            local = local_keys[item.key].original.string
            body = local[LEADING_BLANK_LINES.match(local).end():]
        # 已匹配的空白记录包含行尾。只为实际赋值和注释补齐行尾。
        chunks.append(prefix + (body.rstrip('\r\n') + newline if body.strip() else body))
    # 保留独有注释及额外键的相对顺序；模板注释和自动分组标题不重复追加。
    extras = []
    for item in local_bindings:
        if item.key is not None:
            if item.key not in known:
                local = item.original.string
                extras.append(local[LEADING_BLANK_LINES.match(local).end():].rstrip('\r\n'))
        else:
            for line in item.original.string.splitlines():
                if line.strip() and line.strip() not in template_comments and line.strip() != LOCAL_SECTION:
                    extras.append(line)
    if extras:
        chunks.append(newline + LOCAL_SECTION + newline + newline.join(extras) + newline)
    return ''.join(chunks)


def _resolved(bindings, environment, *, override):
    """沿用 dotenv 的变量原子与优先级，评估环境快照而不修改进程环境。"""
    values = {}
    for item in bindings:
        if item.key is None:
            continue
        if item.value is None:
            values[item.key] = None
        else:
            scope = dict(environment) if override else dict(values)
            scope.update(values if override else environment)
            values[item.key] = ''.join(atom.resolve(scope) for atom in parse_variables(item.value))
    return values


def _check_values(before, after):
    """当前环境不能掩盖引用次序变化，也覆盖无变量及独立变量的环境。"""
    references = {atom.name for item in [*before, *after] if item.value is not None
                  for atom in parse_variables(item.value) if isinstance(atom, Variable)}
    marker = uuid.uuid4().hex
    environments = [dict(os.environ), {}, {name: f'{marker}_{name}' for name in references}]
    for environment in environments:
        for override in (False, True):
            original = _resolved(before, environment, override=override)
            proposed = _resolved(after, environment, override=override)
            changed = [key for key, value in original.items() if proposed.get(key) != value]
            if changed:
                raise ValueError('重排会改变已有变量展开结果，请先检查引用顺序：' + ', '.join(changed))


def _windows_is_private(path: Path) -> bool:
    """检查私有 ACE 且禁止继承，避免未来父目录授权改变私有性。"""
    import ctypes
    from ctypes import wintypes
    from agent_core.core.windows_security import verify_private_path

    try:
        verify_private_path(path)
    except PermissionError:
        return False
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID)]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.GetSecurityDescriptorControl.argtypes = [wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    descriptor = wintypes.LPVOID()
    error = security.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None,
                                           ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(control.value & 0x1000)
    finally:
        kernel.LocalFree(descriptor)


def _windows_private_temp(parent: Path) -> tuple[int, str]:
    """创建时即指定私有 DACL，避免继承权限与写入之间的窗口。"""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                               wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID]
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('descriptor', wintypes.LPVOID),
                    ('inherit', wintypes.BOOL)]

    token, descriptor, sid_text = wintypes.HANDLE(), wintypes.LPVOID(), wintypes.LPWSTR()
    handle = None
    path = None
    created = False
    try:
        if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        length = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        buffer = ctypes.create_string_buffer(length.value)
        if not security.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        sddl = f'O:{sid_text.value}D:P(A;;FA;;;SY)(A;;FA;;;{sid_text.value})'
        if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        path = str(parent / ('.env-sync-' + uuid.uuid4().hex))
        handle = kernel.CreateFileW(path, 0xC0000000, 0, ctypes.byref(attributes), 1, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        created = True
        fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        handle = None
        created = False
        return fd, path
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
        if descriptor:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(sid_text)
        if token:
            kernel.CloseHandle(token)
        if created:
            os.unlink(path)


def _write(target: Path, text: str, original: str | None):
    if os.name == 'nt':
        from agent_core.core.files import opened_path
        target = target.absolute()
        with opened_path(target.parent, target.parent, directory=True):
            _write_locked(target, text, original)
    else:
        _write_locked(target, text, original)


def _write_locked(target: Path, text: str, original: str | None):
    if os.name == 'nt':
        descriptor, temporary = _windows_private_temp(target.parent.absolute())
    else:
        descriptor, temporary = tempfile.mkstemp(prefix='.env-sync-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            if os.name != 'nt':
                os.chmod(temporary, 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # 发布前检查目标未变；不创建持有秘密的备份副本。
        current, _ = _read(target, optional=True)
        if current != original:
            raise ValueError('本地配置在同步期间发生变化，请重新运行')
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync_env(template: Path, target: Path, *, write: bool = False) -> dict:
    """检查/同步模板键与布局，返回键名和计数，不返回任何配置值。"""
    template, target = Path(template), Path(target)
    if os.name == 'nt':
        with ExitStack() as handles:
            template = _windows_path(template, handles)
            target = _windows_path(target, handles)
            return _sync_paths(template, target, write=write)
    if template.resolve() == target.resolve():
        raise ValueError('模板与本地文件必须不同')
    return _sync_paths(template, target, write=write)


def _sync_paths(template: Path, target: Path, *, write: bool) -> dict:
    if template == target:
        raise ValueError('模板与本地文件必须不同')
    reference, template_stat = _read(template)
    original, target_stat = _read(target, optional=True)
    if os.name == 'nt' and target_stat is not None and os.path.samestat(template_stat, target_stat):
        raise ValueError('模板与本地文件必须不同')
    template_bindings, template_keys = _parse(reference, '模板')
    if not template_keys:
        raise ValueError('模板不能为空')
    local_bindings, local_keys = _parse(original or '', '本地配置')
    # 只看解析后的记录结束符，不能把多行值内部的换行当作布局。
    newline = '\n'
    for item in local_bindings:
        record = item.original.string
        if record.endswith(('\r', '\n')):
            newline = '\r\n' if record.endswith('\r\n') else record[-1]
            break
    rendered = _render(template_bindings, local_bindings, local_keys, newline)
    proposed, _ = _parse(rendered, '同步结果')
    _check_values(local_bindings, proposed)
    report = {'changed': original != rendered,
              'added': [key for key in template_keys if key not in local_keys],
              'preserved': len(template_keys.keys() & local_keys.keys()),
              'local_only': [key for key in local_keys if key not in template_keys]}
    private = False
    if write and original is not None and not report['changed']:
        if os.name == 'nt':
            private = _windows_is_private(target)
        else:
            private = stat.S_IMODE(target.stat().st_mode) == 0o600
    if write and not private:
        _write(target, rendered, original)
    return report


def main():
    root = project_root(__file__)
    parser = argparse.ArgumentParser(description='同步 .env 的配置键与布局，保留本地值')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='只读检查（默认）')
    mode.add_argument('--write', action='store_true', help='原子同步本地文件')
    parser.add_argument('--template', type=Path, default=root / '.env.example')
    parser.add_argument('--target', type=Path, default=root / '.env')
    args = parser.parse_args()
    try:
        report = sync_env(args.template, args.target, write=args.write)
    except (OSError, ValueError) as exc:
        # 解析错误只含行号/键名；文件错误不包含任何读取出的行内容。
        print('配置同步失败：' + str(exc))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0 if args.write or not report['changed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
