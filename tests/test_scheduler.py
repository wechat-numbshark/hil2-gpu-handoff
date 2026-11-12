"""后台定时任务：能启动、能停下、异常不致命。"""

from __future__ import annotations

import threading
import time

from hil2.scheduler import PeriodicWorker


class TestPeriodicWorker:
    def test_run_once_counts_success(self):
        calls = []
        worker = PeriodicWorker(name="t", interval_seconds=60, job=lambda: calls.append(1))
        worker.run_once()
        assert calls == [1] and worker.runs == 1 and worker.failures == 0

    def test_exception_counted_not_raised(self):
        def boom():
            raise RuntimeError("坏了")

        worker = PeriodicWorker(name="t", interval_seconds=60, job=boom)
        worker.run_once()  # 不应抛出：单轮失败不能让整个监控停摆
        assert worker.failures == 1 and worker.runs == 0

    def test_start_runs_then_stop_halts(self):
        started = threading.Event()
        calls = []

        def job():
            calls.append(1)
            started.set()

        worker = PeriodicWorker(name="t", interval_seconds=0.05, job=job)
        worker.start()
        assert started.wait(2), "后台任务没有在 2 秒内跑起来"
        worker.stop()
        seen = len(calls)
        time.sleep(0.2)
        assert len(calls) == seen, "stop() 之后不应再执行"

    def test_double_start_is_idempotent(self):
        worker = PeriodicWorker(name="t", interval_seconds=60, job=lambda: None)
        worker.start()
        worker.start()
        worker.stop()

    def test_stop_before_start_is_safe(self):
        PeriodicWorker(name="t", interval_seconds=60, job=lambda: None).stop()
