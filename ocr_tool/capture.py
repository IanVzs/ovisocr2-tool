"""区域截图：复用系统 Spectacle（KDE / Wayland）。"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ocr_tool.paths import CAPTURE_DIR, ensure_runtime_dir
from ocr_tool.proc import system_env

log = logging.getLogger("ocrtool")


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
            env=system_env(),  # 冻结包里必须还原干净环境，否则系统 spectacle 加载我们打包的 Qt 崩溃
        )
    except OSError as exc:
        out.unlink(missing_ok=True)
        raise CaptureError(f"无法启动 spectacle: {exc}") from exc

    if out.is_file() and out.stat().st_size > 0:
        return out

    out.unlink(missing_ok=True)
    stderr = (result.stderr or "").strip()
    # spectacle 崩溃（如库不匹配）会打印错误到 stderr；此时是真错误，别当成用户取消。
    crashed = result.returncode not in (0, 1) or (
        stderr and any(k in stderr.lower() for k in ("error", "cannot", "symbol", "undefined", "version", "abort"))
    )
    if crashed:
        log.warning("spectacle 异常退出 code=%s stderr=%s", result.returncode, stderr[:300])
        raise CaptureError(f"截图组件异常退出(code={result.returncode})：{stderr[:200] or '无输出'}")
    # 正常取消：返回非 0 且无文件、无错误输出
    raise CaptureCancelled()
