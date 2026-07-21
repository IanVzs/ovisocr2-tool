"""PyInstaller 入口。

用绝对 import 引用 ocr_tool —— PyInstaller 会把入口脚本当顶层 __main__ 运行，
包内相对 import 会失败。freeze_support 放最前，避免 spawn 子进程重跑入口。
"""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()

    from ocr_tool.__main__ import main

    raise SystemExit(main())
