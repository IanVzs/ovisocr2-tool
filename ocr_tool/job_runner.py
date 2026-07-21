"""后台串行执行：启动预热（拉起 llama-server）+ OCR 任务。"""

from __future__ import annotations

import logging
import queue
import threading
import time

from ocr_tool.clipboard import ClipboardError, copy_text
from ocr_tool.notify import notify
from ocr_tool.ocr_engine import HunyuanOCREngine
from ocr_tool.tasks import TaskKind, TaskManager, TaskStatus

log = logging.getLogger("ocrtool")

_WARMUP = "__warmup__"
APP_NAME = "OCR工具"


class JobRunner:
    def __init__(self, tasks: TaskManager) -> None:
        self.tasks = tasks
        self.engine = HunyuanOCREngine()
        self._q: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="ocr-jobs", daemon=True)
        self._started = False
        self._ready = threading.Event()
        self._warmup_lock = threading.Lock()

    @property
    def model_ready(self) -> bool:
        return self._ready.is_set()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()
        self._q.put(_WARMUP)

    def enqueue_ocr(self, task_id: str) -> None:
        self._q.put(task_id)

    def stop(self) -> None:
        self._q.put(None)

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout=timeout)

    def _loop(self) -> None:
        while True:
            task_id = self._q.get()
            if task_id is None:
                break
            if task_id == _WARMUP:
                self._warmup()
                continue
            if not self._ready.is_set():
                self._warmup()
            self._run_ocr_task(task_id)
        self.engine.shutdown()

    def _warmup(self) -> None:
        with self._warmup_lock:
            if self._ready.is_set():
                return
            svc = self.tasks.create(TaskKind.SERVICE, title="启动 HunyuanOCR 服务")
            log.info("任务[%s] 启动 llama-server", svc.id)
            self.tasks.update(
                svc.id,
                status=TaskStatus.RUNNING,
                progress=0,
                detail="连接/拉起本地服务…",
            )
            try:

                def on_prog(pct: float, detail: str) -> None:
                    self.tasks.update(
                        svc.id, status=TaskStatus.RUNNING, progress=pct, detail=detail
                    )

                self.engine.ensure_ready(on_progress=on_prog)
                self.tasks.update(
                    svc.id, status=TaskStatus.DONE, progress=100, detail="服务已就绪"
                )
                self._ready.set()
                log.info("模型服务就绪，可开始截图识别")
                notify(APP_NAME, "服务已就绪，可开始截图识别")
            except Exception as exc:  # noqa: BLE001
                self.tasks.update(
                    svc.id, status=TaskStatus.FAILED, error=str(exc), detail="启动失败"
                )
                log.exception("服务启动失败: %s", exc)
                notify(f"{APP_NAME} 启动失败", str(exc)[:200], urgency="critical")

    def _run_ocr_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task or not task.image_path:
            return

        log.info("任务[%s] OCR 开始: %s", task_id, task.image_path)
        self.tasks.update(
            task_id, status=TaskStatus.RUNNING, progress=1, detail="排队执行…"
        )
        if not self._ready.is_set():
            try:
                self._warmup()
            except Exception as exc:  # noqa: BLE001
                self.tasks.update(
                    task_id,
                    status=TaskStatus.FAILED,
                    error=f"服务未就绪: {exc}",
                    detail="失败",
                )
                return

        try:

            def on_prog(pct: float, detail: str) -> None:
                self.tasks.update(
                    task_id, status=TaskStatus.RUNNING, progress=pct, detail=detail
                )

            text = self.engine.parse_image(task.image_path, on_progress=on_prog)
            self.tasks.update(
                task_id,
                status=TaskStatus.DONE,
                progress=100,
                detail="完成",
                result_text=text,
            )
            log.info("任务[%s] OCR 完成，%d 字符", task_id, len(text))

            if text.strip():
                try:
                    copy_text(text)
                    preview = text.replace("\n", " ").strip()
                    if len(preview) > 80:
                        preview = preview[:80] + "…"
                    notify(f"{APP_NAME} 完成", f"已复制到剪贴板\n{preview}")
                    log.info("任务[%s] 已写入剪贴板", task_id)
                except ClipboardError as exc:
                    notify(f"{APP_NAME} 完成", "识别完成，但写入剪贴板失败")
                    log.warning("任务[%s] 剪贴板失败: %s", task_id, exc)
            else:
                notify(APP_NAME, "未识别到文本")
                log.info("任务[%s] 空结果", task_id)
        except Exception as exc:  # noqa: BLE001
            self.tasks.update(
                task_id, status=TaskStatus.FAILED, error=str(exc), detail="失败"
            )
            notify(f"{APP_NAME} 解析失败", str(exc)[:200], urgency="critical")
            log.exception("任务[%s] OCR 失败: %s", task_id, exc)

        time.sleep(0.01)
