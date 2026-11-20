"""Windows 区域截图：Qt 蒙层框选 + mss 抓屏。

Linux 上仍用外部 spectacle（见 capture.py）；本模块只在 Windows 时导入。

坐标方案（关键）：
- 框选 UI 用 Qt 蒙层，鼠标事件的局部坐标只用于画橡皮筋框。
- 截取位置用 Win32 GetCursorPos 取**物理像素坐标**，直接喂给 mss（同样是
  物理坐标系）。全程不碰 Qt 的逻辑坐标 / DPR 换算 —— 多显示器 + 每屏不同
  DPI 下，Qt 逻辑坐标与物理坐标的换算极易错位（已踩坑），物理坐标直连最稳。
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import QApplication, QRubberBand, QWidget

from ocr_tool.capture import CaptureCancelled, CaptureError
from ocr_tool.paths import CAPTURE_DIR, ensure_runtime_dir

log = logging.getLogger("ocrtool")


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _cursor_pos_physical() -> tuple[int, int]:
    """GetCursorPos：返回物理像素坐标（Win32 虚拟桌面坐标系），与 mss 一致。"""
    p = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


class _RegionSelector(QWidget):
    """覆盖整个虚拟桌面的半透明窗口：拖拽画框，松开完成，Esc 取消。"""

    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._origin_local: QPoint | None = None  # 画橡皮筋用（窗口局部坐标）
        self._origin_phys: tuple[int, int] | None = None  # 截取起点（物理坐标）
        self._rect_phys: tuple[int, int, int, int] | None = None  # x,y,w,h 物理
        self._done = False
        self._band = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self._band.setStyleSheet(
            "QRubberBand { border: 2px dashed #38BDF8; background: rgba(56,189,248,0.15); }"
        )

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        self.hide()
        self.finished.emit()

    def show_fullscreen(self) -> None:
        virtual = QGuiApplication.primaryScreen().virtualGeometry()
        self.setGeometry(virtual)
        self.show()
        self.raise_()
        self.activateWindow()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 90))
        tip = self.rect()
        tip.setHeight(44)
        painter.fillRect(tip, QColor(15, 23, 42, 220))
        painter.setPen(QColor("#E2E8F0"))
        font = painter.font()
        font.setPointSize(11)
        painter.setFont(font)
        painter.drawText(
            tip, Qt.AlignmentFlag.AlignCenter,
            "拖动鼠标框选识别区域 · Esc 取消",
        )
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin_local = event.position().toPoint()
            self._origin_phys = _cursor_pos_physical()
            self._band.setGeometry(QRect(self._origin_local, self._origin_local))
            self._band.show()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._origin_local is not None:
            p = event.position().toPoint()
            self._band.setGeometry(
                QRect(
                    min(self._origin_local.x(), p.x()),
                    min(self._origin_local.y(), p.y()),
                    abs(p.x() - self._origin_local.x()),
                    abs(p.y() - self._origin_local.y()),
                )
            )

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._origin_phys is None:
            return
        end = _cursor_pos_physical()
        start = self._origin_phys
        self._origin_local = None
        self._origin_phys = None
        self._band.hide()
        w, h = abs(end[0] - start[0]), abs(end[1] - start[1])
        if w < 4 or h < 4:
            self._rect_phys = None
        else:
            self._rect_phys = (min(start[0], end[0]), min(start[1], end[1]), w, h)
        self._finish()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._rect_phys = None
            self._finish()


def _grab_region(rect: tuple[int, int, int, int], path: Path) -> None:
    """用 mss 按物理坐标直接抓取，返回非黑像素数用于诊断。"""
    import mss
    from PIL import Image

    x, y, w, h = rect
    path.parent.mkdir(parents=True, exist_ok=True)
    with mss.mss() as sct:
        raw = sct.grab({"left": x, "top": y, "width": w, "height": h})
        img = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb, "raw", "RGB")
        img.save(str(path), "PNG")
    small = img.resize((32, 32))
    non_black = sum(1 for p in small.getdata() if sum(p) > 24)
    log.info("抓屏(mss物理坐标): rect=(%s,%s,%sx%s) 图=%sx%s 非黑=%s/1024",
             x, y, w, h, img.width, img.height, non_black)
    return non_black


def capture_region_win(*, persist: bool = True) -> Path:
    """Windows 框选截图，成功返回 PNG 路径；取消抛 CaptureCancelled。"""
    app = QApplication.instance()
    if app is None:
        raise CaptureError("截图需要 Qt 应用已启动")

    selector = _RegionSelector()
    from PySide6.QtCore import QEventLoop

    loop = QEventLoop()
    selector.finished.connect(loop.quit)
    selector.show_fullscreen()
    loop.exec()

    rect_phys = selector._rect_phys
    selector.deleteLater()
    if rect_phys is None:
        raise CaptureCancelled()

    if persist:
        ensure_runtime_dir()
        out = CAPTURE_DIR / (
            f"cap-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000}.png"
        )
    else:
        import tempfile

        fd, name = tempfile.mkstemp(prefix="ocr-tool-", suffix=".png")
        os.close(fd)
        out = Path(name)

    try:
        non_black = _grab_region(rect_phys, out)
    except Exception as exc:  # noqa: BLE001
        out.unlink(missing_ok=True)
        raise CaptureError(f"抓屏失败: {exc}") from exc
    if not out.is_file() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        raise CaptureError("抓屏结果为空")
    if non_black == 0:
        log.warning("抓屏结果为纯黑！显示器可能锁屏/休眠或驱动不支持抓屏。")
    log.info("区域截图完成: %s", out)
    return out
