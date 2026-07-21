#!/usr/bin/env python3
"""下载 OCR 后端的薄包装：实际逻辑在包内 ``ocr_tool.backend_setup``。

保留此脚本是为兼容 `make install-llama` 的旧调用；打包安装版可用
`ocr-tool --setup-backend` 达到同样效果。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_tool.backend_setup import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
