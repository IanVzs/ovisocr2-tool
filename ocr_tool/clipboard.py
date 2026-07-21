"""写入系统剪贴板（Wayland 优先 wl-copy）。"""

from __future__ import annotations

import shutil
import subprocess


class ClipboardError(Exception):
    """剪贴板写入失败。"""


def _run_clip(cmd: list[str], data: bytes, name: str) -> None:
    """喂数据给剪贴板守护进程。

    关键：wl-copy / xclip 都会 fork 成常驻守护进程来持续提供剪贴板内容，
    该守护进程会继承 stdout/stderr。若用管道捕获(capture_output)，管道 EOF
    永远等不到 → subprocess 死锁 → 调用方(UI 线程)卡死。因此这里把
    stdout/stderr 接到 DEVNULL，并加超时兜底，保证快速返回、绝不阻塞。
    """
    try:
        result = subprocess.run(
            cmd,
            input=data,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClipboardError(f"{name} 超时未返回") from exc
    except OSError as exc:
        raise ClipboardError(f"{name} 启动失败: {exc}") from exc
    if result.returncode != 0:
        raise ClipboardError(f"{name} 失败(code={result.returncode})")


def copy_text(text: str) -> None:
    """把纯文本写入系统剪贴板。"""
    data = text.encode("utf-8")
    wl_copy = shutil.which("wl-copy")
    if wl_copy:
        _run_clip([wl_copy], data, "wl-copy")
        return

    xclip = shutil.which("xclip")
    if xclip:
        _run_clip([xclip, "-selection", "clipboard"], data, "xclip")
        return

    raise ClipboardError(
        "未找到 wl-copy 或 xclip。请安装：sudo dnf install wl-clipboard"
    )
