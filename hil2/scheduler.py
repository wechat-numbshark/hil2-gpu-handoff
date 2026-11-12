"""后台定时任务：卡片超时扫描与预约同步。

用普通线程 + Event.wait 实现，不引入 APScheduler：1 核 1G 服务器上够用，
且进程退出时能立刻停下来。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class PeriodicWorker:
    def __init__(
        self,
        *,
        name: str,
        interval_seconds: float,
        job: Callable[[], object],
    ) -> None:
        self.name = name
        self.interval_seconds = interval_seconds
        self.job = job
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.runs = 0
        self.failures = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()
        logger.info("后台任务已启动：%s（每 %ss）", self.name, self.interval_seconds)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("后台任务已停止：%s", self.name)

    def run_once(self) -> None:
        """同步执行一次，供测试和手动触发使用。"""
        try:
            self.job()
            self.runs += 1
        except Exception:
            self.failures += 1
            logger.exception("后台任务执行失败：%s", self.name)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.interval_seconds)
