"""Windows 平台能力：Win32 剪贴板、进程树终止、国内可达的下载源。

只在 Windows 分支由各模块按需导入，Linux 运行路径不依赖本模块。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from ctypes import wintypes

# ---------------------------------------------------------------- 剪贴板

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002

try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.CloseClipboard.restype = wintypes.BOOL
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalUnlock.restype = wintypes.BOOL
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
except (AttributeError, OSError):  # 非 Windows 上导入也不会炸
    _user32 = None  # type: ignore[assignment]
    _kernel32 = None  # type: ignore[assignment]


def win_copy_text(text: str) -> None:
    """用 Win32 API 把 UTF-16LE 文本写入系统剪贴板（不依赖 pywin32/tkinter）。"""
    if _user32 is None or _kernel32 is None:
        raise OSError("Win32 剪贴板不可用")
    data = text.encode("utf-16-le") + b"\x00\x00"
    opened = False
    for _ in range(20):
        if _user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.05)
    if not opened:
        raise OSError("OpenClipboard 超时（剪贴板被其它程序占用）")
    try:
        _user32.EmptyClipboard()
        handle = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        if not handle:
            raise OSError("GlobalAlloc 失败")
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            _kernel32.GlobalFree(handle)
            raise OSError("GlobalLock 失败")
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            _kernel32.GlobalUnlock(handle)
        if not _user32.SetClipboardData(_CF_UNICODETEXT, handle):
            _kernel32.GlobalFree(handle)
            raise OSError("SetClipboardData 失败")
        # 成功后句柄归系统所有，不能再 GlobalFree
    finally:
        _user32.CloseClipboard()


# ---------------------------------------------------------------- 子进程


def terminate_process_tree(proc: subprocess.Popen) -> None:
    """结束本应用拉起的子进程及其后代（llama-server 可能再拉起子进程）。"""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


# ---------------------------------------------------------------- 下载源

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def hf_base_url() -> str:
    """HuggingFace 下载根地址：默认镜像（国内可达），可用环境变量覆盖。"""
    return _env("OCR_TOOL_HF_ENDPOINT", _env("HF_ENDPOINT", "https://hf-mirror.com")).rstrip("/")


def resolve_github_url(url: str) -> str:
    """GitHub release 直链：套代理前缀（默认国内可达的 ghproxy）。"""
    proxy = _env("OCR_TOOL_GH_PROXY", "https://ghproxy.net/")
    if not proxy or proxy.upper() == "DIRECT":
        return url
    return proxy.rstrip("/") + "/" + url.lstrip("/")
