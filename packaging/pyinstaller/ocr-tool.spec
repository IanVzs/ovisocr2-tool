# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec：把 OCR工具（PySide6 托盘 + HTTP 客户端）打成 onedir 独立目录。
# 模型与 llama-server 不打进包（在共享数据目录 ~/.local/share/ocr-tool/，首次用
# `ocr-tool --setup-backend` 下载），所以包体只含 Python 应用与 PySide6。
#
# 用法（项目根目录执行）：
#   pyinstaller --noconfirm --clean packaging/pyinstaller/ocr-tool.spec
# 产物：dist/ocr-tool/

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

PROJECT_ROOT = Path(SPECPATH).resolve().parent.parent
APP_NAME = "ocr-tool"

a = Analysis(
    [str(Path(SPECPATH) / "launcher.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        # 托盘图标：tray_app.make_tray_icon 从 packaging/ 读取；缺失会退回绘制。
        (str(PROJECT_ROOT / "packaging" / "ocr-tool-icon-256.png"), "packaging"),
        (str(PROJECT_ROOT / "packaging" / "ocr-tool-icon.png"), "packaging"),
    ],
    hiddenimports=[*collect_submodules("ocr_tool")],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
