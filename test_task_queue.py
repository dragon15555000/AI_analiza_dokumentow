"""
Unit testy dla task_queue.py
"""

import threading
import time
import unittest

from task_queue import (
    TaskQueue,
    TaskQueueConfig,
    TaskStatus,
    get_task,
    get_task_status,
    submit_task,
    update_task,
)


class TestTaskQueue(unittest.TestCase):
    """Testy TaskQueue"""

    def setUp(self):
        """Utwórz fresh queue na każdy test."""
        import tempfile
        import os
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.queue = TaskQueue(TaskQueueConfig(max_concurrent=2, history_limit=10), db_path=self.db_path)

    def tearDown(self):
        import os
        self.queue.shutdown(wait=True)
        try:
            os.close(self.db_fd)
            os.unlink(self.db_path)
        except Exception:
            pass

    def test_submit_task_immediate_execution(self):
        """Test: Dodaj task, on się od razu wykonuje."""
        executed = []

        def work(task_id):
            time.sleep(0.05)
            executed.append(task_id)

        queued, task = self.queue.submit("test", "Test task", work)
        task_id = task.id
        # snapshot zwrócony może być QUEUED lub RUNNING (race condition OK)
        self.assertIn(task.status, [TaskStatus.QUEUED, TaskStatus.RUNNING])

        # Czekaj aż się skończy
        time.sleep(0.3)
        finished = self.queue.get_finished()
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].status, TaskStatus.COMPLETED)
        self.assertEqual(len(executed), 1)

    def test_submit_task_queued(self):
        """Test: Gdy max_concurrent tasków już running, nowe czekają."""
        started = []
        barrier = [0]  # Mutex-like counter
        import threading

        barrier_lock = threading.Lock()

        def slow_work(task_id):
            with barrier_lock:
                barrier[0] += 1
                if barrier[0] == 1:  # Pierwszy task zablokuje
                    time.sleep(0.5)
                elif barrier[0] == 2:  # Drugi task też zablokuje
                    time.sleep(0.5)
            started.append(task_id)

        queued1, task1 = self.queue.submit("test1", "Task 1", slow_work)
        queued2, task2 = self.queue.submit("test2", "Task 2", slow_work)
        queued3, task3 = self.queue.submit("test3", "Task 3", slow_work)

        # max_concurrent=2, więc:
        # task1: running, task2: running, task3: queued
        self.assertFalse(queued1)
        self.assertFalse(queued2)
        self.assertTrue(queued3)

        # Czekaj aż wszystkie się skończą
        time.sleep(1.5)
        finished = self.queue.get_finished()
        self.assertEqual(len(finished), 3)
        for task in finished:
            self.assertEqual(task.status, TaskStatus.COMPLETED)

    def test_task_status_transitions(self):
        """Test: Task przechodzi queued → running → completed."""

        def work(task_id):
            time.sleep(0.1)

        queued, task = self.queue.submit("test", "Test", work)
        task_id = task.id

        # Po bardzo krótkim czekaniu: może być QUEUED lub RUNNING
        time.sleep(0.01)
        snapshot1 = self.queue.get_task(task_id)
        self.assertIn(snapshot1.status, [TaskStatus.QUEUED, TaskStatus.RUNNING])

        # Po czekaniu na wykonanie: COMPLETED
        time.sleep(0.2)
        completed = self.queue.get_task(task_id)
        self.assertEqual(completed.status, TaskStatus.COMPLETED)

    def test_cancel_queued_task(self):
        """Test: Anulowanie taska w kolejce (zanim się zacznie)."""
        executed = []
        barrier = threading.Lock()

        def slow_work(task_id):
            time.sleep(0.3)
            executed.append(task_id)

        queued1, task1 = self.queue.submit("slow1", "Slow 1", slow_work)
        queued2, task2 = self.queue.submit("slow2", "Slow 2", slow_work)
        queued3, task3 = self.queue.submit("slow3", "Slow 3", slow_work)

        # Anuluj task3 kiedy jeszcze czeka w queue
        time.sleep(0.05)
        cancelled = self.queue.cancel_task(task3.id)
        self.assertTrue(cancelled)

        # Czekaj aż task1 i task2 się skończą
        time.sleep(1.0)

        # Powinno być tylko 2 wykonane (task1, task2), task3 nie
        self.assertEqual(len(executed), 2)
        self.assertNotIn(task3.id, executed)

        # task3 powinno być CANCELLED
        cancelled_task = self.queue.get_task(task3.id)
        self.assertEqual(cancelled_task.status, TaskStatus.CANCELLED)

    def test_task_error_handling(self):
        """Test: Task kiedy work_fn rzuca exception."""

        def failing_work(task_id):
            raise ValueError("Deliberate error")

        queued, task = self.queue.submit("fail", "Failing task", failing_work)

        # Czekaj aż się skończy
        time.sleep(0.2)

        failed = self.queue.get_task(task.id)
        self.assertEqual(failed.status, TaskStatus.FAILED)
        self.assertIn("Deliberate error", failed.error)

    def test_update_task_progress(self):
        """Test: Update progress podczas wykonywania."""

        def work_with_updates(task_id):
            for i in range(3):
                time.sleep(0.05)
                update_task(task_id, progress_pct=(i + 1) * 33, done=i + 1, total=3)

        # Użyj globalne submit_task zamiast self.queue
        queued, task = submit_task("progress", "Progress task", work_with_updates)

        # Czekaj na wykonanie
        time.sleep(0.5)
        completed = get_task(task.id)
        self.assertEqual(completed.status, TaskStatus.COMPLETED)
        # progress_pct powinien być zmieniony co najmniej raz
        self.assertGreaterEqual(completed.done, 1)  # co najmniej jeden update

    def test_task_history_limit(self):
        """Test: Historia tasków nie przekracza limitu."""

        def quick_work(task_id):
            pass

        # Utwórz 15 tasków w kolejce z history_limit=10
        for i in range(15):
            self.queue.submit(f"t{i}", f"Task {i}", quick_work)

        time.sleep(0.5)  # Czekaj aż się skończą

        finished = self.queue.get_finished()
        self.assertLessEqual(len(finished), 10)  # Historia limitowana do 10

    def test_concurrent_execution(self):
        """Test: max_concurrent=2 pracuje prawidłowo z konkurencją."""
        import threading

        running_count = [0]
        max_concurrent_seen = [0]
        running_lock = threading.Lock()

        def work(task_id):
            with running_lock:
                running_count[0] += 1
                if running_count[0] > max_concurrent_seen[0]:
                    max_concurrent_seen[0] = running_count[0]
            time.sleep(0.1)
            with running_lock:
                running_count[0] -= 1

        for i in range(5):
            self.queue.submit(f"t{i}", f"Task {i}", work)

        time.sleep(1.0)  # Czekaj na wszystkie

        # max_concurrent widział co najwyżej 2 jednocześnie
        self.assertLessEqual(max_concurrent_seen[0], 2)

    def test_task_snapshot_isolation(self):
        """Test: Task snapshot nie jest zmodyfikowany przez updates."""

        def work(task_id):
            for i in range(3):
                time.sleep(0.05)
                update_task(task_id, done=i + 1, progress_pct=(i + 1) * 33)

        # Użyj globalne submit_task zamiast self.queue
        queued, task_snapshot = submit_task("test", "Snapshot test", work)
        original_done = task_snapshot.done
        original_pct = task_snapshot.progress_pct

        time.sleep(0.5)

        # Pobranie snapshot drugiej raz - powinno być inne
        new_snapshot = get_task(task_snapshot.id)
        self.assertGreaterEqual(new_snapshot.done, 1)  # co najmniej jeden update
        self.assertGreater(new_snapshot.progress_pct, original_pct)

        # Ale oryginalny snapshot nie zmienił się
        self.assertEqual(task_snapshot.done, original_done)
        self.assertEqual(task_snapshot.progress_pct, original_pct)

    def test_get_status_returns_all_tasks(self):
        """Test: get_status() zwraca podzielone na queued/running/finished."""

        def work(task_id):
            time.sleep(0.1)

        t1_q, t1 = self.queue.submit("t1", "Task 1", work)
        t2_q, t2 = self.queue.submit("t2", "Task 2", work)
        t3_q, t3 = self.queue.submit("t3", "Task 3", work)

        # Natychmiast: t1, t2 running, t3 queued (lub wszystkie już się zaczęły)
        status = self.queue.get_status()
        queued_count = len(status["queued"])
        running_count = len(status["running"])
        self.assertEqual(queued_count + running_count, 3)  # w sumie 3

        # Po czekaniu: wszystkie finished
        time.sleep(0.35)
        status = self.queue.get_status()
        self.assertEqual(len(status["queued"]), 0)
        self.assertEqual(len(status["running"]), 0)
        self.assertEqual(len(status["finished"]), 3)

    def test_persistence(self):
        """Test trwałości: nowa instancja kolejki widzi zadanie zapisane przez poprzednią."""
        import tempfile
        import os

        fd, db_path = tempfile.mkstemp()
        try:
            # Pierwsza instancja
            queue1 = TaskQueue(db_path=db_path)

            # Dodaj zadanie
            def dummy_work(task_id):
                pass
            queued, task1 = queue1.submit("test_persist", "Persist Task", dummy_work)
            task_id = task1.id

            # Poczekaj chwilę, upewnij się, że jest w bazie
            time.sleep(0.1)

            # Druga instancja na tej samej bazie
            queue2 = TaskQueue(db_path=db_path)
            task2 = queue2.get_task(task_id)

            self.assertIsNotNone(task2)
            self.assertEqual(task2.id, task_id)
            self.assertEqual(task2.label, "Persist Task")

            # Zamknij executory
            queue1.shutdown(wait=True)
            queue2.shutdown(wait=True)
        finally:
            os.close(fd)
            try:
                os.unlink(db_path)
            except Exception:
                pass


class TestGlobalHelpers(unittest.TestCase):
    """Testy globalnych helper functions"""

    def setUp(self):
        import tempfile
        import os
        import task_queue
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.original_queue = task_queue._global_task_queue
        task_queue._global_task_queue = TaskQueue(db_path=self.db_path)

    def tearDown(self):
        import os
        import task_queue
        task_queue._global_task_queue.shutdown(wait=True)
        task_queue._global_task_queue = self.original_queue
        try:
            os.close(self.db_fd)
            os.unlink(self.db_path)
        except Exception:
            pass

    def test_global_submit_task(self):
        """Test: Globalna funkcja submit_task."""

        def work(task_id):
            pass

        queued, task = submit_task("global", "Global test", work)
        self.assertIsNotNone(task.id)
        self.assertEqual(task.kind, "global")

        time.sleep(0.1)
        status = get_task_status()
        self.assertGreaterEqual(len(status["finished"]), 1)

    def test_global_get_task(self):
        """Test: Pobieranie taska po ID."""

        def work(task_id):
            time.sleep(0.1)

        _, task = submit_task("test", "Test", work)
        retrieved = get_task(task.id)
        self.assertEqual(retrieved.id, task.id)


if __name__ == "__main__":
    unittest.main()
