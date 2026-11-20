"""Windows 全局快捷键：Win32 RegisterHotKey + Qt 原生事件过滤器接收 WM_HOTKEY。

Linux 上仍走 KDE KGlobalAccel（见 hotkey.py）；本模块只在 Windows 时导入。
保持与 KDE 版 GlobalHotkey 相同的对外接口：available / current / apply / activated / shutdown。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QWidget

log = logging.getLogger("ocrtool")

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # 按住不重复触发
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
_HOTKEY_ID = 0x4F43  # 应用唯一 id

try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    _user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    _user32.RegisterHotKey.restype = wintypes.BOOL
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.UnregisterHotKey.restype = wintypes.BOOL
except (AttributeError, OSError):
    _user32 = None  # type: ignore[assignment]


def _qt_key_to_vk(key: int) -> int:
    """Qt::Key 值 → Win32 虚拟键码。字母/数字本身即 ASCII，特殊键需映射。"""
    from PySide6.QtCore import Qt

    # 字母 A-Z / 数字 0-9：Qt 键码就是大写 ASCII，等于 VK 值。
    if ord("A") <= key <= ord("Z") or ord("0") <= key <= ord("9"):
        return key
    # 功能键 F1..F35：Qt 基址 0x01000030，VK_F1=0x70
    if Qt.Key.Key_F1.value <= key <= Qt.Key.Key_F35.value:
        return 0x70 + (key - Qt.Key.Key_F1.value)
    mapping = {
        Qt.Key.Key_Space.value: 0x20,
        Qt.Key.Key_Return.value: 0x0D,
        Qt.Key.Key_Enter.value: 0x0D,
        Qt.Key.Key_Escape.value: 0x1B,
        Qt.Key.Key_Tab.value: 0x09,
        Qt.Key.Key_Backspace.value: 0x08,
        Qt.Key.Key_Delete.value: 0x2E,
        Qt.Key.Key_Insert.value: 0x2D,
        Qt.Key.Key_Home.value: 0x24,
        Qt.Key.Key_End.value: 0x23,
        Qt.Key.Key_PageUp.value: 0x21,
        Qt.Key.Key_PageDown.value: 0x22,
        Qt.Key.Key_Left.value: 0x25,
        Qt.Key.Key_Right.value: 0x27,
        Qt.Key.Key_Up.value: 0x26,
        Qt.Key.Key_Down.value: 0x28,
    }
    return mapping.get(key, 0)


def _vk_and_mods(sequence: str) -> tuple[int, int]:
    """把组合键字符串转成 (Win32 虚拟键码, 修饰键位)。"""
    seq = QKeySequence(sequence)
    if seq.isEmpty():
        return 0, 0
    combo = seq[0]
    vk = _qt_key_to_vk(int(combo.key()))
    mods = 0
    m = combo.keyboardModifiers()
    from PySide6.QtCore import Qt

    if m & Qt.KeyboardModifier.ControlModifier:
        mods |= MOD_CONTROL
    if m & Qt.KeyboardModifier.AltModifier:
        mods |= MOD_ALT
    if m & Qt.KeyboardModifier.ShiftModifier:
        mods |= MOD_SHIFT
    if m & Qt.KeyboardModifier.MetaModifier:
        mods |= MOD_WIN
    return vk, mods


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class WinNativeEventFilter(QAbstractNativeEventFilter):
    """接收 WM_HOTKEY 并回调（QAbstractNativeEventFilter 非 QObject，不能用 Signal）。

    三个 PySide6 / Qt 细节：
    - eventType 是 QByteArray；窗口消息走 "windows_generic_MSG"（同一条消息还会
      以 "windows_dispatcher_MSG" 再过一次过滤器，故只匹配前者）。
    - 即便如此，Qt 对同一窗口消息仍会把过滤器链跑两遍，需按 (wParam, time) 去重，
      否则按一次热键触发两次截图。
    - message 是 shiboken VoidPtr，须用 int() 取地址，且其 __bool__ 不可靠，
      不能用 ``if message`` 判空。
    """

    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__()
        self._callback = callback
        self._last: tuple[int, int] | None = None

    def nativeEventFilter(self, eventType, message):  # noqa: N802
        try:
            etype = bytes(eventType) if not isinstance(eventType, bytes) else eventType
            if etype == b"windows_generic_MSG" and message is not None:
                msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
                if msg.message == WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    key = (int(msg.wParam), int(msg.time))
                    if key != self._last:
                        self._last = key
                        self._callback()
        except Exception:  # noqa: BLE001
            pass
        return False, 0


class GlobalHotkey(QObject):
    """Windows 全局快捷键（与 KDE 版接口一致）。

    RegisterHotKey 的 hwnd 不能传 NULL——传 NULL 时 WM_HOTKEY 进线程消息队列，
    Qt 原生事件过滤器收不到。因此建一个永不显示的隐藏窗口持有热键注册。
    """

    activated = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bus = None
        self._available = _user32 is not None
        self._current = ""
        self._registered = False
        self._owner: QWidget | None = None  # 隐藏窗口，持有热键注册
        self._filter = WinNativeEventFilter(self.activated.emit)
        app = QApplication.instance()
        if app is not None and self._available:
            app.installNativeEventFilter(self._filter)
        if not self._available:
            log.info("Win32 快捷键 API 不可用")

    @property
    def available(self) -> bool:
        return self._available

    @property
    def current(self) -> str:
        return self._current

    def _owner_hwnd(self) -> int:
        """惰性创建隐藏窗口并返回其 HWND（winId() 强制创建真实窗口句柄）。"""
        if self._owner is None:
            self._owner = QWidget(None, Qt.WindowType.Tool)
            self._owner.setWindowTitle("ocr-tool-hotkey-owner")
            self._owner.resize(1, 1)
        return int(self._owner.winId())

    def _unregister(self) -> None:
        if self._registered and _user32 is not None:
            _user32.UnregisterHotKey(wintypes.HWND(self._owner_hwnd()), _HOTKEY_ID)
            self._registered = False

    def apply(self, sequence: str) -> tuple[bool, str]:
        if not self._available:
            return False, "当前环境不支持全局快捷键"
        self._unregister()
        sequence = (sequence or "").strip()
        if not sequence:
            self._current = ""
            return True, "已禁用全局快捷键"
        vk, mods = _vk_and_mods(sequence)
        if vk == 0 or mods == 0:
            return False, "快捷键无效：需包含 Ctrl/Alt/Shift/Win 修饰键和一个主键"
        hwnd = self._owner_hwnd()
        ok = bool(
            _user32.RegisterHotKey(
                wintypes.HWND(hwnd), _HOTKEY_ID, mods | MOD_NOREPEAT, vk
            )
        )
        if not ok:
            code = ctypes.get_last_error()
            if code == ERROR_HOTKEY_ALREADY_REGISTERED:
                return False, f"快捷键已被其他程序占用：{sequence}"
            return False, f"注册快捷键失败（code={code}）"
        self._registered = True
        self._current = sequence
        log.info("全局快捷键已注册: %s (vk=0x%02X mods=0x%X)", sequence, vk, mods)
        return True, f"已生效：{sequence}"

    def shutdown(self) -> None:
        self._unregister()
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeNativeEventFilter(self._filter)
            except Exception:  # noqa: BLE001
                pass
        if self._owner is not None:
            self._owner.close()
            self._owner.deleteLater()
            self._owner = None
