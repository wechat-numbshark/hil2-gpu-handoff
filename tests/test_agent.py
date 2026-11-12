"""集群侧采集脚本：五态判定与只读边界。

采集脚本零依赖、单文件，测试通过替换其 ``run()`` 来模拟现场命令输出。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# 会改变机器状态的命令，采集脚本任何路径都不得出现。
MUTATING = {
    "stop", "kill", "rm", "rmi", "restart", "down", "prune", "exec-rm",
    "shutdown", "reboot", "pkill", "killall", "truncate", "mv", "chmod", "chown",
}


class Recorder:
    """替换 agent.run，按命令前缀返回预设结果并记录全部调用。"""

    def __init__(self, module, responses):
        self.module = module
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, timeout=None):
        argv = tuple(argv)
        self.calls.append(argv)
        for prefix, result in self.responses.items():
            if argv[: len(prefix)] == prefix:
                return result(self.module) if callable(result) else result
        return self.module.CommandResult(argv, 1, "", "")

    @property
    def flat(self) -> list[str]:
        return [token for call in self.calls for token in call]


def ok(module, stdout=""):
    return module.CommandResult(("x",), 0, stdout, "")


def fail(module, error="命令不存在：docker"):
    return module.CommandResult(("x",), None, "", "", error)


def exit_code(module, code):
    return module.CommandResult(("x",), code, "", "")


DOCKER = ("docker", "ps")
PGREP = ("docker", "exec")
FIND = ("find",)
NVIDIA = ("nvidia-smi",)
WHO = ("who",)


@pytest.fixture
def agent(agent_module, tmp_path, monkeypatch):
    """把脚本指向临时目录，避免碰到真实 /data1、/run、/var。"""
    monkeypatch.setattr(agent_module, "DATA_DIR", tmp_path / "data1")
    (tmp_path / "data1").mkdir()
    monkeypatch.setattr(agent_module, "CLAIM_FILE", tmp_path / "claim.json")
    monkeypatch.setattr(agent_module, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(agent_module, "LOG_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(agent_module, "MACHINE_NAME", "52")
    monkeypatch.setattr(agent_module, "WEBHOOK_URL", "")
    monkeypatch.setattr(agent_module, "COORDINATOR_URL", "")
    monkeypatch.setattr(
        agent_module,
        "read_network_identity",
        lambda: ("10.20.30.52", ["10.20.30.52", "172.18.0.1"], ""),
    )
    return agent_module


def install(agent, monkeypatch, responses):
    recorder = Recorder(agent, responses)
    monkeypatch.setattr(agent, "run", recorder)
    return recorder


def containers(*rows: str) -> str:
    return "\n".join(rows)


RUNNING_ALL = containers(
    "hil-sim-1\trunning", "hil-proxy-1\trunning", "hil-tester-1\trunning"
)
STOPPED_ALL = containers(
    "hil-sim-1\texited", "hil-proxy-1\texited", "hil-tester-1\texited"
)


class TestCollectionFailures:
    def test_docker_unavailable_is_unknown(self, agent, monkeypatch):
        install(agent, monkeypatch, {DOCKER: lambda m: fail(m)})
        snapshot = agent.collect()
        assert snapshot.state is agent.State.UNKNOWN
        assert "Docker" in snapshot.reasons[0]

    def test_no_container_matched_is_unknown_not_idle(self, agent, monkeypatch):
        # 正则和现场对不上时，绝不能因为「没看到容器」就宣告空闲。
        install(agent, monkeypatch, {DOCKER: lambda m: ok(m, "unrelated-app\trunning")})
        assert agent.collect().state is agent.State.UNKNOWN

    def test_pytest_check_failure_is_unknown_not_idle(self, agent, monkeypatch):
        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: fail(m, "命令超时：docker"),
            FIND: lambda m: ok(m, ""),
        })
        snapshot = agent.collect()
        assert snapshot.state is agent.State.UNKNOWN
        assert any("检查失败" in reason for reason in snapshot.reasons)

    def test_data_dir_unreadable_is_unknown(self, agent, monkeypatch):
        monkeypatch.setattr(agent, "DATA_DIR", Path("/definitely/not/here"))
        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: exit_code(m, 1),
        })
        snapshot = agent.collect()
        assert snapshot.state is agent.State.UNKNOWN
        assert any("数据目录" in reason for reason in snapshot.reasons)

    def test_find_failure_is_unknown(self, agent, monkeypatch):
        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: exit_code(m, 1),
            FIND: lambda m: fail(m, "命令超时：find"),
        })
        assert agent.collect().state is agent.State.UNKNOWN


class TestIdleAndReserved:
    def test_all_core_stopped_is_idle(self, agent, monkeypatch):
        install(agent, monkeypatch, {DOCKER: lambda m: ok(m, STOPPED_ALL)})
        snapshot = agent.collect()
        assert snapshot.state is agent.State.IDLE
        assert "核心容器均已停止" in snapshot.reasons[0]

    def test_stopped_with_active_claim_is_reserved(self, agent, monkeypatch):
        agent.CLAIM_FILE.write_text(json.dumps({
            "owner": "张工",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        }), encoding="utf-8")
        install(agent, monkeypatch, {DOCKER: lambda m: ok(m, STOPPED_ALL)})
        assert agent.collect().state is agent.State.RESERVED

    def test_expired_claim_ignored(self, agent, monkeypatch):
        agent.CLAIM_FILE.write_text(json.dumps({
            "owner": "张工",
            "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        }), encoding="utf-8")
        install(agent, monkeypatch, {DOCKER: lambda m: ok(m, STOPPED_ALL)})
        assert agent.collect().state is agent.State.IDLE

    def test_naive_claim_time_rejected_with_warning(self, agent, monkeypatch):
        agent.CLAIM_FILE.write_text(
            json.dumps({"owner": "张工", "expires_at": "2099-01-01T00:00:00"}), encoding="utf-8"
        )
        install(agent, monkeypatch, {DOCKER: lambda m: ok(m, STOPPED_ALL)})
        snapshot = agent.collect()
        assert snapshot.state is agent.State.IDLE
        assert any("时区" in warning for warning in snapshot.warnings)

    def test_corrupt_claim_file_does_not_crash(self, agent, monkeypatch):
        agent.CLAIM_FILE.write_text("{ 不是 JSON", encoding="utf-8")
        install(agent, monkeypatch, {DOCKER: lambda m: ok(m, STOPPED_ALL)})
        snapshot = agent.collect()
        assert snapshot.state is agent.State.IDLE
        assert any("格式错误" in warning for warning in snapshot.warnings)


class TestBusy:
    def test_pytest_running_is_busy(self, agent, monkeypatch):
        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: ok(m, "123 pytest tests/"),
            FIND: lambda m: ok(m, ""),
        })
        snapshot = agent.collect()
        assert snapshot.state is agent.State.BUSY
        assert snapshot.pytest_active is True

    def test_recent_data_write_is_busy_without_pytest(self, agent, monkeypatch):
        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: exit_code(m, 1),
            FIND: lambda m: ok(m, "/data1/run/latest.log"),
        })
        snapshot = agent.collect()
        assert snapshot.state is agent.State.BUSY
        assert snapshot.recent_data is True

    def test_extra_test_process_recognised(self, agent, monkeypatch):
        monkeypatch.setattr(agent, "EXTRA_TEST_PROCESSES", ["robot"])
        calls = {"n": 0}

        def pgrep(module):
            calls["n"] += 1
            # 第一次问 pytest 没有，第二次问 robot 命中。
            return exit_code(module, 1) if calls["n"] == 1 else ok(module, "9 robot suite")

        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: pgrep,
            FIND: lambda m: ok(m, ""),
        })
        assert agent.collect().state is agent.State.BUSY


class TestLoginSessionsAreAuxiliary:
    def test_collects_session_count_and_users(self, agent, monkeypatch):
        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: ok(m, "123 pytest tests/"),
            FIND: lambda m: ok(m, ""),
            WHO: lambda m: ok(m, "jwj pts/0 2026-09-04 09:00\nzcx pts/1 2026-09-04 09:05\njwj pts/2 2026-09-04 09:10"),
        })
        snapshot = agent.collect()
        assert snapshot.login_session_count == 3
        assert snapshot.login_users == ["jwj", "zcx"]

    def test_session_alone_never_makes_machine_busy(self, agent, monkeypatch):
        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: exit_code(m, 1),
            FIND: lambda m: ok(m, ""),
            WHO: lambda m: ok(m, "jwj pts/0 2026-09-04 09:00"),
        })
        assert agent.collect().state is agent.State.SUSPECTED_IDLE


class TestSuspectedIdle:
    def test_containers_up_but_no_activity(self, agent, monkeypatch):
        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: exit_code(m, 1),
            FIND: lambda m: ok(m, ""),
        })
        snapshot = agent.collect()
        assert snapshot.state is agent.State.SUSPECTED_IDLE
        assert snapshot.pytest_active is False and snapshot.recent_data is False

    def test_claim_overrides_suspected_idle(self, agent, monkeypatch):
        agent.CLAIM_FILE.write_text(json.dumps({
            "owner": "王工",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }), encoding="utf-8")
        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: exit_code(m, 1),
            FIND: lambda m: ok(m, ""),
        })
        assert agent.collect().state is agent.State.RESERVED


class TestGpuNeverDecides:
    def _no_activity(self, agent, monkeypatch, gpu_stdout):
        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: exit_code(m, 1),
            FIND: lambda m: ok(m, ""),
            NVIDIA: lambda m: ok(m, gpu_stdout),
        })
        return agent.collect()

    def test_gpu_at_100_percent_still_suspected_idle(self, agent, monkeypatch):
        # 交接文件第 2 节：GPU 高不能证明还在用。
        rows = "\n".join(f"{i}, 99, 24000" for i in range(8))
        snapshot = self._no_activity(agent, monkeypatch, rows)
        assert snapshot.state is agent.State.SUSPECTED_IDLE
        assert len(snapshot.gpus) == 8

    def test_gpu_at_zero_does_not_force_idle(self, agent, monkeypatch):
        # 反向也一样：GPU 低不能证明空闲。
        rows = "\n".join(f"{i}, 0, 0" for i in range(8))
        assert self._no_activity(agent, monkeypatch, rows).state is agent.State.SUSPECTED_IDLE

    def test_gpu_failure_does_not_change_verdict(self, agent, monkeypatch):
        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: ok(m, "1 pytest"),
            FIND: lambda m: ok(m, ""),
            NVIDIA: lambda m: fail(m, "命令不存在：nvidia-smi"),
        })
        snapshot = agent.collect()
        assert snapshot.state is agent.State.BUSY
        assert "不影响闲忙结论" in snapshot.gpu_note


class TestReadOnlyBoundary:
    @pytest.mark.parametrize("scenario", [
        {DOCKER: lambda m: ok(m, RUNNING_ALL), PGREP: lambda m: ok(m, "1 pytest"),
         FIND: lambda m: ok(m, "")},
        {DOCKER: lambda m: ok(m, STOPPED_ALL)},
        {DOCKER: lambda m: ok(m, RUNNING_ALL), PGREP: lambda m: exit_code(m, 1),
         FIND: lambda m: ok(m, "")},
        {DOCKER: lambda m: fail(m)},
    ])
    def test_no_mutating_command_in_any_path(self, agent, monkeypatch, scenario):
        recorder = install(agent, monkeypatch, scenario)
        agent.collect()
        assert recorder.calls, "至少应该执行过一条采集命令"
        assert not (MUTATING & set(recorder.flat)), f"出现了会改状态的命令：{recorder.calls}"

    def test_docker_exec_is_only_used_for_pgrep(self, agent, monkeypatch):
        recorder = install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: exit_code(m, 1),
            FIND: lambda m: ok(m, ""),
        })
        agent.collect()
        for call in recorder.calls:
            if call[:2] == ("docker", "exec"):
                assert call[3] == "pgrep", f"docker exec 只允许跑 pgrep，实际：{call}"

    def test_agent_never_writes_claim_file(self, agent, monkeypatch):
        install(agent, monkeypatch, {DOCKER: lambda m: ok(m, STOPPED_ALL)})
        agent.collect()
        # 保留声明由用户维护，采集脚本只读。
        assert not agent.CLAIM_FILE.exists()


class TestEventShape:
    def _snapshot(self, agent, monkeypatch, state=None):
        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: ok(m, "1 pytest"),
            FIND: lambda m: ok(m, ""),
            NVIDIA: lambda m: ok(m, "0, 90, 2048"),
        })
        return agent.collect()

    def test_event_matches_schema(self, agent, monkeypatch):
        snapshot = self._snapshot(agent, monkeypatch)
        observed = datetime.now().astimezone()
        event = agent.build_event(snapshot, 0, observed)
        assert event["schema"] == "hil.monitor.v1"
        assert event["cluster"] == "52"
        assert event["state"] == "busy"
        assert event["facts"]["primary_ip"] == "10.20.30.52"
        assert event["facts"]["ipv4_addresses"] == ["10.20.30.52", "172.18.0.1"]
        assert event["facts"]["gpus"][0]["utilization"] == 90

    def test_event_parses_on_the_server_side(self, agent, monkeypatch):
        from hil2.models import MonitorEvent, MonitorState

        snapshot = self._snapshot(agent, monkeypatch)
        event = agent.build_event(snapshot, 3, datetime.now().astimezone())
        parsed = MonitorEvent.from_dict(event)
        # 采集端和服务端的契约必须真的对得上。
        assert parsed.state is MonitorState.BUSY
        assert parsed.cluster == "52"
        assert parsed.facts.gpus[0].utilization == 90

    def test_message_carries_event_json(self, agent, monkeypatch):
        from hil2.feishu.callback import extract_monitor_payload

        snapshot = self._snapshot(agent, monkeypatch)
        event = agent.build_event(snapshot, 0, datetime.now().astimezone())
        text = agent.format_message(event, snapshot, 0)
        assert extract_monitor_payload(text) == event

    def test_suspected_minutes_derived_from_streak(self, agent, monkeypatch):
        monkeypatch.setattr(agent, "SAMPLE_SECONDS", 300)
        snapshot = self._snapshot(agent, monkeypatch)
        event = agent.build_event(snapshot, 3, datetime.now().astimezone())
        assert event["suspected_idle_minutes"] == 15

    def test_observed_at_has_timezone(self, agent, monkeypatch):
        snapshot = self._snapshot(agent, monkeypatch)
        event = agent.build_event(snapshot, 0, datetime.now().astimezone())
        assert datetime.fromisoformat(event["observed_at"]).tzinfo is not None


class TestRunOnceAndState:
    def _install_idle(self, agent, monkeypatch):
        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: exit_code(m, 1),
            FIND: lambda m: ok(m, ""),
        })

    def test_streak_increments_and_persists(self, agent, monkeypatch):
        self._install_idle(agent, monkeypatch)
        state = agent.run_once({})
        assert state["suspect_streak"] == 1
        state = agent.run_once(state)
        assert state["suspect_streak"] == 2
        # 重启后从磁盘恢复，15 分钟计时不会被清零。
        assert agent.load_state()["suspect_streak"] == 2

    def test_streak_resets_on_activity(self, agent, monkeypatch):
        self._install_idle(agent, monkeypatch)
        state = agent.run_once({})
        install(agent, monkeypatch, {
            DOCKER: lambda m: ok(m, RUNNING_ALL),
            PGREP: lambda m: ok(m, "1 pytest"),
            FIND: lambda m: ok(m, ""),
        })
        assert agent.run_once(state)["suspect_streak"] == 0

    def test_local_jsonl_always_written(self, agent, monkeypatch):
        self._install_idle(agent, monkeypatch)
        agent.run_once({})
        lines = agent.LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
        assert json.loads(lines[0])["cluster"] == "52"

    def test_dry_run_sends_nothing(self, agent, monkeypatch):
        monkeypatch.setattr(agent, "DRY_RUN", True)
        monkeypatch.setattr(agent, "COORDINATOR_URL", "http://should-not-be-called")
        sent = []
        monkeypatch.setattr(agent, "_post_json", lambda *a, **k: sent.append(a) or (True, ""))
        self._install_idle(agent, monkeypatch)
        agent.run_once({})
        # 灰度阶段只写本地日志，绝不外发。
        assert sent == []
        assert agent.LOG_FILE.exists()
