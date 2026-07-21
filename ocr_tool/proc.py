"""子进程环境处理：在 PyInstaller 冻结包里拉起系统程序时还原干净环境。

冻结后 bootloader 会把 ``LD_LIBRARY_PATH`` / ``QT_*`` 等指向包内自带库，让本应用
找到打包的 PySide6/Qt。但用 subprocess 拉起**系统程序**（spectacle / wl-copy /
notify-send / llama-server）时，它们会继承这些变量，去加载我们打包的库版本而
崩溃（例如系统 Spectacle 是 Qt 6.7，包里是 Qt 6.11，ABI/插件不匹配直接退出，
表现为「刚点截图就自动取消」）。

PyInstaller 覆盖这些变量前会把原值存到 ``<VAR>_ORIG``。这里据此还原：有 _ORIG
就恢复系统原值，没有就删掉注入值——等价于系统本来的环境。
"""

from __future__ import annotations

import os
import sys

# PyInstaller / Qt 运行时可能注入或覆盖、会影响系统程序加载的变量。
_OVERRIDDEN = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
    "QT_QPA_FONTDIR",
    "QML2_IMPORT_PATH",
    "QML_IMPORT_PATH",
    "GST_PLUGIN_PATH",
    "GST_PLUGIN_SYSTEM_PATH",
    "GI_TYPELIB_PATH",
    "GDK_PIXBUF_MODULE_FILE",
    "FONTCONFIG_PATH",
    "FONTCONFIG_FILE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


def system_env() -> dict[str, str]:
    """返回适合拉起系统程序的环境副本。

    非冻结（源码运行）时原样返回当前环境；冻结时还原被 PyInstaller 覆盖的变量。
    """
    env = os.environ.copy()
    if not getattr(sys, "frozen", False):
        return env
    for key in _OVERRIDDEN:
        orig = env.pop(key + "_ORIG", None)
        if orig:
            env[key] = orig
        else:
            env.pop(key, None)
    return env
