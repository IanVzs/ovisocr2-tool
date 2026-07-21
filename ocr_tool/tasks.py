"""简单任务管理：最多展示 9 条。"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


MAX_VISIBLE_TASKS = 9


class TaskKind(str, Enum):
    SERVICE = "service"
    OCR = "ocr"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    id: str
    kind: TaskKind
    title: str
    status: TaskStatus = TaskStatus.QUEUED
    progress: float = 0.0  # 0..100
    detail: str = ""
    error: str = ""
    image_path: str | None = None
    result_text: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def can_copy(self) -> bool:
        return (
            self.kind == TaskKind.OCR
            and self.status == TaskStatus.DONE
            and bool(self.result_text)
        )

    @property
    def can_view_image(self) -> bool:
        return self.kind == TaskKind.OCR and bool(self.image_path)

    @property
    def show_action_buttons(self) -> bool:
        """下载任务不显示「查看原图 / 复制文本」。"""
        return self.kind == TaskKind.OCR

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "title": self.title,
            "status": self.status.value,
            "progress": self.progress,
            "detail": self.detail,
            "error": self.error,
            "image_path": self.image_path,
            "has_text": bool(self.result_text),
            "can_copy": self.can_copy,
            "can_view_image": self.can_view_image,
            "show_action_buttons": self.show_action_buttons,
        }


Listener = Callable[[], None]


class TaskManager:
    """线程安全任务表；变更时通知 UI。"""

    def __init__(self, max_visible: int = MAX_VISIBLE_TASKS) -> None:
        self.max_visible = max_visible
        self._lock = threading.RLock()
        self._tasks: list[Task] = []
        self._listeners: list[Listener] = []

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    def list_tasks(self) -> list[Task]:
        with self._lock:
            return list(self._tasks)

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            for t in self._tasks:
                if t.id == task_id:
                    return t
        return None

    def create(
        self,
        kind: TaskKind,
        title: str,
        *,
        image_path: str | None = None,
    ) -> Task:
        task = Task(
            id=uuid.uuid4().hex[:10],
            kind=kind,
            title=title,
            image_path=image_path,
        )
        with self._lock:
            self._tasks.insert(0, task)
            self._trim_locked()
        self._notify()
        return task

    def update(
        self,
        task_id: str,
        *,
        status: TaskStatus | None = None,
        progress: float | None = None,
        detail: str | None = None,
        error: str | None = None,
        result_text: str | None = None,
    ) -> None:
        with self._lock:
            task = next((t for t in self._tasks if t.id == task_id), None)
            if not task:
                return
            if status is not None:
                task.status = status
            if progress is not None:
                task.progress = max(0.0, min(100.0, float(progress)))
            if detail is not None:
                task.detail = detail
            if error is not None:
                task.error = error
            if result_text is not None:
                task.result_text = result_text
            task.updated_at = time.time()
        self._notify()

    def _trim_locked(self) -> None:
        """超过上限时优先丢掉最旧的已结束任务。"""
        while len(self._tasks) > self.max_visible:
            ended = [
                i
                for i, t in enumerate(self._tasks)
                if t.status
                in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED)
            ]
            if ended:
                # 列表新在前，最旧结束任务在末尾附近
                drop_i = ended[-1]
            else:
                drop_i = len(self._tasks) - 1
            self._tasks.pop(drop_i)
