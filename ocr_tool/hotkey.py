"""全局快捷键（KDE Plasma，KGlobalAccel over DBus）。

Wayland 下应用无法自行 grab 全局按键（pynput 那套只在 X11 生效），必须让合成器
代管。KDE Plasma 的 ``org.kde.kglobalaccel`` 允许注册一个组件 + 动作并指定具体
组合键，然后由 kwin 在按键时通过 DBus 信号回调本进程——这也是 KDE 原生应用的做法，
Wayland/X11 都能用。

非 KDE 环境（没有 kglobalaccel 服务）时本模块整体降级为不可用，UI 侧据此提示，
不影响托盘其余功能。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

from PySide6.QtCore import SLOT, QObject, Signal, Slot
from PySide6.QtDBus import QDBusConnection, QDBusInterface
from PySide6.QtGui import QKeySequence

from ocr_tool.proc import system_env

log = logging.getLogger("ocrtool")

_SERVICE = "org.kde.kglobalaccel"
_PATH = "/kglobalaccel"
_IFACE = "org.kde.KGlobalAccel"
_COMPONENT_IFACE = "org.kde.kglobalaccel.Component"

# 组件 / 动作标识（对应 kwin 里的注册项，也决定组件对象路径 /component/ocr_tool）。
_COMPONENT_UNIQUE = "ocr_tool"
_COMPONENT_FRIENDLY = "OCR工具"
_ACTION_UNIQUE = "capture"
_ACTION_FRIENDLY = "截图识别"

# SetShortcutFlag：SetPresent(2) 让 daemon 真正 grab 物理键，NoAutoloading(4) 不落盘、
# 不自动加载（每次启动由我们按配置重设，语义确定）。
_SET_PRESENT_NOAUTOLOAD = 6


def _action_id() -> list[str]:
    return [_COMPONENT_UNIQUE, _ACTION_UNIQUE, _COMPONENT_FRIENDLY, _ACTION_FRIENDLY]


def _key_int(sequence: str) -> int:
    """把 "Meta+Shift+O" 这类字符串转成 KGlobalAccel 用的合成整型键值。"""
    seq = QKeySequence(sequence)
    if seq.isEmpty():
        return 0
    return int(seq[0].toCombined())


def _parse_granted_keys(stdout: str, *, busctl: bool) -> list[int]:
    """解析 setShortcut 返回的实际按键整型列表。

    gdbus 形如 ``([301989967],)``；busctl 形如 ``ai 1 301989967``（类型 计数 值…）。
    """
    if busctl:
        toks = stdout.split()
        if len(toks) >= 2 and toks[0] == "ai":
            count = int(toks[1])
            return [int(t) for t in toks[2 : 2 + count]]
        return []
    return [int(m) for m in re.findall(r"-?\d+", stdout)]


class GlobalHotkey(QObject):
    """注册 / 更新全局快捷键，并在按下时发出 :attr:`activated`。"""

    activated = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bus = QDBusConnection.sessionBus()
        self._iface = QDBusInterface(_SERVICE, _PATH, _IFACE, self._bus)
        self.available = bool(self._bus.isConnected() and self._iface.isValid())
        self._signal_connected = False
        self._current = ""
        if not self.available:
            log.info("未检测到 KGlobalAccel，全局快捷键不可用（非 KDE 环境）")

    @property
    def current(self) -> str:
        return self._current

    def apply(self, sequence: str) -> tuple[bool, str]:
        """设置（或清除）快捷键。返回 (成功, 展示给用户的说明)。"""
        if not self.available:
            return False, "当前桌面环境不支持全局快捷键"

        self._iface.call("doRegister", _action_id())

        sequence = (sequence or "").strip()
        if not sequence:
            if self._set_shortcut([]) is None:
                return False, "清除失败：缺少 gdbus / busctl"
            self._current = ""
            return True, "已禁用全局快捷键"

        key = _key_int(sequence)
        if key == 0:
            return False, "快捷键无效"

        # 关键：必须走 setShortcut + SetPresent，daemon 才会真正 grab 物理键；
        # 只 setForeignShortcut 会“注册但不抓键”，表现为按键无响应。
        # 而 setShortcut 的 flags 是 DBus uint32，PySide 无法编出顶层 u，故借道 gdbus/busctl。
        granted = self._set_shortcut([key])
        if granted is None:
            return False, "设置失败：缺少 gdbus / busctl"

        self._ensure_signal_connected()
        self._current = sequence
        # daemon 返回实际分到的键：拿不到该键说明被别的应用占用。
        if key not in granted:
            return True, f"已设为 {sequence}，但可能被其它应用占用"
        return True, f"已生效：{sequence}"

    def _set_shortcut(self, keys: list[int]) -> list[int] | None:
        """调 setShortcut(SetPresent|NoAutoloading)，返回 daemon 实际分配的键；失败返回 None。

        setShortcut 的 flags 是 DBus uint32，PySide 无法编出顶层 ``u``，故借道命令行
        DBus 客户端（gdbus 优先，busctl 兜底）。用 :func:`system_env` 还原干净环境，
        避免冻结包把包内 Qt/库路径泄漏给系统程序。
        """
        env = system_env()
        gdbus = shutil.which("gdbus", path=env.get("PATH"))
        busctl = None if gdbus else shutil.which("busctl", path=env.get("PATH"))
        if gdbus:
            action = (
                f"['{_COMPONENT_UNIQUE}', '{_ACTION_UNIQUE}', "
                f"'{_COMPONENT_FRIENDLY}', '{_ACTION_FRIENDLY}']"
            )
            keys_arg = "@ai []" if not keys else "[" + ",".join(map(str, keys)) + "]"
            cmd = [
                gdbus, "call", "--session",
                "--dest", _SERVICE,
                "--object-path", _PATH,
                "--method", f"{_IFACE}.setShortcut",
                action, keys_arg, f"uint32 {_SET_PRESENT_NOAUTOLOAD}",
            ]
        elif busctl:
            cmd = [
                busctl, "--user", "call", _SERVICE, _PATH, _IFACE,
                "setShortcut", "asaiu",
                "4", _COMPONENT_UNIQUE, _ACTION_UNIQUE,
                _COMPONENT_FRIENDLY, _ACTION_FRIENDLY,
                str(len(keys)), *[str(k) for k in keys],
                str(_SET_PRESENT_NOAUTOLOAD),
            ]
        else:
            log.warning("未找到 gdbus / busctl，无法激活全局快捷键")
            return None
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5,
                env=env, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("设置全局快捷键失败: %s", exc)
            return None
        if res.returncode != 0:
            log.warning("setShortcut 返回 %s: %s", res.returncode, res.stderr.strip())
            return None
        return _parse_granted_keys(res.stdout, busctl=bool(busctl))

    def _ensure_signal_connected(self) -> None:
        if self._signal_connected:
            return
        ok = self._bus.connect(
            _SERVICE,
            f"/component/{_COMPONENT_UNIQUE}",
            _COMPONENT_IFACE,
            "globalShortcutPressed",
            self,
            SLOT("_on_pressed(QString,QString,qlonglong)"),
        )
        self._signal_connected = ok
        if not ok:
            log.warning("连接 globalShortcutPressed 信号失败")

    @Slot(str, str, "qlonglong")
    def _on_pressed(self, _component: str, _action: str, _timestamp: int) -> None:
        # kwin 回调：componentUnique, actionUnique, timestamp。只有 capture 动作。
        self.activated.emit()

    def shutdown(self) -> None:
        """退出时注销，避免残留占用组合键。"""
        if not self.available:
            return
        try:
            self._iface.call("unregister", _COMPONENT_UNIQUE, _ACTION_UNIQUE)
        except Exception as exc:  # noqa: BLE001
            log.debug("注销快捷键忽略异常: %s", exc)
