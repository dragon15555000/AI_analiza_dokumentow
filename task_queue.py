# -*- coding: utf-8 -*-
"""
Task Queue System — zarządzanie kolejką zadań w tle.
Pozwala na nieblokujące wykonywanie ciężkich operacji (import PDF, wektoryzacja, analiza)
z limitem N równoczesnych tasków.
"""

import time
import logging
import threading
import hashlib
from enum import Enum
from collections import deque
from dataclasses import dataclass, field, asdict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

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
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    updated_at: float = field(default_factory=time.time)
    progress_pct: int = 0
    done: int = 0
    total: Optional[int] = None
    current_item: Optional[str] = None
    error: Optional[str] = None
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
# TASK QUEUE — główna klasa
# ============================================================

class TaskQueue:
    """
    Kolejka zadań z ThreadPoolExecutor.
    - Queued: czekające na wykonanie
    - Running: aktualnie się wykonujące (max N)
    - Finished: skończone/błąd/anulowane
    """

    def __init__(self, config: Optional[TaskQueueConfig] = None):
        self.config = config or TaskQueueConfig()
        self._executor = ThreadPoolExecutor(max_workers=self.config.max_concurrent)

        # Státus tasków
        self._queued_tasks: deque[Task] = deque()  # FIFO
        self._running_tasks: dict[str, Task] = {}  # task_id -> Task
        self._finished_tasks: deque[Task] = deque(maxlen=self.config.history_limit)

        # Thread safety
        self._lock = threading.Lock()
        self._semaphore = threading.Semaphore(self.config.max_concurrent)

    def submit(
        self,
        kind: str,
        label: str,
        work_fn: Callable,
        meta: Optional[dict] = None,
    ) -> tuple[bool, Task]:
        """
        Dodaj zadanie do kolejki.

        Args:
            kind: Typ zadania (e.g., 'document_import', 'hybrid_search')
            label: Opis dla UI
            work_fn: Funkcja do wykonania (będzie wywoływana z task_id)
            meta: Metadane (folder, query, itp.)

        Returns:
            (queued, task) - queued: czy czeka, task: Task object
        """
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
            self._queued_tasks.append(task)
            queued = len(self._running_tasks) >= self.config.max_concurrent
            logger.info(
                f"Task submitted: {task_id} ({kind}) — {'queued' if queued else 'will run soon'}"
            )

        # Uruchom w tle (worker wczyta z queued_tasks gdy będzie miejsce)
        self._executor.submit(self._worker_wrapper, task_id, work_fn)

        return queued, self._task_snapshot(task)

    def _worker_wrapper(self, task_id: str, work_fn: Callable) -> None:
        """
        Worker thread — wczeka na miejsce, przeniesie task z queued → running,
        wykonaj pracę, przenieś do finished.
        """
        try:
            # Czekaj aż będzie miejsce (semaphore)
            self._semaphore.acquire()
            task = None

            try:
                # Przeniesz z queued → running
                with self._lock:
                    # Znajdź task w queued
                    task = None
                    for t in list(self._queued_tasks):
                        if t.id == task_id:
                            task = t
                            break

                    if not task:
                        logger.warning(f"Task {task_id} nie znaleziony w queue")
                        return

                    self._queued_tasks.remove(task)
                    task.status = TaskStatus.RUNNING
                    task.started_at = time.time()
                    task.updated_at = time.time()
                    self._running_tasks[task_id] = task
                    logger.info(f"Task started: {task_id}")

                # Wykonaj pracę
                work_fn(task_id)

                # Oznacz jako COMPLETED
                with self._lock:
                    if task_id in self._running_tasks:
                        task = self._running_tasks.pop(task_id)
                        task.status = TaskStatus.COMPLETED
                        task.finished_at = time.time()
                        task.updated_at = time.time()
                        self._finished_tasks.append(task)
                        logger.info(f"Task completed: {task_id}")

            except Exception as e:
                # Błąd podczas wykonania
                error_msg = str(e)[:200]
                logger.error(f"Task {task_id} failed: {error_msg}")
                with self._lock:
                    if task_id in self._running_tasks:
                        task = self._running_tasks.pop(task_id)
                        task.status = TaskStatus.FAILED
                        task.error = error_msg
                        task.finished_at = time.time()
                        task.updated_at = time.time()
                        self._finished_tasks.append(task)

        finally:
            self._semaphore.release()

    def update_task(self, task_id: str, **updates) -> None:
        """
        Aktualizuj progress/status zadania (wywoływane z work_fn).

        Args:
            task_id: ID zadania
            **updates: {progress_pct, done, total, current_item, ...}
        """
        # Filtruj niebezpieczne pola
        safe_updates = {
            k: v for k, v in updates.items()
            if k not in {"id", "status", "kind", "label", "started_at", "finished_at"}
        }

        with self._lock:
            # Szukaj w running_tasks (najbardziej prawdopodobne)
            task = self._running_tasks.get(task_id)

            # Jeśli nie znaleziono w running, szukaj w queued (rzadko)
            if not task:
                for t in self._queued_tasks:
                    if t.id == task_id:
                        task = t
                        break

            # Ostatnia szansa: szukaj w finished (edge case: update zaraz po finish)
            if not task:
                for t in self._finished_tasks:
                    if t.id == task_id:
                        task = t
                        break

            # Aktualizuj jeśli znaleziono
            if task:
                task.updated_at = time.time()
                for k, v in safe_updates.items():
                    setattr(task, k, v)

    def get_task(self, task_id: str) -> Optional[Task]:
        """Pobierz task po ID (snapshot)."""
        with self._lock:
            if task_id in self._running_tasks:
                return self._task_snapshot(self._running_tasks[task_id])
            for task in self._queued_tasks:
                if task.id == task_id:
                    return self._task_snapshot(task)
            for task in self._finished_tasks:
                if task.id == task_id:
                    return self._task_snapshot(task)
        return None

    def get_status(self) -> dict:
        """Pobierz status wszystkich tasków."""
        with self._lock:
            return {
                "queued": [self._task_snapshot(t) for t in self._queued_tasks],
                "running": [self._task_snapshot(t) for t in self._running_tasks.values()],
                "finished": [self._task_snapshot(t) for t in reversed(self._finished_tasks)],
            }

    def get_queued(self) -> list[Task]:
        """Lista czekających tasków."""
        with self._lock:
            return [self._task_snapshot(t) for t in self._queued_tasks]

    def get_running(self) -> list[Task]:
        """Lista uruchomionych tasków."""
        with self._lock:
            return [self._task_snapshot(t) for t in self._running_tasks.values()]

    def get_finished(self) -> list[Task]:
        """Lista skończonych tasków (COMPLETED, FAILED, CANCELLED)."""
        with self._lock:
            return [self._task_snapshot(t) for t in reversed(self._finished_tasks)]

    def cancel_task(self, task_id: str) -> bool:
        """
        Anuluj task (jeśli queued) lub oznacz jako cancelled (jeśli running).
        Już executing thread nie zostanie zatrzymany.

        Returns:
            True jeśli uda się anulować, False jeśli task nie znaleziony
        """
        with self._lock:
            # Szukaj w queued
            for i, task in enumerate(self._queued_tasks):
                if task.id == task_id:
                    self._queued_tasks.remove(task)
                    task.status = TaskStatus.CANCELLED
                    task.finished_at = time.time()
                    task.updated_at = time.time()
                    self._finished_tasks.append(task)
                    logger.info(f"Task cancelled (was queued): {task_id}")
                    return True

            # Szukaj w running
            if task_id in self._running_tasks:
                task = self._running_tasks[task_id]
                # Zaznaacz ale nie zatrzymuj (thread będzie dalej running)
                task.status = TaskStatus.CANCELLED
                task.updated_at = time.time()
                logger.info(f"Task marked as cancelled (still executing): {task_id}")
                return True

        return False

    def _task_snapshot(self, task: Optional[Task]) -> Optional[Task]:
        """Utwórz snapshot taska dla thread safety."""
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
        """Zamknij executor (dla testów/shutdown aplikacji)."""
        self._executor.shutdown(wait=wait, timeout=self.config.executor_timeout)


# ============================================================
# GLOBAL INSTANCE & HELPERS
# ============================================================

_global_task_queue: Optional[TaskQueue] = None
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
    meta: Optional[dict] = None,
) -> tuple[bool, Task]:
    """Submit zadanie do kolejki."""
    return get_task_queue().submit(kind, label, work_fn, meta)


def get_task(task_id: str) -> Optional[Task]:
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
