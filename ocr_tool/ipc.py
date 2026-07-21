"""托盘单例 IPC：第二实例提交截图 / quit / status。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any, Callable

from ocr_tool.paths import PID_PATH, RUNTIME_DIR, SOCKET_PATH, ensure_runtime_dir

_RECV_BUF = 65536


def _probe() -> dict | None:
    """探测 socket 对端；失败返回 None。"""
    if not SOCKET_PATH.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(str(SOCKET_PATH))
            _send(sock, {"cmd": "ping"})
            return _recv(sock)
    except OSError:
        return None


def is_app_alive() -> bool:
    """托盘应用是否在跑。"""
    resp = _probe()
    if not resp or not resp.get("ok"):
        if SOCKET_PATH.exists() and resp is None:
            _cleanup_stale()
        return False
    return True


def request(cmd: str, **extra: Any) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(60.0)
        sock.connect(str(SOCKET_PATH))
        _send(sock, {"cmd": cmd, **extra})
        return _recv(sock)


def request_capture() -> dict:
    return request("capture")


def request_quit() -> bool:
    resp = _probe()
    if not resp or not resp.get("ok"):
        _cleanup_stale()
        return False
    try:
        request("quit")
    except OSError:
        pass
    deadline = time.time() + 5
    while time.time() < deadline and _probe() is not None:
        time.sleep(0.1)
    _cleanup_stale()
    return True


def request_status() -> dict:
    if not is_app_alive():
        return {"running": False, "tasks": []}
    try:
        resp = request("status")
        resp.setdefault("running", True)
        return resp
    except OSError:
        return {"running": False, "tasks": []}


def spawn_detached_app() -> None:
    """后台拉起托盘进程。"""
    ensure_runtime_dir()
    env = os.environ.copy()
    log_path = RUNTIME_DIR / "ocr-tool-spawn.log"
    log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    # 冻结(PyInstaller)后 sys.executable 就是应用自身，不支持 `-m ocr_tool`；
    # 直接把 --foreground 传给它。源码运行则走 `-m ocr_tool`。
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--foreground"]
    else:
        cmd = [sys.executable, "-m", "ocr_tool", "--foreground"]
    subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=log_file,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    log_file.close()
    deadline = time.time() + 45
    while time.time() < deadline:
        if is_app_alive():
            return
        time.sleep(0.2)
    raise RuntimeError("托盘应用启动超时")


def _cleanup_stale() -> None:
    for path in (SOCKET_PATH, PID_PATH):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _send(sock: socket.socket, payload: dict) -> None:
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    sock.sendall(data)


def _recv(sock: socket.socket) -> dict:
    buf = bytearray()
    line: bytes | None = None
    while True:
        chunk = sock.recv(_RECV_BUF)
        if not chunk:
            break
        buf.extend(chunk)
        nl = buf.find(b"\n")
        if nl >= 0:
            line = bytes(buf[:nl])
            break
    if line is None:
        line = bytes(buf)
    if not line:
        return {"ok": False, "error": "空响应"}
    try:
        return json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"响应解析失败: {exc}"}


Handler = Callable[[dict], dict]


def serve_ipc(handler: Handler, *, stop_flag: Callable[[], bool]) -> None:
    """后台线程：accept + 处理。"""
    ensure_runtime_dir()
    if SOCKET_PATH.exists():
        try:
            SOCKET_PATH.unlink()
        except OSError:
            pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET_PATH))
    server.listen(8)
    server.settimeout(0.5)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")

    try:
        while not stop_flag():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            try:
                req = _recv(conn)
                resp = handler(req)
                _send(conn, resp)
            except Exception as exc:  # noqa: BLE001
                try:
                    _send(conn, {"ok": False, "error": str(exc)})
                except OSError:
                    pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
    finally:
        try:
            server.close()
        except OSError:
            pass
        _cleanup_stale()
