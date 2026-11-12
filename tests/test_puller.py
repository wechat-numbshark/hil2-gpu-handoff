"""SSH 拉取调度：按各自间隔到点、连击计数、并发隔离。"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from hil2.models import MonitorState
from hil2.puller import SshPuller
from hil2.registry import ClusterConfig, CollectMode


@pytest.fixture
def clock():
    class Clock:
        def __init__(self):
            self.now = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)

        def __call__(self):
            return self.now

        def advance(self, **delta):
            self.now += timedelta(**delta)
    return Clock()


@pytest.fixture
def ingested():
    return []


@pytest.fixture
def puller(store, clock, ingested):
    def ingest(event):
        ingested.append(event)
        return {"accepted": True, "state": event.state.value}
    return SshPuller(store=store, ingest=ingest, now=clock)


def add_ssh(store, cluster="52", **kwargs):
    config = ClusterConfig(cluster=cluster, mode=CollectMode.SSH_PULL,
                           ssh_host=f"host-{cluster}", **kwargs)
    return store.save_cluster(config)


def stub_state(monkeypatch, state="busy"):
    import json as _json

    payload = {
        "schema": "hil.monitor.v1", "cluster": "52", "state": state,
        "observed_at": datetime.now(timezone.utc).isoformat(), "reasons": ["stub"],
    }

    def fake_run(argv, **kwargs):
        # 从远端命令里取出真实集群号，多台并发时才不会串
        text = " ".join(argv) if isinstance(argv, list) else str(argv)
        cluster = "52"
        for part in text.split():
            if part.startswith("HIL_MACHINE_NAME="):
                cluster = part.split("=", 1)[1].strip("'")
        body = {**payload, "cluster": cluster}
        return subprocess.CompletedProcess(
            argv, 0, f"EVENT_JSON={_json.dumps(body)}", "")
    monkeypatch.setattr(subprocess, "run", fake_run)


class TestScheduling:
    def test_only_ssh_mode_clusters(self, store, puller):
        add_ssh(store, "52")
        store.save_cluster(ClusterConfig(cluster="58", mode=CollectMode.AGENT_PUSH))
        assert [c.cluster for c in puller.due_clusters()] == ["52"]

    def test_disabled_cluster_skipped(self, store, puller):
        add_ssh(store, "52", enabled=False)
        assert puller.due_clusters() == []

    def test_first_run_is_immediately_due(self, store, puller):
        add_ssh(store, "52")
        assert len(puller.due_clusters()) == 1

    def test_respects_per_cluster_interval(self, store, puller, clock, monkeypatch):
        stub_state(monkeypatch)
        add_ssh(store, "52", sample_seconds=300)
        puller.run_once()
        assert puller.due_clusters() == []          # 刚采过
        clock.advance(seconds=299)
        assert puller.due_clusters() == []
        clock.advance(seconds=2)
        assert len(puller.due_clusters()) == 1      # 到点了

    def test_different_intervals_independent(self, store, puller, clock, monkeypatch):
        stub_state(monkeypatch)
        add_ssh(store, "52", sample_seconds=60)
        add_ssh(store, "58", sample_seconds=600)
        puller.run_once()
        clock.advance(seconds=120)
        assert [c.cluster for c in puller.due_clusters()] == ["52"]


class TestStreak:
    def test_counts_consecutive_suspected(self, store, puller, clock, monkeypatch):
        stub_state(monkeypatch, "suspected_idle")
        add_ssh(store, "52", sample_seconds=300)
        for expected in (5, 10, 15):
            puller.collect_one(store.get_cluster("52"))
            assert store.get_setting("pull_streak:52") * 5 == expected

    def test_injected_into_event(self, store, puller, ingested, monkeypatch):
        stub_state(monkeypatch, "suspected_idle")
        add_ssh(store, "52", sample_seconds=300)
        for _ in range(3):
            puller.collect_one(store.get_cluster("52"))
        # 连续 3 轮 × 5 分钟 = 15 分钟，正好达到人工确认阈值
        assert ingested[-1].suspected_idle_minutes == 15

    def test_resets_on_activity(self, store, puller, monkeypatch):
        stub_state(monkeypatch, "suspected_idle")
        add_ssh(store, "52")
        puller.collect_one(store.get_cluster("52"))
        stub_state(monkeypatch, "busy")
        puller.collect_one(store.get_cluster("52"))
        assert store.get_setting("pull_streak:52") == 0

    def test_failure_resets_streak_not_escalates(self, store, puller, monkeypatch):
        """采集失败是 UNKNOWN，不能被当成「又一轮疑似空闲」。"""
        stub_state(monkeypatch, "suspected_idle")
        add_ssh(store, "52")
        puller.collect_one(store.get_cluster("52"))
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess([], 255, "", "refused"))
        puller.collect_one(store.get_cluster("52"))
        assert store.get_setting("pull_streak:52") == 0


class TestExecution:
    def test_records_last_seen(self, store, puller, monkeypatch):
        stub_state(monkeypatch)
        add_ssh(store, "52")
        puller.collect_one(store.get_cluster("52"))
        loaded = store.get_cluster("52")
        assert loaded.last_seen_at is not None
        assert loaded.last_error == ""

    def test_records_error(self, store, puller, monkeypatch):
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess([], 255, "", "refused"))
        add_ssh(store, "52")
        puller.collect_one(store.get_cluster("52"))
        assert "refused" in store.get_cluster("52").last_error

    def test_error_recovers(self, store, puller, monkeypatch):
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess([], 255, "", "refused"))
        add_ssh(store, "52")
        puller.collect_one(store.get_cluster("52"))
        stub_state(monkeypatch)
        puller.collect_one(store.get_cluster("52"))
        assert store.get_cluster("52").last_error == ""

    def test_event_reaches_ingest(self, store, puller, ingested, monkeypatch):
        stub_state(monkeypatch)
        add_ssh(store, "52")
        puller.run_once()
        assert len(ingested) == 1
        assert ingested[0].source == "ssh_pull"

    def test_failure_still_ingests_unknown(self, store, puller, ingested, monkeypatch):
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess([], 255, "", "refused"))
        add_ssh(store, "52")
        puller.run_once()
        # 采不到也要报上去，让大盘和管理员知道这台出问题了
        assert ingested[0].state is MonitorState.UNKNOWN

    def test_one_bad_cluster_does_not_block_others(self, store, puller, ingested, monkeypatch):
        calls = {"n": 0}

        def flaky(argv, **kwargs):
            calls["n"] += 1
            text = " ".join(argv) if isinstance(argv, list) else str(argv)
            if "host-58" in text:
                raise RuntimeError("模拟这台彻底炸了")
            import json as _json
            return subprocess.CompletedProcess(argv, 0, "EVENT_JSON=" + _json.dumps({
                "schema": "hil.monitor.v1", "cluster": "52", "state": "busy",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }), "")
        monkeypatch.setattr(subprocess, "run", flaky)

        add_ssh(store, "52")
        add_ssh(store, "58")
        results = puller.run_once()
        assert len(results) == 2
        # 52 正常入库，58 记下错误，互不影响
        assert any(r.get("state") == "busy" for r in results)
        assert "refused" in store.get_cluster("58").last_error or store.get_cluster("58").last_error

    def test_empty_when_nothing_due(self, store, puller):
        assert puller.run_once() == []


class TestForce:
    def test_force_collects_now(self, store, puller, monkeypatch):
        stub_state(monkeypatch)
        add_ssh(store, "52", sample_seconds=3600)
        puller.run_once()
        result = puller.force("52")     # 不等间隔，立刻再采一次
        assert result["state"] == "busy"

    def test_force_unknown_cluster(self, store, puller):
        with pytest.raises(ValueError, match="不在纳管"):
            puller.force("99")

    def test_force_rejects_agent_mode(self, store, puller):
        store.save_cluster(ClusterConfig(cluster="52", mode=CollectMode.AGENT_PUSH))
        with pytest.raises(ValueError, match="不是 SSH"):
            puller.force("52")
