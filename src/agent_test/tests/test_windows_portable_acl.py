"""真实 Windows ACL 回归；只操作 tmp_path，不属于模拟 frozen 或实际 exe 验收。"""
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import json
import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='需要实际 Windows token、所有者和 NTFS ACL')


class NativeAcl:
    """通过实际进程 token 和安全描述符设置受控 ACL，不查询或输出用户名。"""

    def __init__(self):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.security = ctypes.WinDLL('advapi32', use_last_error=True)
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.GetCurrentThread.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.LocalFree.argtypes = [wintypes.LPVOID]
        self.security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self.security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.security.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL,
                                                 ctypes.POINTER(wintypes.HANDLE)]
        self.security.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                                  ctypes.c_int, ctypes.c_int, ctypes.POINTER(wintypes.HANDLE)]
        self.security.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        self.security.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL, wintypes.LPVOID,
                                                       wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
        self.security.SetThreadToken.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.HANDLE]
        self.security.RevertToSelf.argtypes = []
        self.security.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
        self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID]
        self.security.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
        self.security.SetFileSecurityW.restype = wintypes.BOOL
        self.security.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID)]
        self.security.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self.current_sid = self._current_sid()

    def _sid_text(self, sid):
        text = wintypes.LPWSTR()
        if not self.security.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            self.kernel.LocalFree(text)

    def _current_sid(self):
        token = wintypes.HANDLE()
        if not self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            length = wintypes.DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
            buffer = ctypes.create_string_buffer(length.value)
            if not self.security.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
                raise ctypes.WinError(ctypes.get_last_error())
            return self._sid_text(ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0])
        finally:
            self.kernel.CloseHandle(token)

    def owner_is_current(self, path: Path) -> bool:
        owner, descriptor = wintypes.LPVOID(), wintypes.LPVOID()
        error = self.security.GetNamedSecurityInfoW(str(path), 1, 0x1, ctypes.byref(owner),
                                                   None, None, None, ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        try:
            return self._sid_text(owner) == self.current_sid
        finally:
            self.kernel.LocalFree(descriptor)

    @contextmanager
    def current_user_default_owner(self):
        """只在本线程使用复制 token，消除管理员默认 owner 为组的差异。"""
        original, process, duplicate = wintypes.HANDLE(), wintypes.HANDLE(), wintypes.HANDLE()
        active = False
        try:
            # 保存已有线程身份；无 impersonation token 时最后恢复进程身份。
            if not self.security.OpenThreadToken(self.kernel.GetCurrentThread(), 0x4 | 0x8, True,
                                                 ctypes.byref(original)):
                if ctypes.get_last_error() != 1008:  # ERROR_NO_TOKEN
                    raise ctypes.WinError(ctypes.get_last_error())
            if not self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x2 | 0x8,
                                                  ctypes.byref(process)):
                raise ctypes.WinError(ctypes.get_last_error())
            # SecurityImpersonation + TokenImpersonation；原进程 token 始终只读。
            if not self.security.DuplicateTokenEx(process, 0x4 | 0x8 | 0x20 | 0x80, None, 2, 2,
                                                  ctypes.byref(duplicate)):
                raise ctypes.WinError(ctypes.get_last_error())
            length = wintypes.DWORD()
            self.security.GetTokenInformation(duplicate, 1, None, 0, ctypes.byref(length))
            buffer = ctypes.create_string_buffer(length.value)
            if not self.security.GetTokenInformation(duplicate, 1, buffer, length, ctypes.byref(length)):
                raise ctypes.WinError(ctypes.get_last_error())
            # TOKEN_OWNER 只有一个 SID 指针；token user 所在缓冲区保持到调用结束。
            owner = wintypes.LPVOID(ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0])
            if not self.security.SetTokenInformation(duplicate, 4, ctypes.byref(owner), ctypes.sizeof(owner)):
                raise ctypes.WinError(ctypes.get_last_error())
            # 管理员 runner 的复制 token 也不得凭 Restore/TakeOwnership 绕过权限前提。
            ctypes.set_last_error(0)
            if not self.security.AdjustTokenPrivileges(duplicate, True, None, 0, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.security.SetThreadToken(None, duplicate):
                raise ctypes.WinError(ctypes.get_last_error())
            active = True
            yield
        finally:
            try:
                if active:
                    restored = (self.security.SetThreadToken(None, original) if original
                                else self.security.RevertToSelf())
                    if not restored:
                        raise ctypes.WinError(ctypes.get_last_error())
            finally:
                for handle in (duplicate, process, original):
                    if handle:
                        self.kernel.CloseHandle(handle)

    def try_apply(self, path: Path, sddl: str, flags: int) -> tuple[bool, int]:
        descriptor = wintypes.LPVOID()
        if not self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.set_last_error(0)
            applied = bool(self.security.SetFileSecurityW(str(path), flags, descriptor))
            return applied, ctypes.get_last_error()
        finally:
            self.kernel.LocalFree(descriptor)

    def apply(self, path: Path, sddl: str, flags: int = 0x4 | 0x80000000) -> None:
        applied, error = self.try_apply(path, sddl, flags)
        if not applied:
            raise ctypes.WinError(error)

    def assert_owner_rewrite_is_denied(self, path: Path) -> None:
        assert self.owner_is_current(path), '受控新文件应已由当前用户拥有'
        applied, error = self.try_apply(path, f'O:{self.current_sid}', 0x1)
        assert not applied, '受控 Modify 夹具不能获得 WRITE_OWNER'
        assert error == 5, '重写所有者必须因缺少 WRITE_OWNER 而拒绝'


@pytest.fixture
def modify_parent(tmp_path):
    """新父只给当前用户可继承 Modify；SYSTEM FullControl，不继承外部授权。"""
    acl = NativeAcl()
    parent = tmp_path / 'modify-only-parent'
    with acl.current_user_default_owner():
        parent.mkdir()
        try:
            acl.apply(parent, f'D:P(A;OICI;FA;;;SY)(A;OICI;0x1301bf;;;{acl.current_sid})')
            assert acl.owner_is_current(parent)
            yield parent, acl
        finally:
            # 先恢复夹具父权限，再恢复线程 token，失败红测也必须能回收。
            acl.apply(parent, f'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{acl.current_sid})')


def test_owned_modify_file_can_be_protected_without_write_owner(modify_parent):
    from agent_core.core.windows_security import protect_private_path, verify_private_path
    parent, acl = modify_parent
    source = parent / 'new-private-file'
    source.write_bytes(b'controlled-private-content')
    acl.assert_owner_rewrite_is_denied(source)
    # 独立对照文件证明相同继承环境允许仅设置 DACL；不能先给被测源提权。
    control = parent / 'dacl-only-control'
    control.write_bytes(b'controlled-comparison')
    acl.apply(control, f'D:P(A;;FA;;;SY)(A;;FA;;;{acl.current_sid})')
    verify_private_path(control)
    protect_private_path(source)
    verify_private_path(source)
    assert acl.owner_is_current(source)
    assert source.read_bytes() == b'controlled-private-content'


def test_portable_first_start_and_atomic_report_work_below_modify_parent(modify_parent):
    from agent_core.core.windows_security import verify_private_path
    from agent_frontend.desktop.launcher import _write_info
    from agent_frontend.desktop.portable import initialize_layout
    parent, acl = modify_parent
    root = parent / 'portable-app'
    root.mkdir()
    inherited = root / 'inherited-acl-probe'
    inherited.write_bytes(b'controlled-inheritance-proof')
    acl.assert_owner_rewrite_is_denied(inherited)
    layout = initialize_layout(root)
    for relative in ('config', 'data', 'data/state', 'data/workspaces', 'data/projects/default',
                     'data/raw', 'cache/rag', 'cache/huggingface/hub', 'cache/huggingface/modules',
                     'cache/sentence-transformers', 'cache/fastembed', 'cache/torch', 'cache/tmp', 'logs'):
        path = root / relative
        assert path.is_dir(), relative
        verify_private_path(path)
        assert acl.owner_is_current(path)
    verify_private_path(layout.env_file)
    assert acl.owner_is_current(layout.env_file)
    report = root / 'unique-portable-info.json'
    payload = {'root': str(layout.root), 'paths': {'env_file': str(layout.env_file)}}
    _write_info(report, payload)
    assert json.loads(report.read_text(encoding='utf-8')) == payload
    verify_private_path(report)
    assert acl.owner_is_current(report)
    assert inherited.read_bytes() == b'controlled-inheritance-proof'
    assert not list(root.glob('.portable-env-*'))
    assert not list(root.glob('.portable-info-*'))


def test_existing_foreign_env_acl_is_rejected_without_repair(modify_parent):
    from agent_core.core.windows_security import verify_private_path
    from agent_frontend.desktop.portable import PortableError, initialize_layout
    parent, acl = modify_parent
    root = parent / 'foreign-env-app'
    root.mkdir()
    env = root / '.env'
    content = b'PRIVATE_EXISTING=preserve-exactly\n'
    env.write_bytes(content)
    acl.apply(env, f'D:P(A;;FA;;;SY)(A;;FA;;;{acl.current_sid})(A;;GR;;;AU)')
    with pytest.raises(PermissionError):
        verify_private_path(env)
    with pytest.raises(PortableError) as error:
        initialize_layout(root)
    assert error.value.kind == 'unsafe'
    assert error.value.path == env
    assert env.read_bytes() == content
    assert acl.owner_is_current(env)
    with pytest.raises(PermissionError):
        verify_private_path(env)
