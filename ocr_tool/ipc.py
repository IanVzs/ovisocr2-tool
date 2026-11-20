"""托盘单例 IPC：第二实例提交截图 / quit / status。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any, Callable

from ocr_tool.paths import (
    IS_WINDOWS,
    PID_PATH,
    RUNTIME_DIR,
    SOCKET_PATH,
    ensure_runtime_dir,
)

_RECV_BUF = 65536
PORT_PATH = RUNTIME_DIR / "ocr-tool.port"


def _connect() -> socket.socket:
    """建立到托盘的连接（Linux: AF_UNIX；Windows: TCP loopback）。"""
    if IS_WINDOWS:
        port = int(PORT_PATH.read_text(encoding="ascii").strip())
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(60.0)
        sock.connect(("127.0.0.1", port))
        return sock
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(60.0)
    sock.connect(str(SOCKET_PATH))
    return sock


def _alive_marker_exists() -> bool:
    if IS_WINDOWS:
        return PORT_PATH.exists()
    return SOCKET_PATH.exists()


def _probe() -> dict | None:
    """探测 socket 对端；失败返回 None。"""
    if not _alive_marker_exists():
        return None
    try:
        with _connect() as sock:
            sock.settimeout(1.0)
            _send(sock, {"cmd": "ping"})
            return _recv(sock)
    except (OSError, ValueError):
        return None


def is_app_alive() -> bool:
    """托盘应用是否在跑。"""
    resp = _probe()
    if not resp or not resp.get("ok"):
        if _alive_marker_exists() and resp is None:
            _cleanup_stale()
        return False
    return True


def request(cmd: str, **extra: Any) -> dict:
    with _connect() as sock:
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
    popen_kwargs: dict[str, Any] = {
        "stdout": log_file,
        "stderr": log_file,
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    if IS_WINDOWS:
        # Windows 无 start_new_session，用独立进程组 + 不弹控制台窗口。
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        popen_kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **popen_kwargs)
    log_file.close()
    deadline = time.time() + 45
    while time.time() < deadline:
        if is_app_alive():
            return
        time.sleep(0.2)
    raise RuntimeError("托盘应用启动超时")


def _cleanup_stale() -> None:
    paths = (SOCKET_PATH, PID_PATH, PORT_PATH) if IS_WINDOWS else (SOCKET_PATH, PID_PATH)
    for path in paths:
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
    if IS_WINDOWS:
        PORT_PATH.unlink(missing_ok=True)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # 绑定随机空闲端口，写入文件供客户端发现。
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        PORT_PATH.write_text(str(port), encoding="ascii")
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    else:
        if SOCKET_PATH.exists():
            try:
                SOCKET_PATH.unlink()
            except OSError:
                pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(SOCKET_PATH))
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    server.listen(8)
    server.settimeout(0.5)

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
