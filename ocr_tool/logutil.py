"""终端与文件日志。"""

from __future__ import annotations

import logging
import sys

from ocr_tool.paths import LOG_PATH, ensure_runtime_dir

_LOG = logging.getLogger("ocrtool")
_CONFIGURED = False


def setup_logging(*, to_stdout: bool | None = None) -> logging.Logger:
    """配置根 logger。to_stdout 默认：终端交互则开，否则只写文件。"""
    global _CONFIGURED
    ensure_runtime_dir()
    if to_stdout is None:
        to_stdout = sys.stdout.isatty()

    if _CONFIGURED:
        return _LOG

    _LOG.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    _LOG.addHandler(fh)

    if to_stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        _LOG.addHandler(sh)

    _CONFIGURED = True
    _LOG.debug("日志文件: %s", LOG_PATH)
    return _LOG


def get_logger() -> logging.Logger:
    if not _CONFIGURED:
        return setup_logging()
    return _LOG
