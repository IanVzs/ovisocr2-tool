"""应用版本号解析。唯一权威源是仓库根目录的 ``pyproject.toml``。"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path


def app_version() -> str:
    """当前应用版本。查询顺序：

    1. 冻结版本文件 ``ocr_tool/_version.py``（打包产物无 pyproject/dist-info 时用）
    2. importlib 元数据 ``metadata.version("ocr-tool")``
    3. 仓库根目录 ``pyproject.toml``
    4. 都失败则返回 ``"unknown"``
    """
    try:
        from ocr_tool._version import __version__

        return __version__
    except Exception:  # noqa: BLE001
        pass
    try:
        return metadata.version("ocr-tool")
    except metadata.PackageNotFoundError:
        pass
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            if line.startswith("version"):
                return line.split('"')[1]
    except (OSError, IndexError):
        pass
    return "unknown"
