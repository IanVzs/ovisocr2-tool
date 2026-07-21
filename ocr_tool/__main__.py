"""CLI 入口：托盘单例 + 区域截图 OCR。"""

from __future__ import annotations

import argparse
import sys
import time

from ocr_tool.ipc import (
    is_app_alive,
    request_capture,
    request_quit,
    request_status,
    spawn_detached_app,
)
from ocr_tool.logutil import setup_logging
from ocr_tool.version import app_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ocr-tool",
        description="区域截图 → HunyuanOCR 解析（系统托盘常驻）",
    )
    parser.add_argument("--version", action="store_true", help="显示版本号")
    parser.add_argument("--quit", action="store_true", help="退出托盘应用")
    parser.add_argument("--status", action="store_true", help="查看任务状态")
    parser.add_argument(
        "--setup-backend",
        action="store_true",
        help="下载 llama-server + 模型到共享数据目录（打包版首次使用）",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="前台运行托盘（通常由应用自动拉起）",
    )
    args = parser.parse_args(argv)

    log = setup_logging(to_stdout=args.foreground or sys.stdout.isatty())

    if args.version:
        print(app_version())
        return 0

    if args.setup_backend:
        from ocr_tool.backend_setup import main as setup_main

        return setup_main()

    if args.quit:
        was = request_quit()
        print("托盘已退出" if was else "托盘未在运行")
        return 0

    if args.status:
        _print_status(request_status())
        return 0

    if args.foreground:
        return _run_tray(capture_on_start=False)

    if is_app_alive():
        return _submit_to_running(log)

    return _run_tray(capture_on_start=True)


def _run_tray(*, capture_on_start: bool) -> int:
    from ocr_tool.tray_app import run_tray_app

    return run_tray_app(capture_on_start=capture_on_start)


def _submit_to_running(log) -> int:
    resp = request_capture()
    if not resp.get("ok"):
        print(str(resp.get("error") or "提交截图失败"), file=sys.stderr)
        return 1
    print("已提交截图识别")
    _poll_tasks_brief(log)
    return 0


def _poll_tasks_brief(log, *, rounds: int = 12, interval: float = 0.5) -> None:
    """轮询几次任务进度，写入 stdout / 日志。"""
    seen_running = False
    for _ in range(rounds):
        st = request_status()
        tasks = st.get("tasks") or []
        active = [t for t in tasks if t.get("status") in ("queued", "running")]
        if active:
            seen_running = True
            for t in active:
                line = (
                    f"  [{t.get('id')}] {t.get('title')} "
                    f"{t.get('status')} {float(t.get('progress', 0)):.0f}% "
                    f"{t.get('detail') or ''}"
                )
                print(line)
                log.info(line.strip())
        if seen_running and not active:
            for t in tasks:
                if t.get("status") == "done":
                    print(f"  完成: {t.get('title')}")
            break
        time.sleep(interval)


def _print_status(st: dict) -> None:
    if not st.get("running"):
        print("托盘: 未运行")
        return
    print("托盘: 运行中")
    tasks = st.get("tasks") or []
    if not tasks:
        print("  (无任务)")
        return
    for t in tasks:
        err = f" — {t['error']}" if t.get("error") else ""
        print(
            f"  [{t.get('id')}] {t.get('title')} "
            f"— {t.get('status')} {float(t.get('progress', 0)):.0f}% "
            f"{t.get('detail') or ''}{err}"
        )


def ensure_tray_running() -> None:
    """若托盘未运行则后台拉起。"""
    if not is_app_alive():
        spawn_detached_app()


if __name__ == "__main__":
    raise SystemExit(main())
