"""SSH 拉取调度。

按每台集群自己的 ``sample_seconds`` 到点触发，不是所有集群一个节奏。
连续疑似空闲的次数由本模块记账——远端是一次性执行，它不知道自己已经
连续几轮没活动了。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import collector_ssh
from .models import MonitorState
from .registry import ClusterConfig, CollectMode
from .store import Store

logger = logging.getLogger(__name__)

# 同时 ssh 的并发数。1 核机器上开太多只会互相抢 CPU。
MAX_PARALLEL = 4
STREAK_KEY = "pull_streak"


class SshPuller:
    def __init__(
        self,
        *,
        store: Store,
        ingest: Callable[[Any], dict[str, Any]],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.ingest = ingest
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._last_run: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    def due_clusters(self) -> list[ClusterConfig]:
        """挑出到点该采的集群。每台按自己的采集间隔走。"""
        now = self._now()
        due: list[ClusterConfig] = []
        for config in self.store.list_clusters(only_enabled=True):
            if config.mode is not CollectMode.SSH_PULL:
                continue
            last = self._last_run.get(config.cluster)
            if last is None or now - last >= timedelta(seconds=config.sample_seconds):
                due.append(config)
        return due

    def _streak(self, cluster: str) -> int:
        return int(self.store.get_setting(f"{STREAK_KEY}:{cluster}", 0) or 0)

    def _set_streak(self, cluster: str, value: int) -> None:
        self.store.set_setting(f"{STREAK_KEY}:{cluster}", value)

    # ------------------------------------------------------------------
    def collect_one(self, config: ClusterConfig) -> dict[str, Any]:
        """采一台。无论成败都会产出事件——失败时是 UNKNOWN，绝不是空闲。"""
        streak = self._streak(config.cluster)
        # 先按「这一轮也算疑似空闲」估算分钟数；若结果不是疑似空闲会归零
        projected = (streak + 1) * config.sample_seconds // 60

        event, error = collector_ssh.collect(config, suspected_idle_minutes=projected)

        if event.state is MonitorState.SUSPECTED_IDLE:
            self._set_streak(config.cluster, streak + 1)
        else:
            self._set_streak(config.cluster, 0)

        self.store.mark_cluster_seen(config.cluster, error=error)
        with self._lock:
            self._last_run[config.cluster] = self._now()

        result = self.ingest(event)
        return {
            "cluster": config.cluster,
            "state": event.state.value,
            "error": error,
            "suspected_idle_minutes": event.suspected_idle_minutes,
            "ingest": result,
        }

    def run_once(self) -> list[dict[str, Any]]:
        """扫一轮。并发采集，单台失败不影响其它台。"""
        due = self.due_clusters()
        if not due:
            return []
        logger.info("SSH 拉取 %s 台：%s", len(due), "、".join(c.cluster for c in due))

        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(due))) as pool:
            futures = {pool.submit(self.collect_one, config): config for config in due}
            for future, config in futures.items():
                try:
                    results.append(future.result())
                except Exception:
                    # 单台异常不能拖垮整轮；下一轮还会再试
                    logger.exception("集群 %s 拉取异常", config.cluster)
                    self.store.mark_cluster_seen(config.cluster, error="拉取任务异常")
                    results.append({"cluster": config.cluster, "error": "拉取任务异常"})
        return results

    def force(self, cluster: str) -> dict[str, Any]:
        """后台「立即采集」按钮。"""
        config = self.store.get_cluster(cluster)
        if config is None:
            raise ValueError(f"集群 {cluster} 不在纳管范围内")
        if config.mode is not CollectMode.SSH_PULL:
            raise ValueError(f"集群 {cluster} 不是 SSH 拉取模式")
        return self.collect_one(config)
