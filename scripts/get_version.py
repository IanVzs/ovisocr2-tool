"""打印 pyproject.toml 中的版本号（Makefile 用，跨平台免 awk）。"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
if not m:
    raise SystemExit("pyproject.toml 中未找到 version 字段")
print(m.group(1))
