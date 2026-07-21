"""用户配置持久化。

存到共享数据目录下的 ``config.json``（dev 与安装版共用）。目前仅存全局快捷键，
后续新增配置项直接往里加即可。读写都做容错，坏文件不影响启动。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ocr_tool.paths import DATA_DIR

log = logging.getLogger("ocrtool")

CONFIG_PATH = DATA_DIR / "config.json"

# 首次运行的默认全局快捷键（Meta=Super/Win 键）。空串表示禁用。
DEFAULT_HOTKEY = "Meta+Shift+O"

_DEFAULTS: dict[str, Any] = {
    "hotkey": DEFAULT_HOTKEY,
}


def load_config() -> dict[str, Any]:
    """读取配置，缺失 / 损坏时回退默认值。"""
    cfg = dict(_DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cfg.update(raw)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("配置读取失败，用默认值: %s", exc)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    """原子写回配置。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(CONFIG_PATH)
    except OSError as exc:
        log.warning("配置写入失败: %s", exc)
