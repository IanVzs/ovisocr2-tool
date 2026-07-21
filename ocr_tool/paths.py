"""运行时与数据路径。

运行时(socket/pid/日志/截图)放 XDG_RUNTIME_DIR（易失）；模型与推理后端放
XDG_DATA_HOME 下的共享数据目录，dev(`make run`) 与打包安装版共用同一份，
避免重复下载 ~1.3GB 模型。可用 OCR_TOOL_DATA_DIR 覆盖数据目录。
"""

from __future__ import annotations

import os
from pathlib import Path

APP_ID = "ocr-tool"

RUNTIME_DIR = Path(
    os.environ.get("XDG_RUNTIME_DIR", f"/tmp/ocr-tool-{os.getuid()}")
)
SOCKET_PATH = RUNTIME_DIR / "ocr-tool.sock"
PID_PATH = RUNTIME_DIR / "ocr-tool.pid"
CAPTURE_DIR = RUNTIME_DIR / "captures"
LOG_PATH = RUNTIME_DIR / "ocr-tool.log"


def _data_home() -> Path:
    override = os.environ.get("OCR_TOOL_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_ID


# 共享数据目录：模型 + 预编译 llama-server（dev 与安装版共用）。
DATA_DIR = _data_home()
MODELS_DIR = DATA_DIR / "models"
LLAMA_BIN_DIR = DATA_DIR / "llama-bin"


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)


def ensure_data_dir() -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    LLAMA_BIN_DIR.mkdir(parents=True, exist_ok=True)
