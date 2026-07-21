"""系统托盘 + 任务面板。"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QKeyCombination,
    QObject,
    QPoint,
    QRect,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ocr_tool.capture import CaptureCancelled, CaptureError, capture_region
from ocr_tool.clipboard import ClipboardError, copy_text
from ocr_tool.config import DEFAULT_HOTKEY, load_config, save_config
from ocr_tool.hotkey import GlobalHotkey
from ocr_tool.ipc import serve_ipc
from ocr_tool.job_runner import JobRunner
from ocr_tool.notify import notify
from ocr_tool.tasks import Task, TaskKind, TaskManager, TaskStatus
from ocr_tool.version import app_version

log = logging.getLogger("ocrtool")

APP_NAME = "OCR工具"

_STATUS_LABELS = {
    TaskStatus.QUEUED: "排队",
    TaskStatus.RUNNING: "进行中",
    TaskStatus.DONE: "完成",
    TaskStatus.FAILED: "失败",
    TaskStatus.CANCELLED: "已取消",
}

_STATUS_COLORS_LIGHT = {
    TaskStatus.QUEUED: ("#E2E8F0", "#475569"),
    TaskStatus.RUNNING: ("#DBEAFE", "#1D4ED8"),
    TaskStatus.DONE: ("#D1FAE5", "#047857"),
    TaskStatus.FAILED: ("#FEE2E2", "#B91C1C"),
    TaskStatus.CANCELLED: ("#F1F5F9", "#64748B"),
}

_STATUS_COLORS_DARK = {
    TaskStatus.QUEUED: ("#334155", "#CBD5E1"),
    TaskStatus.RUNNING: ("#1E3A5F", "#93C5FD"),
    TaskStatus.DONE: ("#0B3B2E", "#6EE7B7"),
    TaskStatus.FAILED: ("#4C1D1D", "#FCA5A5"),
    TaskStatus.CANCELLED: ("#2E3440", "#94A3B8"),
}

_PANEL_QSS_LIGHT = """
#TaskPanel {
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 14px;
}
#PanelTitle { color: #0F172A; font-size: 14px; font-weight: 700; }
#PanelHint { color: #94A3B8; font-size: 11px; }
#CloseBtn {
    background: transparent; border: none; color: #94A3B8;
    font-size: 16px; padding: 2px 8px; border-radius: 6px;
}
#CloseBtn:hover { background: #F1F5F9; color: #334155; }
#TaskScroll { background: transparent; border: none; }
#TaskCard {
    background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 12px;
}
#TaskTitle { color: #0F172A; font-size: 13px; font-weight: 600; }
#TaskDetail { color: #64748B; font-size: 11px; }
#TaskPreview {
    color: #475569; font-size: 11px; background: #EEF2F7;
    border-radius: 6px; padding: 5px 8px;
}
#TaskImage {
    background: #0F172A; border: 1px solid #E2E8F0; border-radius: 8px; padding: 4px;
}
#TaskError { color: #DC2626; font-size: 11px; }
#StatusPill {
    border-radius: 9px; padding: 2px 8px; font-size: 11px; font-weight: 600;
}
#ActionBtn {
    background: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 8px;
    color: #334155; padding: 5px 12px; font-size: 12px;
}
#ActionBtn:hover { background: #EFF6FF; border-color: #93C5FD; color: #1D4ED8; }
#ActionBtn:disabled { background: #F1F5F9; border-color: #E2E8F0; color: #CBD5E1; }
QProgressBar {
    background: #E2E8F0; border: none; border-radius: 5px; height: 10px;
    text-align: center; color: transparent;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #0EA5E9, stop:1 #2563EB);
    border-radius: 5px;
}
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical {
    background: #CBD5E1; border-radius: 4px; min-height: 24px;
}
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
#FooterSep { background: #E2E8F0; max-height: 1px; min-height: 1px; border: none; }
#SectionLabel { color: #64748B; font-size: 11px; font-weight: 600; }
#HotkeyBtn {
    background: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 8px;
    color: #0F172A; padding: 5px 12px; font-size: 12px; font-weight: 600;
}
#HotkeyBtn:hover { border-color: #93C5FD; }
#HotkeyBtn[capturing="true"] { border-color: #2563EB; color: #2563EB; background: #EFF6FF; }
#LinkBtn {
    background: transparent; border: none; color: #94A3B8; font-size: 11px; padding: 2px 4px;
}
#LinkBtn:hover { color: #2563EB; }
#HotkeyStatus { color: #94A3B8; font-size: 11px; }
#VersionLabel { color: #94A3B8; font-size: 11px; }
"""

_PANEL_QSS_DARK = """
#TaskPanel {
    background: #20242E;
    border: 1px solid #333A47;
    border-radius: 14px;
}
#PanelTitle { color: #F1F5F9; font-size: 14px; font-weight: 700; }
#PanelHint { color: #7C879B; font-size: 11px; }
#CloseBtn {
    background: transparent; border: none; color: #8A94A6;
    font-size: 16px; padding: 2px 8px; border-radius: 6px;
}
#CloseBtn:hover { background: #2C333F; color: #E2E8F0; }
#TaskScroll { background: transparent; border: none; }
#TaskCard {
    background: #2A303C; border: 1px solid #363E4C; border-radius: 12px;
}
#TaskTitle { color: #E6EAF2; font-size: 13px; font-weight: 600; }
#TaskDetail { color: #98A2B3; font-size: 11px; }
#TaskPreview {
    color: #C3CCDA; font-size: 11px; background: #232A36;
    border-radius: 6px; padding: 5px 8px;
}
#TaskImage {
    background: #12151C; border: 1px solid #363E4C; border-radius: 8px; padding: 4px;
}
#TaskError { color: #F87171; font-size: 11px; }
#StatusPill {
    border-radius: 9px; padding: 2px 8px; font-size: 11px; font-weight: 600;
}
#ActionBtn {
    background: #2F3644; border: 1px solid #3C4557; border-radius: 8px;
    color: #CBD5E1; padding: 5px 12px; font-size: 12px;
}
#ActionBtn:hover { background: #313B52; border-color: #3B82F6; color: #93C5FD; }
#ActionBtn:disabled { background: #262C36; border-color: #2E3440; color: #566072; }
QProgressBar {
    background: #363E4C; border: none; border-radius: 5px; height: 10px;
    text-align: center; color: transparent;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #0EA5E9, stop:1 #2563EB);
    border-radius: 5px;
}
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical {
    background: #3C4557; border-radius: 4px; min-height: 24px;
}
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
#FooterSep { background: #333A47; max-height: 1px; min-height: 1px; border: none; }
#SectionLabel { color: #98A2B3; font-size: 11px; font-weight: 600; }
#HotkeyBtn {
    background: #2F3644; border: 1px solid #3C4557; border-radius: 8px;
    color: #E6EAF2; padding: 5px 12px; font-size: 12px; font-weight: 600;
}
#HotkeyBtn:hover { border-color: #3B82F6; }
#HotkeyBtn[capturing="true"] { border-color: #3B82F6; color: #93C5FD; background: #313B52; }
#LinkBtn {
    background: transparent; border: none; color: #7C879B; font-size: 11px; padding: 2px 4px;
}
#LinkBtn:hover { color: #93C5FD; }
#HotkeyStatus { color: #7C879B; font-size: 11px; }
#VersionLabel { color: #7C879B; font-size: 11px; }
"""


def build_theme(app: QApplication) -> dict:
    """按系统调色板明暗选一套面板样式（暗色桌面→暗色面板，避免闪瞎）。"""
    win = app.palette().color(QPalette.ColorRole.Window)
    lum = 0.299 * win.red() + 0.587 * win.green() + 0.114 * win.blue()
    dark = lum < 128
    return {
        "dark": dark,
        "qss": _PANEL_QSS_DARK if dark else _PANEL_QSS_LIGHT,
        "status_colors": _STATUS_COLORS_DARK if dark else _STATUS_COLORS_LIGHT,
    }


def make_tray_icon() -> QIcon:
    """优先用 packaging 图标，缺失时退回绘制的 OCR 方块。"""
    candidates = [
        Path(__file__).resolve().parents[1] / "packaging" / "ocr-tool-icon-256.png",
        Path(__file__).resolve().parents[1] / "packaging" / "ocr-tool-icon.png",
    ]
    for path in candidates:
        if path.is_file():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon

    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#0F766E"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(4, 4, size - 8, size - 8, 14, 14)
    painter.setPen(QColor("#FFFFFF"))
    font = QFont("Sans Serif", 11, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "OCR")
    painter.end()
    return QIcon(pm)


class TaskRow(QFrame):
    """单条任务卡片。"""

    # 预览文本裁剪长度（折叠空白后取前 N 字，够区分不同任务即可）。
    _PREVIEW_CHARS = 90
    _IMAGE_MAX_W = 356

    def __init__(self, task: Task, controller: TrayController) -> None:
        super().__init__()
        self.setObjectName("TaskCard")
        self.task_id = task.id
        self._task = task
        self._controller = controller
        self._image_open = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._title = QLabel(task.title)
        self._title.setObjectName("TaskTitle")
        self._title.setWordWrap(True)
        header.addWidget(self._title, stretch=1)
        self._status = QLabel()
        self._status.setObjectName("StatusPill")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self._status, alignment=Qt.AlignmentFlag.AlignTop)
        root.addLayout(header)

        self._detail = QLabel()
        self._detail.setObjectName("TaskDetail")
        self._detail.setWordWrap(True)
        root.addWidget(self._detail)

        # 识别结果预览：截取一段文本，方便一眼区分是哪次任务的内容。
        self._preview = QLabel()
        self._preview.setObjectName("TaskPreview")
        self._preview.setWordWrap(True)
        self._preview.hide()
        root.addWidget(self._preview)

        prog_row = QHBoxLayout()
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(10)
        prog_row.addWidget(self._progress, stretch=1)
        self._pct = QLabel("0%")
        self._pct.setObjectName("TaskDetail")
        self._pct.setFixedWidth(36)
        self._pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        prog_row.addWidget(self._pct)
        root.addLayout(prog_row)

        self._error = QLabel()
        self._error.setObjectName("TaskError")
        self._error.setWordWrap(True)
        self._error.hide()
        root.addWidget(self._error)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._view_btn = QPushButton("查看原图")
        self._view_btn.setObjectName("ActionBtn")
        self._view_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._view_btn.clicked.connect(self._on_view)
        self._copy_btn = QPushButton("复制文本")
        self._copy_btn.setObjectName("ActionBtn")
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.clicked.connect(self._on_copy)
        actions.addWidget(self._view_btn)
        actions.addWidget(self._copy_btn)
        actions.addStretch()
        root.addLayout(actions)

        # 原图就地展开区域（不再弹独立窗口，避免主窗口失焦被关）。
        self._image = QLabel()
        self._image.setObjectName("TaskImage")
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.hide()
        root.addWidget(self._image)

        self.refresh(task)

    def refresh(self, task: Task) -> None:
        self._task = task
        self._title.setText(task.title)
        status_text = _STATUS_LABELS.get(task.status, task.status.value)
        self._status.setText(status_text)
        colors = self._controller.status_colors
        bg, fg = colors.get(task.status, ("#334155", "#CBD5E1"))
        self._status.setStyleSheet(
            f"#StatusPill {{ background: {bg}; color: {fg}; "
            f"border-radius: 9px; padding: 2px 8px; font-size: 11px; font-weight: 600; }}"
        )
        self._detail.setText(task.detail or "")
        self._detail.setVisible(bool(task.detail))
        pct = int(task.progress)
        self._progress.setValue(pct)
        self._pct.setText(f"{pct}%")
        if task.error:
            self._error.setText(task.error)
            self._error.show()
        else:
            self._error.hide()

        preview = self._preview_text(task)
        self._preview.setText(preview)
        self._preview.setVisible(bool(preview))

        show_actions = task.show_action_buttons
        self._view_btn.setVisible(show_actions)
        self._copy_btn.setVisible(show_actions)
        if show_actions:
            self._view_btn.setEnabled(task.can_view_image)
            self._copy_btn.setEnabled(task.can_copy)
        # 图片没了（不可看）就把展开的原图收起来。
        if self._image_open and not task.can_view_image:
            self._collapse_image()

    def _preview_text(self, task: Task) -> str:
        raw = " ".join((task.result_text or "").split())
        if not raw:
            return ""
        if len(raw) > self._PREVIEW_CHARS:
            return raw[: self._PREVIEW_CHARS] + "…"
        return raw

    def _on_view(self) -> None:
        if self._image_open:
            self._collapse_image()
            return
        if not self._task.image_path:
            return
        pixmap = QPixmap(str(self._task.image_path))
        if pixmap.isNull():
            notify(APP_NAME, "无法加载原图")
            return
        if pixmap.width() > self._IMAGE_MAX_W:
            pixmap = pixmap.scaledToWidth(
                self._IMAGE_MAX_W, Qt.TransformationMode.SmoothTransformation
            )
        self._image.setPixmap(pixmap)
        self._image.show()
        self._image_open = True
        self._view_btn.setText("收起原图")
        self._request_relayout()

    def _collapse_image(self) -> None:
        self._image.hide()
        self._image.clear()
        self._image_open = False
        self._view_btn.setText("查看原图")
        self._request_relayout()

    def _request_relayout(self) -> None:
        # 展开 / 收起后让顶层面板重新按内容调整高度。
        win = self.window()
        if win is not None:
            win.adjustSize()

    def _on_copy(self) -> None:
        if not self._task.can_copy or not self._task.result_text:
            return
        try:
            copy_text(self._task.result_text)
            notify(APP_NAME, "已复制到剪贴板")
        except ClipboardError as exc:
            notify(APP_NAME, f"复制失败：{exc}", urgency="critical")


class OutsideClickFilter(QObject):
    """点击面板外任意处时关闭面板。"""

    def __init__(self, panel: TaskPanel) -> None:
        super().__init__(panel)
        self._panel = panel

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        try:
            if not self._panel.isVisible():
                return False
        except RuntimeError:
            # 面板 C++ 对象已在退出期销毁，忽略
            return False
        if event.type() != QEvent.Type.MouseButtonPress:
            return False
        if time.monotonic() < self._panel.ignore_outside_until:
            return False
        app = QApplication.instance()
        if app is not None and app.activeModalWidget() is not None:
            return False
        if not hasattr(event, "globalPosition"):
            return False
        pos = event.globalPosition().toPoint()  # type: ignore[attr-defined]
        if self._panel.frameGeometry().contains(pos):
            return False
        QTimer.singleShot(0, self._panel.hide)
        return False


_MODIFIER_KEYS = {
    Qt.Key.Key_Shift,
    Qt.Key.Key_Control,
    Qt.Key.Key_Alt,
    Qt.Key.Key_Meta,
    Qt.Key.Key_Super_L,
    Qt.Key.Key_Super_R,
    Qt.Key.Key_AltGr,
}


class HotkeyButton(QPushButton):
    """点一下开始录制组合键，按下含修饰键的组合即提交。

    录制期间 grab 键盘并吞掉 Esc（取消录制而非关闭面板）。要求至少含一个修饰键，
    否则全局快捷键容易和普通输入撞车。
    """

    captured = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("HotkeyBtn")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._capturing = False
        self._value = ""
        self.clicked.connect(self._toggle_capture)

    def set_value(self, sequence: str) -> None:
        self._value = sequence or ""
        if not self._capturing:
            self.setText(self._value or "未设置")

    @property
    def is_capturing(self) -> bool:
        return self._capturing

    def _toggle_capture(self) -> None:
        if self._capturing:
            self._stop_capture()
        else:
            self._start_capture()

    def _start_capture(self) -> None:
        self._capturing = True
        self.setText("按下组合键…")
        self._restyle()
        self.grabKeyboard()

    def _stop_capture(self) -> None:
        self._capturing = False
        self.releaseKeyboard()
        self.setText(self._value or "未设置")
        self._restyle()

    def cancel_capture(self) -> bool:
        """外部（Esc）请求取消录制；正在录制返回 True。"""
        if self._capturing:
            self._stop_capture()
            return True
        return False

    def _restyle(self) -> None:
        self.setProperty("capturing", "true" if self._capturing else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if not self._capturing:
            super().keyPressEvent(event)
            return
        key = Qt.Key(event.key())
        if key == Qt.Key.Key_Escape:
            self._stop_capture()
            return
        if key in _MODIFIER_KEYS:
            return  # 等待非修饰键
        mods = event.modifiers()
        if mods == Qt.KeyboardModifier.NoModifier or mods == Qt.KeyboardModifier.KeypadModifier:
            self.setText("需含 Ctrl/Alt/Meta…")
            return
        seq = QKeySequence(QKeyCombination(mods, key))
        text = seq.toString(QKeySequence.SequenceFormat.PortableText)
        if not text:
            return
        self._capturing = False
        self.releaseKeyboard()
        self._value = text
        self.setText(text)
        self._restyle()
        self.captured.emit(text)


class TaskPanel(QWidget):
    """托盘旁弹出的任务面板（普通顶层窗口）。

    应用默认跑在 XWayland(xcb) 上（见 run_tray_app）：X11 语义下可 move() 到光标处、
    QCursor.pos() 准确、能收到失焦事件。因此这里用普通顶层窗口 + move 锚定光标，
    并通过失焦 / 点外部 / Esc 三种方式关闭。
    """

    def __init__(self, controller: TrayController) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self._controller = controller
        self.ignore_outside_until = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(400)
        self.setMinimumHeight(180)
        self.setMaximumHeight(520)

        shell = QVBoxLayout(self)
        shell.setContentsMargins(10, 10, 10, 10)

        card = QFrame()
        card.setObjectName("TaskPanel")
        shell.addWidget(card)

        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 120))
        card.setGraphicsEffect(shadow)

        outer = QVBoxLayout(card)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        head = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        title = QLabel(APP_NAME)
        title.setObjectName("PanelTitle")
        hint = QLabel("拖动标题移动 · 失焦 / 点击外部 / Esc 关闭")
        hint.setObjectName("PanelHint")
        title_col.addWidget(title)
        title_col.addWidget(hint)
        head.addLayout(title_col, stretch=1)
        close_btn = QPushButton("×")
        close_btn.setObjectName("CloseBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setFixedSize(28, 28)
        close_btn.clicked.connect(self.hide)
        head.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(head)

        scroll = QScrollArea()
        scroll.setObjectName("TaskScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumHeight(160)
        self._list_host = QWidget()
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(8)
        self._list_layout.addStretch()
        scroll.setWidget(self._list_host)
        outer.addWidget(scroll)

        outer.addWidget(self._build_footer())

        self.setStyleSheet(controller.theme["qss"])
        self._rows: dict[str, TaskRow] = {}
        self._empty_label: QLabel | None = None
        self._drag_offset: QPoint | None = None

        self._outside_filter = OutsideClickFilter(self)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._outside_filter)

    def _build_footer(self) -> QWidget:
        """快捷键配置区 + 版本号，置于面板底部。"""
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)

        sep = QFrame()
        sep.setObjectName("FooterSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        col.addWidget(sep)

        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel("全局快捷键")
        label.setObjectName("SectionLabel")
        row.addWidget(label)
        self._hotkey_btn = HotkeyButton()
        self._hotkey_btn.captured.connect(self._on_hotkey_captured)
        row.addWidget(self._hotkey_btn)
        clear_btn = QPushButton("清除")
        clear_btn.setObjectName("LinkBtn")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.clicked.connect(lambda: self._on_hotkey_captured(""))
        row.addWidget(clear_btn)
        row.addStretch()
        col.addLayout(row)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self._hotkey_status = QLabel("")
        self._hotkey_status.setObjectName("HotkeyStatus")
        self._hotkey_status.setWordWrap(True)
        foot.addWidget(self._hotkey_status, stretch=1)
        version = QLabel(f"{APP_NAME} v{app_version()}")
        version.setObjectName("VersionLabel")
        foot.addWidget(version, alignment=Qt.AlignmentFlag.AlignRight)
        col.addLayout(foot)

        return box

    def sync_hotkey_view(self) -> None:
        """从控制器同步当前快捷键与状态到 UI。"""
        self._hotkey_btn.set_value(self._controller.hotkey_sequence)
        self._hotkey_btn.setEnabled(self._controller.hotkey.available)
        self._hotkey_status.setText(self._controller.hotkey_status)

    def _on_hotkey_captured(self, sequence: str) -> None:
        _ok, msg = self._controller.set_hotkey(sequence)
        self._hotkey_btn.set_value(self._controller.hotkey_sequence)
        self._hotkey_status.setText(msg)

    def event(self, e: QEvent) -> bool:  # noqa: N802
        # 失焦（切到别的窗口/点别处）即关闭，像正常 flyout。
        # 刚弹出的 grace 窗口内忽略，避免与弹出动作自身竞争立即关掉。
        if (
            e.type() == QEvent.Type.WindowDeactivate
            and self.isVisible()
            and time.monotonic() >= self.ignore_outside_until
        ):
            QTimer.singleShot(0, self.hide)
        return super().event(e)

    def hideEvent(self, e: QEvent) -> None:  # noqa: N802
        # 关面板时确保取消录制并释放键盘，避免键盘被 grab 卡住。
        self._hotkey_btn.cancel_capture()
        super().hideEvent(e)

    def keyPressEvent(self, e) -> None:  # noqa: N802
        # Esc：录制中交给按钮取消，否则关闭面板（保留原关闭语义）。
        if e.key() == Qt.Key.Key_Escape:
            if not self._hotkey_btn.cancel_capture():
                self.hide()
            return
        super().keyPressEvent(e)

    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        # 空白处按下即可拖动整窗（子控件如按钮会自行消费事件，不触发拖动）。
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_offset)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(e)

    def refresh(self) -> None:
        # 就地增删 / 更新行（按 task id 复用），避免每次刷新重建行——
        # 那会把用户展开的原图、滚动位置一并清掉。
        tasks = self._controller.tasks.list_tasks()
        wanted = {t.id for t in tasks}

        for tid in list(self._rows):
            if tid not in wanted:
                row = self._rows.pop(tid)
                self._list_layout.removeWidget(row)
                row.deleteLater()

        if not tasks:
            if self._empty_label is None:
                empty = QLabel("暂无任务")
                empty.setObjectName("PanelHint")
                empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
                empty.setStyleSheet("padding: 28px 0;")
                self._list_layout.insertWidget(0, empty)
                self._empty_label = empty
            self.adjustSize()
            return

        if self._empty_label is not None:
            self._list_layout.removeWidget(self._empty_label)
            self._empty_label.deleteLater()
            self._empty_label = None

        for idx, task in enumerate(tasks):
            row = self._rows.get(task.id)
            if row is None:
                row = TaskRow(task, self._controller)
                self._rows[task.id] = row
            else:
                row.refresh(task)
            self._list_layout.insertWidget(idx, row)
        self.adjustSize()

    def show_near_anchor(self, anchor: QPoint | None, tray: QSystemTrayIcon) -> None:
        """贴着光标弹出：光标在屏幕上半屏则朝下，下半屏则朝上。"""
        self.refresh()
        self.sync_hotkey_view()
        self.adjustSize()

        cursor = anchor if anchor is not None else QCursor.pos()
        screen = QGuiApplication.screenAt(cursor) or QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)

        w, h = self.width(), self.height()
        gap = 12
        x = cursor.x() - w // 2

        if cursor.y() < avail.center().y():
            # 光标在上半屏：面板在光标下方
            y = cursor.y() + gap
        else:
            # 光标在下半屏：面板在光标上方
            y = cursor.y() - h - gap

        x = max(avail.left() + 8, min(x, avail.right() - w - 8))
        y = max(avail.top() + 8, min(y, avail.bottom() - h - 8))

        self.ignore_outside_until = time.monotonic() + 0.4
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()


class IpcBridge(QObject):
    """把 IPC 线程请求切到 Qt 主线程。"""

    capture_requested = Signal()
    quit_requested = Signal()


class TrayController(QObject):
    """托盘、任务、IPC 编排。"""

    tasks_changed = Signal()

    def __init__(self, theme: dict) -> None:
        super().__init__()
        self.theme = theme
        self.status_colors = theme["status_colors"]
        self.tasks = TaskManager()
        self.runner = JobRunner(self.tasks)
        self._stop = threading.Event()
        self._ipc_bridge = IpcBridge()
        self._ipc_bridge.capture_requested.connect(self.start_capture)
        self._ipc_bridge.quit_requested.connect(self.quit_app)

        self.tasks.add_listener(self._on_tasks_changed)
        self.runner.start()

        self._config = load_config()
        self.hotkey = GlobalHotkey(self)
        self.hotkey.activated.connect(self.start_capture)
        self.hotkey_sequence = ""
        self.hotkey_status = ""
        self._init_hotkey()

        self._panel = TaskPanel(self)
        self._tray = QSystemTrayIcon(make_tray_icon())
        self._tray.setToolTip("OCR工具 · 单击截图识别")

        menu = QMenu()
        act_panel = QAction("显示主窗口", self._tray)
        act_panel.triggered.connect(self.show_panel)
        menu.addAction(act_panel)
        act_capture = QAction("新截图识别", self._tray)
        act_capture.triggered.connect(self.start_capture)
        menu.addAction(act_capture)
        menu.addSeparator()
        act_quit = QAction("退出", self._tray)
        act_quit.triggered.connect(self.quit_app)
        menu.addAction(act_quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

        ipc_thread = threading.Thread(
            target=self._run_ipc,
            name="ocrtool-ipc",
            daemon=True,
        )
        ipc_thread.start()
        log.info("托盘应用已启动 (pid=%s)，开始后台预热模型", os.getpid())

    def _on_tasks_changed(self) -> None:
        self.tasks_changed.emit()

    @Slot()
    def _refresh_ui(self) -> None:
        if self._panel.isVisible():
            self._panel.refresh()
        active = [
            t
            for t in self.tasks.list_tasks()
            if t.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
        ]
        if not active:
            self._tray.setToolTip("OCR工具 截图识别（空闲）")
        else:
            t = active[0]
            self._tray.setToolTip(
                f"OCR工具 · {t.title} {t.progress:.0f}% {t.detail or ''}".strip()
            )

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # 单击（含双击 / 中键）触发新截图；主窗口改从右键菜单打开。
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.MiddleClick,
        ):
            self.start_capture()

    def _init_hotkey(self) -> None:
        """启动时按配置注册全局快捷键（首次运行用默认组合）。"""
        seq = str(self._config.get("hotkey", DEFAULT_HOTKEY) or "")
        if not self.hotkey.available:
            self.hotkey_sequence = seq
            self.hotkey_status = "当前桌面环境不支持全局快捷键"
            return
        ok, msg = self.hotkey.apply(seq)
        self.hotkey_sequence = self.hotkey.current
        self.hotkey_status = msg if ok else f"快捷键未生效：{msg}"

    def set_hotkey(self, sequence: str) -> tuple[bool, str]:
        """更新全局快捷键并持久化。返回 (成功, 说明) 供 UI 展示。"""
        ok, msg = self.hotkey.apply(sequence)
        if ok:
            self.hotkey_sequence = self.hotkey.current
            self._config["hotkey"] = self.hotkey_sequence
            save_config(self._config)
        self.hotkey_status = msg
        return ok, msg

    def show_panel(self) -> None:
        # 始终以当前光标为锚点（上半屏朝下弹、下半屏朝上弹）。
        self._panel.show_near_anchor(QCursor.pos(), self._tray)

    @Slot()
    def start_capture(self) -> None:
        if not self.runner.model_ready:
            log.info("服务尚未就绪，请稍候…")
            notify(APP_NAME, "服务启动中，请稍后再截图")
            self.show_panel()
            return
        log.info("开始区域截图…")
        try:
            path = capture_region(persist=True)
        except CaptureCancelled:
            log.info("用户取消截图")
            return
        except CaptureError as exc:
            log.warning("截图失败: %s", exc)
            notify(f"{APP_NAME} 截图失败", str(exc), urgency="critical")
            return

        task = self.tasks.create(
            TaskKind.OCR,
            title=f"OCR {path.name}",
            image_path=str(path),
        )
        log.info("任务[%s] 已创建: %s", task.id, path)
        self.runner.enqueue_ocr(task.id)
        notify(APP_NAME, "已开始识别，完成后自动复制（点托盘图标看进度）")

    def quit_app(self) -> None:
        if self._stop.is_set():
            return
        log.info("正在退出托盘应用")
        self._stop.set()
        self.hotkey.shutdown()
        self.runner.stop()
        self._panel.hide()
        self._tray.hide()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._panel._outside_filter)
            app.quit()

    def _run_ipc(self) -> None:
        serve_ipc(self._handle_ipc, stop_flag=self._stop.is_set)

    def _handle_ipc(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "ping":
            return {"ok": True, "app": "tray", "proto": 2}
        if cmd == "status":
            return {
                "ok": True,
                "app": "tray",
                "proto": 2,
                "running": True,
                "tasks": [t.to_dict() for t in self.tasks.list_tasks()],
            }
        if cmd == "capture":
            self._ipc_bridge.capture_requested.emit()
            return {"ok": True, "app": "tray", "message": "已提交截图"}
        if cmd == "quit":
            self._ipc_bridge.quit_requested.emit()
            return {"ok": True}
        return {"ok": False, "error": f"未知命令: {cmd}"}


def run_tray_app(*, capture_on_start: bool = True) -> int:
    """启动托盘：先预热模型；就绪后再按需自动截图。"""
    # 优先 XWayland(xcb)：原生 Wayland 下应用无法 move() 自身窗口、QCursor.pos()
    # 不准、也收不到失焦事件，导致「贴光标弹出 / 失焦关闭」失效。xcb 下这些按
    # X11 语义正常工作；xcb 不可用时回退 wayland。用户可用 QT_QPA_PLATFORM 覆盖。
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb;wayland")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("OCR工具")

    theme = build_theme(app)
    controller = TrayController(theme)
    controller.tasks_changed.connect(controller._refresh_ui)

    if capture_on_start:

        def _capture_when_ready() -> None:
            if controller.runner.model_ready:
                controller.start_capture()
                return
            QTimer.singleShot(400, _capture_when_ready)

        QTimer.singleShot(500, _capture_when_ready)

    return app.exec()
