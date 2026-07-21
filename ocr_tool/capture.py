"""区域截图：复用系统 Spectacle（KDE / Wayland）。"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ocr_tool.paths import CAPTURE_DIR, ensure_runtime_dir


class CaptureCancelled(Exception):
    """用户取消了区域选择。"""


class CaptureError(Exception):
    """截图失败（Spectacle 缺失或非取消错误）。"""


def capture_region(*, persist: bool = True) -> Path:
    """弹出区域选择，成功返回 PNG 路径。

    persist=True 时保存到运行时 captures/ 目录（供托盘「查看原图」）。
    取消框选时抛出 ``CaptureCancelled``；其它失败抛出 ``CaptureError``。
    """
    spectacle = shutil.which("spectacle")
    if not spectacle:
        raise CaptureError(
            "未找到 spectacle。请安装：sudo dnf install spectacle"
        )

    if persist:
        ensure_runtime_dir()
        out = CAPTURE_DIR / f"cap-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000}.png"
    else:
        tmp = tempfile.NamedTemporaryFile(
            prefix="ocr-tool-",
            suffix=".png",
            delete=False,
        )
        out = Path(tmp.name)
        tmp.close()

    # -r 区域 -b 后台不弹编辑窗 -n 不弹 Spectacle 自带通知 -o 输出文件
    cmd = [spectacle, "-r", "-b", "-n", "-o", str(out)]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        out.unlink(missing_ok=True)
        raise CaptureError(f"无法启动 spectacle: {exc}") from exc

    if result.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        # Spectacle 取消选区时常返回非 0 且无文件
        raise CaptureCancelled()

    return out
