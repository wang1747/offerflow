"""登录态的安全存储（审计 P2-5）。

Windows：用 DPAPI（CryptProtectData / CryptUnprotectData，绑定当前用户账号）
加密，token 不以明文落盘。非 Windows（开发 / 跨平台打包缺失时）：回退为明文 +
一次性告警——DPAPI 是 Windows 专属，其他平台没有等价的内核级绑定加密。

对外只暴露 protect/unprotect（对称、同平台内可逆），api_client 把整个
{token, user} 负载加密成单个 base64 字段存储。
"""

from __future__ import annotations

import base64
import logging
import sys

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform.startswith("win")


def is_encryption_available() -> bool:
    """当前平台是否提供真正的（DPAPI）加密。False 表示回退明文。"""
    return _IS_WINDOWS


if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]

    def _protect_windows(data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, size=len(data))
        in_blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
        out_blob = _DATA_BLOB()
        if not _crypt32.CryptProtectData(
                ctypes.byref(in_blob), None, None, None, None, 0,
                ctypes.byref(out_blob)):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            _kernel32.LocalFree(out_blob.pbData)

    def _unprotect_windows(blob: bytes) -> bytes:
        buf = ctypes.create_string_buffer(blob, size=len(blob))
        in_blob = _DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
        out_blob = _DATA_BLOB()
        if not _crypt32.CryptUnprotectData(
                ctypes.byref(in_blob), None, None, None, None, 0,
                ctypes.byref(out_blob)):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            _kernel32.LocalFree(out_blob.pbData)


_warn_once = {"done": False}


def protect(plain: str) -> str:
    """把明文字符串加密为可安全落盘的 base64 字符串。"""
    raw = plain.encode("utf-8")
    if _IS_WINDOWS:
        blob = _protect_windows(raw)
    else:
        if not _warn_once["done"]:
            logger.warning(
                "secure_storage: 当前平台非 Windows，DPAPI 不可用，登录态将以明文"
                "存储（audit P2-5 回退路径）。生产安装包运行在 Windows 上才会真正加密。"
            )
            _warn_once["done"] = True
        blob = raw  # 回退：base64 包装的明文
    return base64.b64encode(blob).decode("ascii")


def unprotect(b64: str) -> str:
    """protect 的逆运算；解析失败抛异常（调用方需自行决定清登录态）。"""
    blob = base64.b64decode(b64)
    if _IS_WINDOWS:
        raw = _unprotect_windows(blob)
    else:
        raw = blob  # 回退：base64 解码即为明文
    return raw.decode("utf-8")
