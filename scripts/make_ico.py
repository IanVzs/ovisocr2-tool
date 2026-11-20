"""从 packaging/ocr-tool-icon-256.png 生成 Windows .ico（PyInstaller 打包 exe 图标用）。

仅在打包时需要（make build-win 会自动调用）；已存在且比源图新则跳过。
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "packaging" / "ocr-tool-icon-256.png"
DST = ROOT / "packaging" / "ocr-tool-icon.ico"
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    if DST.is_file() and DST.stat().st_mtime >= SRC.stat().st_mtime:
        print(f"已存在且为最新: {DST}")
        return 0
    img = Image.open(SRC).convert("RGBA")
    img.save(DST, format="ICO", sizes=SIZES)
    print(f"已生成 {DST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
