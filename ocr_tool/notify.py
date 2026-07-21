"""系统通知封装（notify-send）。"""

from __future__ import annotations

import shutil
import subprocess


def notify(title: str, body: str = "", *, urgency: str = "normal") -> None:
    """发送桌面通知；notify-send 不可用时静默忽略。"""
    binary = shutil.which("notify-send")
    if not binary:
        return
    cmd = [
        binary,
        "--app-name=OCR工具",
        f"--urgency={urgency}",
        title,
    ]
    if body:
        cmd.append(body)
    subprocess.run(cmd, check=False, capture_output=True)
