"""
Task Queue System — zarządzanie kolejką zadań w tle.
Pozwala na nieblokujące wykonywanie ciężkich operacji (import PDF, wektoryzacja, analiza)
z limitem N równoczesnych tasków.
"""

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger("ai_analiza")


# ============================================================
# ENUMS & DATACLASSES
# ============================================================


class TaskStatus(Enum):
    """Status zadania w kolejce."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """Reprezentacja zadania w kolejce."""

    id: str
    kind: str
    label: str
    status: TaskStatus
    started_at: float | None = None
    finished_at: float | None = None
    updated_at: float = field(default_factory=time.time)
    progress_pct: int = 0
    done: int = 0
    total: int | None = None
    current_item: str | None = None
    error: str | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Konwertuj Task na dict (dla JSON API)."""
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        """Utwórz Task z dicta."""
        data = dict(data)
        if isinstance(data.get("status"), str):
            data["status"] = TaskStatus(data["status"])
        return cls(**data)


@dataclass
class TaskQueueConfig:
    """Konfiguracja kolejki zadań."""

    max_concurrent: int = 2
    history_limit: int = 50
    executor_timeout: int = 3600


# ============================================================
# STATUS MAPPINGS FOR SQLITE
# ============================================================

_STATUS_TO_DB = {
    TaskStatus.QUEUED: "PENDING",
    TaskStatus.RUNNING: "PROCESSING",
    TaskStatus.COMPLETED: "COMPLETED",
    TaskStatus.FAILED: "FAILED",
    TaskStatus.CANCELLED: "CANCELLED"
}

_STATUS_FROM_DB = {
    "PENDING": TaskStatus.QUEUED,
    "PROCESSING": TaskStatus.RUNNING,
    "COMPLETED": TaskStatus.COMPLETED,
    "FAILED": TaskStatus.FAILED,
    "CANCELLED": TaskStatus.CANCELLED
}


class TaskQueue:
    """
    Kolejka zadań z ThreadPoolExecutor i SQLite.
    """

    def __init__(self, config: TaskQueueConfig | None = None, db_path: str = "tasks.db"):
        self.config = config or TaskQueueConfig()
        self.db_path = db_path
        self._executor = ThreadPoolExecutor(max_workers=self.config.max_concurrent)

        self._init_db()
        self._lock = threading.Lock()
        self._semaphore = threading.Semaphore(self.config.max_concurrent)

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    filename TEXT,
                    status TEXT,
                    result TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def _task_from_row(self, row) -> Task:
        task_id, filename, status_str, result_json, created_at, updated_at = row
        data = json.loads(result_json)
        status = _STATUS_FROM_DB.get(status_str, TaskStatus.QUEUED)
        return Task(
            id=task_id,
            kind=data.get("kind", ""),
            label=data.get("label", ""),
            status=status,
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            updated_at=data.get("updated_at", time.time()),
            progress_pct=data.get("progress_pct", 0),
            done=data.get("done", 0),
            total=data.get("total"),
            current_item=data.get("current_item"),
            error=data.get("error"),
            meta=data.get("meta", {})
        )

    def _task_to_row(self, task: Task) -> tuple:
        task_dict = {
            "kind": task.kind,
            "label": task.label,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "updated_at": task.updated_at,
            "progress_pct": task.progress_pct,
            "done": task.done,
            "total": task.total,
            "current_item": task.current_item,
            "error": task.error,
            "meta": task.meta
        }
        result_json = json.dumps(task_dict, ensure_ascii=False)
        filename = task.meta.get("folder", "") if task.meta else ""
        status_str = _STATUS_TO_DB.get(task.status, "PENDING")
        created_at_str = datetime.fromtimestamp(task.started_at or time.time()).isoformat()
        updated_at_str = datetime.fromtimestamp(task.updated_at).isoformat()
        return (task.id, filename, status_str, result_json, created_at_str, updated_at_str)

    def _save_task_to_db(self, task: Task):
        row = self._task_to_row(task)
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO tasks (id, filename, status, result, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                row
            )
            conn.commit()
        finally:
            conn.close()

    def _update_task_in_db(self, task: Task):
        task_dict = {
            "kind": task.kind,
            "label": task.label,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "updated_at": task.updated_at,
            "progress_pct": task.progress_pct,
            "done": task.done,
            "total": task.total,
            "current_item": task.current_item,
            "error": task.error,
            "meta": task.meta
        }
        result_json = json.dumps(task_dict, ensure_ascii=False)
        status_str = _STATUS_TO_DB.get(task.status, "PENDING")
        updated_at_str = datetime.fromtimestamp(task.updated_at).isoformat()
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE tasks SET status = ?, result = ?, updated_at = ? WHERE id = ?",
                (status_str, result_json, updated_at_str, task.id)
            )
            conn.commit()
        finally:
            conn.close()

    def _get_task_from_db(self, task_id: str) -> Task | None:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, filename, status, result, created_at, updated_at FROM tasks WHERE id = ?",
                (task_id,)
            )
            row = cursor.fetchone()
            if row:
                return self._task_from_row(row)
        finally:
            conn.close()
        return None

    def submit(
        self,
        kind: str,
        label: str,
        work_fn: Callable,
        meta: dict | None = None,
    ) -> tuple[bool, Task]:
        task_id = _new_task_id(kind)
        now = time.time()
        task = Task(
            id=task_id,
            kind=kind,
            label=label,
            status=TaskStatus.QUEUED,
            started_at=None,
            finished_at=None,
            updated_at=now,
            meta=meta or {},
        )

        with self._lock:
            self._save_task_to_db(task)
            running_tasks = self.get_running()
            queued = len(running_tasks) >= self.config.max_concurrent
            logger.info(
                f"Task submitted: {task_id} ({kind}) — {'queued' if queued else 'will run soon'}"
            )

        self._executor.submit(self._worker_wrapper, task_id, work_fn)
        return queued, self._task_snapshot(task)

    def _worker_wrapper(self, task_id: str, work_fn: Callable) -> None:
        try:
            self._semaphore.acquire()
            task = None

            try:
                with self._lock:
                    task = self._get_task_from_db(task_id)
                    if not task:
                        logger.warning(f"Task {task_id} nie znaleziony w bazie")
                        return

                    if task.status == TaskStatus.CANCELLED:
                        logger.info(f"Task {task_id} anulowany przed uruchomieniem")
                        return

                    task.status = TaskStatus.RUNNING
                    task.started_at = time.time()
                    task.updated_at = time.time()
                    self._update_task_in_db(task)
                    logger.info(f"Task started: {task_id}")

                work_fn(task_id)

                with self._lock:
                    task = self._get_task_from_db(task_id)
                    if task:
                        if task.status != TaskStatus.CANCELLED:
                            task.status = TaskStatus.COMPLETED
                        task.finished_at = time.time()
                        task.updated_at = time.time()
                        self._update_task_in_db(task)
                        logger.info(f"Task completed: {task_id}")

            except Exception as e:
                error_msg = str(e)[:200]
                logger.error(f"Task {task_id} failed: {error_msg}")
                with self._lock:
                    task = self._get_task_from_db(task_id)
                    if task:
                        if task.status != TaskStatus.CANCELLED:
                            task.status = TaskStatus.FAILED
                        task.error = error_msg
                        task.finished_at = time.time()
                        task.updated_at = time.time()
                        self._update_task_in_db(task)

        finally:
            self._semaphore.release()

    def update_task(self, task_id: str, **updates) -> None:
        safe_updates = {
            k: v
            for k, v in updates.items()
            if k not in {"id", "status", "kind", "label", "started_at", "finished_at"}
        }

        with self._lock:
            task = self._get_task_from_db(task_id)
            if task:
                task.updated_at = time.time()
                for k, v in safe_updates.items():
                    setattr(task, k, v)
                self._update_task_in_db(task)

    def get_task(self, task_id: str) -> Task | None:
        return self._get_task_from_db(task_id)

    def get_status(self) -> dict:
        return {
            "queued": self.get_queued(),
            "running": self.get_running(),
            "finished": self.get_finished(),
        }

    def get_queued(self) -> list[Task]:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, filename, status, result, created_at, updated_at FROM tasks WHERE status = 'PENDING' ORDER BY updated_at ASC"
            )
            rows = cursor.fetchall()
            return [self._task_from_row(r) for r in rows]
        finally:
            conn.close()

    def get_running(self) -> list[Task]:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, filename, status, result, created_at, updated_at FROM tasks WHERE status = 'PROCESSING' ORDER BY updated_at ASC"
            )
            rows = cursor.fetchall()
            return [self._task_from_row(r) for r in rows]
        finally:
            conn.close()

    def get_finished(self) -> list[Task]:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, filename, status, result, created_at, updated_at FROM tasks WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED') ORDER BY updated_at DESC LIMIT ?",
                (self.config.history_limit,)
            )
            rows = cursor.fetchall()
            return [self._task_from_row(r) for r in rows]
        finally:
            conn.close()

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            task = self._get_task_from_db(task_id)
            if not task:
                return False

            if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                task.status = TaskStatus.CANCELLED
                task.finished_at = time.time()
                task.updated_at = time.time()
                self._update_task_in_db(task)
                logger.info(f"Task cancelled: {task_id}")
                return True

        return False

    def _task_snapshot(self, task: Task | None) -> Task | None:
        if not task:
            return None
        snapshot = Task(
            id=task.id,
            kind=task.kind,
            label=task.label,
            status=task.status,
            started_at=task.started_at,
            finished_at=task.finished_at,
            updated_at=task.updated_at,
            progress_pct=task.progress_pct,
            done=task.done,
            total=task.total,
            current_item=task.current_item,
            error=task.error,
            meta=dict(task.meta) if task.meta else {},
        )
        return snapshot

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)


# ============================================================
# GLOBAL INSTANCE & HELPERS
# ============================================================

_global_task_queue: TaskQueue | None = None
_global_queue_lock = threading.Lock()


def _new_task_id(kind: str) -> str:
    """Utwórz unikatowe ID dla taska."""
    raw = f"{kind}:{time.time_ns()}:{threading.get_ident()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def get_task_queue() -> TaskQueue:
    """Pobierz globalną instancję TaskQueue (lazy init)."""
    global _global_task_queue
    if _global_task_queue is None:
        with _global_queue_lock:
            if _global_task_queue is None:
                _global_task_queue = TaskQueue()
    return _global_task_queue


def submit_task(
    kind: str,
    label: str,
    work_fn: Callable,
    meta: dict | None = None,
) -> tuple[bool, Task]:
    """Submit zadanie do kolejki."""
    return get_task_queue().submit(kind, label, work_fn, meta)


def get_task(task_id: str) -> Task | None:
    """Pobierz status taska."""
    return get_task_queue().get_task(task_id)


def update_task(task_id: str, **updates) -> None:
    """Aktualizuj progress taska (wywoływane z work_fn)."""
    get_task_queue().update_task(task_id, **updates)


def get_task_status() -> dict:
    """Pobierz status wszystkich tasków."""
    return get_task_queue().get_status()


def cancel_task(task_id: str) -> bool:
    """Anuluj task."""
    return get_task_queue().cancel_task(task_id)
