"""Windows 私有状态使用当前用户与 SYSTEM 的受保护 DACL。"""
import ctypes
from ctypes import wintypes
from pathlib import Path


def verify_private_path(path: Path) -> None:
    """已有密钥文件只接受当前用户和 SYSTEM 的授权 ACE。"""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID)]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.GetAce.argtypes = [wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)]
    security.GetAclInformation.argtypes = [wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.c_int]
    token, owner, dacl, descriptor = wintypes.HANDLE(), wintypes.LPVOID(), wintypes.LPVOID(), wintypes.LPVOID()

    def sid_text(sid):
        text = wintypes.LPWSTR()
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel.LocalFree(text)

    try:
        if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        length = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        buffer = ctypes.create_string_buffer(length.value)
        if not security.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        allowed = {sid_text(ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]), 'S-1-5-18'}
        error = security.GetNamedSecurityInfoW(str(path), 1, 0x1 | 0x4, ctypes.byref(owner), None,
                                               ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        if not owner or sid_text(owner) not in allowed or not dacl:
            raise PermissionError('模型设置文件需要当前用户的私有访问权限')
        sizes = (wintypes.DWORD * 3)()
        if not security.GetAclInformation(dacl, sizes, ctypes.sizeof(sizes), 2):
            raise ctypes.WinError(ctypes.get_last_error())
        for index in range(sizes[0]):
            ace = wintypes.LPVOID()
            if not security.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            kind = ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte))[0]
            if kind == 1:
                continue  # 拒绝 ACE 不会增加其他主体权限。
            if kind != 0 or sid_text(wintypes.LPVOID(ace.value + 8)) not in allowed:
                raise PermissionError('模型设置文件包含非私有授权，请先修正 Windows ACL')
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if token:
            kernel.CloseHandle(token)


def protect_private_path(path: Path) -> None:
    from agent_core.core.files import opened_path
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    token = wintypes.HANDLE()
    descriptor = wintypes.LPVOID()
    sid_text = wintypes.LPWSTR()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID]
    security.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID)]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.EqualSid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
    security.EqualSid.restype = wintypes.BOOL

    def owner_is_current(path: Path, current_sid: wintypes.LPVOID) -> bool:
        owner = wintypes.LPVOID()
        owner_descriptor = wintypes.LPVOID()
        try:
            error = security.GetNamedSecurityInfoW(str(path), 1, 0x1, ctypes.byref(owner), None,
                                                   None, None, ctypes.byref(owner_descriptor))
            if error:
                raise ctypes.WinError(error)
            if not owner or not owner_descriptor:
                raise PermissionError('无法确认私有路径的所有者')
            return bool(security.EqualSid(owner, current_sid))
        finally:
            if owner_descriptor:
                kernel.LocalFree(owner_descriptor)

    try:
        if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        length = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        buffer = ctypes.create_string_buffer(length.value)
        if not security.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        inheritable = 'OICI' if path.is_dir() else ''
        acl = f'O:{sid_text.value}D:P(A;{inheritable};FA;;;SY)(A;{inheritable};FA;;;{sid_text.value})'
        if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(acl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        with opened_path(path.parent, path, directory=path.is_dir()):
            # 现有所有者已是当前用户时，不额外请求 WRITE_OWNER。
            flags = 0x4 | 0x80000000
            if not owner_is_current(path, sid):
                flags |= 0x1
            if not security.SetFileSecurityW(str(path), flags, descriptor):
                raise ctypes.WinError(ctypes.get_last_error())
            if not owner_is_current(path, sid):
                raise PermissionError('无法确认私有路径的所有者')
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(sid_text)
        if token:
            kernel.CloseHandle(token)
