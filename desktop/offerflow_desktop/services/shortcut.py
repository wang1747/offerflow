"""创建桌面快捷方式（ctypes 调 IShellLink COM 接口，无额外依赖）。"""

import ctypes
import os
import sys
import winreg
from typing import Tuple


def get_desktop_path() -> str:
    """获取当前用户桌面真实路径（含被重定向到非系统盘的桌面）。"""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        )
        path, _ = winreg.QueryValueEx(key, "Desktop")
        winreg.CloseKey(key)
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _create_shortcut(target: str, lnk_path: str, workdir: str = "",
                     icon: str = "", desc: str = "") -> bool:
    """通过 IShellLink COM 接口创建 .lnk 快捷方式。"""
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(0)

    GUID = ctypes.c_ubyte * 16
    CLSID_ShellLink = GUID(
        0x01, 0x14, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00,
        0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46)
    IID_IShellLinkW = GUID(
        0xF9, 0x14, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00,
        0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46)
    IID_IPersistFile = GUID(
        0x0B, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46)

    psl = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(CLSID_ShellLink), None, 1,  # CLSCTX_INPROC_SERVER
        ctypes.byref(IID_IShellLinkW), ctypes.byref(psl))
    if hr != 0:
        return False

    vtbl = ctypes.cast(psl, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    SetPath = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_wchar_p)(vtbl[20])
    SetWorkingDirectory = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_wchar_p)(vtbl[9])
    SetIconLocation = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)(vtbl[17])
    SetDescription = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_wchar_p)(vtbl[7])
    QueryInterface = ctypes.WINFUNCTYPE(
        ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(vtbl[0])
    Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])

    SetPath(psl, target)
    if workdir:
        SetWorkingDirectory(psl, workdir)
    if icon:
        SetIconLocation(psl, icon, 0)
    if desc:
        SetDescription(psl, desc)

    ppf = ctypes.c_void_p()
    hr = QueryInterface(psl, ctypes.byref(IID_IPersistFile), ctypes.byref(ppf))
    if hr != 0:
        Release(psl)
        return False

    pf_vtbl = ctypes.cast(ppf, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    Save = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)(pf_vtbl[6])
    hr = Save(ppf, lnk_path, 1)

    Release(ppf)
    Release(psl)
    ole32.CoUninitialize()
    return hr == 0


def create_desktop_shortcut() -> Tuple[bool, str]:
    """为当前应用创建桌面快捷方式，返回 (是否成功, 桌面路径或错误说明)。"""
    desktop = get_desktop_path()
    target = sys.executable
    lnk = os.path.join(desktop, "OfferFlow 求职工作台.lnk")
    workdir = os.path.dirname(target)
    try:
        ok = _create_shortcut(target, lnk, workdir=workdir,
                              icon=target, desc="OfferFlow 求职工作台")
    except Exception as exc:
        return False, str(exc)
    if ok and os.path.exists(lnk):
        return True, desktop
    return False, "创建失败"
