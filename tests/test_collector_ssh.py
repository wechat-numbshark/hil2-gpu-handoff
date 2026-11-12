"""SSH 拉取采集：命令构造、失败兜底、与 agent 逻辑一致性。"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

import pytest

from hil2 import collector_ssh
from hil2.collector_ssh import (
    SshCollectError,
    build_remote_command,
    build_ssh_argv,
    check_connection,
    collect,
    parse_event_json,
    unknown_event,
)
from hil2.models import MonitorState
from hil2.registry import ClusterConfig, CollectMode


def ssh_cluster(**kwargs) -> ClusterConfig:
    base = {"cluster": "52", "mode": CollectMode.SSH_PULL, "ssh_host": "hil-52",
            "ssh_user": "ops"}
    base.update(kwargs)
    return ClusterConfig(**base)


class TestSshArgv:
    def test_never_prompts_interactively(self):
        # 卡在密码提示上比直接失败更糟：整轮采集会被挂死
        argv = build_ssh_argv(ssh_cluster())
        assert "BatchMode=yes" in argv
        assert any(a.startswith("ConnectTimeout=") for a in argv)

    def test_user_and_host(self):
        assert build_ssh_argv(ssh_cluster())[-1] == "ops@hil-52"

    def test_host_only_when_no_user(self):
        assert build_ssh_argv(ssh_cluster(ssh_user=""))[-1] == "hil-52"

    def test_custom_port(self):
        argv = build_ssh_argv(ssh_cluster(ssh_port=2222))
        assert "-p" in argv and "2222" in argv

    def test_default_port_omitted(self):
        assert "-p" not in build_ssh_argv(ssh_cluster(ssh_port=22))

    def test_key_forces_identities_only(self):
        # 不加 IdentitiesOnly 时 ssh-agent 里的其它钥匙会抢先，导致认证失败
        argv = build_ssh_argv(ssh_cluster(ssh_key_path="/etc/hil/id"))
        assert "-i" in argv and "IdentitiesOnly=yes" in argv

    def test_bastion_proxy_command_supports_separate_identity(self):
        argv = build_ssh_argv(ssh_cluster(
            ssh_jump_host="bastion.internal", ssh_jump_port=2201,
            ssh_jump_user="gate", ssh_jump_key_path="/etc/hil/jump_id",
            ssh_key_path="/etc/hil/gpu_id",
        ))
        option = next(item for item in argv if item.startswith("ProxyCommand="))
        assert "gate@bastion.internal" in option
        assert "-p 2201" in option
        assert "/etc/hil/jump_id" in option
        assert "/etc/hil/gpu_id" not in option
        assert argv[-1] == "ops@hil-52"

    def test_no_bastion_option_when_not_configured(self):
        assert not any(item.startswith("ProxyCommand=") for item in build_ssh_argv(ssh_cluster()))


class TestRemoteCommand:
    def test_remote_cannot_send_anything(self):
        """远端强制只读只算：没有上报地址，且开着 dry-run。"""
        cmd = build_remote_command(ssh_cluster())
        assert "HIL_DRY_RUN=1" in cmd
        assert "FEISHU_WEBHOOK=''" in cmd
        assert "HIL_COORDINATOR_URL=''" in cmd

    def test_special_chars_are_quoted(self):
        cmd = build_remote_command(ssh_cluster(
            tester_pattern="tester|autotest", sim_pattern="(^|[-_])sim($|[-_])"
        ))
        assert "'tester|autotest'" in cmd
        assert "'(^|[-_])sim($|[-_])'" in cmd

    def test_script_comes_from_stdin(self):
        # 用 `python3 -` 从标准输入读脚本，集群上不落任何文件
        assert build_remote_command(ssh_cluster()).endswith("- --once --print-event")

    def test_sudo_only_when_asked(self):
        assert not build_remote_command(ssh_cluster()).startswith("sudo")
        assert build_remote_command(ssh_cluster(ssh_use_sudo=True)).startswith("sudo -n ")

    def test_custom_python_quoted(self):
        cmd = build_remote_command(ssh_cluster(ssh_python="/opt/py 3.11/bin/python3"))
        assert "'/opt/py 3.11/bin/python3'" in cmd

    def test_thresholds_forwarded(self):
        cmd = build_remote_command(ssh_cluster(idle_minutes=25, data_dir="/data9"))
        assert "HIL_IDLE_MINUTES=25" in cmd
        assert "HIL_DATA_DIR=/data9" in cmd


class TestParsing:
    def test_extracts_event(self):
        payload = {"schema": "hil.monitor.v1", "cluster": "52", "state": "busy"}
        output = f"日志行\nEVENT_JSON={json.dumps(payload)}\n"
        assert parse_event_json(output) == payload

    def test_missing_marker_raises_with_actionable_hint(self):
        with pytest.raises(SshCollectError, match="Python"):
            parse_event_json("bash: python3: command not found")

    def test_broken_json_raises(self):
        with pytest.raises(SshCollectError, match="无法解析"):
            parse_event_json("EVENT_JSON={坏的")

    def test_non_object_raises(self):
        with pytest.raises(SshCollectError, match="不是对象"):
            parse_event_json('EVENT_JSON=["a"]')


class TestFailClosed:
    """所有失败路径都必须产出 UNKNOWN，绝不能是空闲。"""

    def _fail_with(self, monkeypatch, exc_or_result):
        def fake_run(*args, **kwargs):
            if isinstance(exc_or_result, Exception):
                raise exc_or_result
            return exc_or_result
        monkeypatch.setattr(subprocess, "run", fake_run)

    def test_connection_refused(self, monkeypatch):
        self._fail_with(monkeypatch, subprocess.CompletedProcess(
            [], 255, "", "ssh: connect to host hil-52 port 22: Connection refused"))
        event, error = collect(ssh_cluster(), script="x")
        assert event.state is MonitorState.UNKNOWN
        assert "Connection refused" in error

    def test_timeout(self, monkeypatch):
        self._fail_with(monkeypatch, subprocess.TimeoutExpired([], 60))
        event, error = collect(ssh_cluster(), script="x")
        assert event.state is MonitorState.UNKNOWN
        assert "超时" in error

    def test_ssh_missing(self, monkeypatch):
        self._fail_with(monkeypatch, FileNotFoundError())
        event, error = collect(ssh_cluster(), script="x")
        assert event.state is MonitorState.UNKNOWN

    def test_permission_denied(self, monkeypatch):
        self._fail_with(monkeypatch, subprocess.CompletedProcess(
            [], 255, "", "Permission denied (publickey)."))
        event, error = collect(ssh_cluster(), script="x")
        assert event.state is MonitorState.UNKNOWN
        assert "publickey" in error

    def test_garbage_output(self, monkeypatch):
        self._fail_with(monkeypatch, subprocess.CompletedProcess([], 0, "随便什么东西", ""))
        event, error = collect(ssh_cluster(), script="x")
        assert event.state is MonitorState.UNKNOWN

    def test_malformed_event_from_remote(self, monkeypatch):
        self._fail_with(monkeypatch, subprocess.CompletedProcess(
            [], 0, 'EVENT_JSON={"cluster":"52","observed_at":"昨天"}', ""))
        event, error = collect(ssh_cluster(), script="x")
        assert event.state is MonitorState.UNKNOWN

    def test_no_failure_ever_yields_idle(self, monkeypatch):
        """穷举失败形态，确认没有一条路径会说「空闲」。"""
        failures = [
            subprocess.TimeoutExpired([], 60),
            FileNotFoundError(),
            subprocess.CompletedProcess([], 255, "", "Host key verification failed."),
            subprocess.CompletedProcess([], 1, "", "sudo: a password is required"),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        for failure in failures:
            self._fail_with(monkeypatch, failure)
            event, _ = collect(ssh_cluster(), script="x")
            assert event.state is MonitorState.UNKNOWN, f"{failure} 产生了 {event.state}"

    def test_unknown_event_carries_context(self):
        event = unknown_event(ssh_cluster(), "connection refused")
        assert event.cluster == "52"
        assert event.source == "ssh_pull"
        assert event.facts.hostname == "hil-52"
        assert "connection refused" in event.reasons[0]


class TestSuccessPath:
    def _succeed(self, monkeypatch, payload):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
            [], 0, f"EVENT_JSON={json.dumps(payload, ensure_ascii=False)}", ""))

    def _payload(self, state="busy", **extra):
        return {
            "schema": "hil.monitor.v1", "event_id": "52-1-busy", "cluster": "52",
            "state": state, "observed_at": datetime.now(timezone.utc).isoformat(),
            "reasons": ["检测到测试进程"], **extra,
        }

    def test_parses_into_event(self, monkeypatch):
        self._succeed(monkeypatch, self._payload())
        event, error = collect(ssh_cluster(), script="x")
        assert error == ""
        assert event.state is MonitorState.BUSY
        assert event.source == "ssh_pull"

    def test_streak_injected_by_caller(self, monkeypatch):
        # 远端是一次性执行，不知道自己连续几轮没活动了
        self._succeed(monkeypatch, self._payload("suspected_idle"))
        event, _ = collect(ssh_cluster(), suspected_idle_minutes=20, script="x")
        assert event.suspected_idle_minutes == 20

    def test_check_connection_shape(self, monkeypatch):
        self._succeed(monkeypatch, self._payload(facts={
            "hostname": "hil-52",
            "containers": {"sim": [{"name": "s1", "state": "running"}]},
        }))
        result = check_connection(ssh_cluster())
        assert result["ok"] is True
        assert result["state_label"] == "使用中"
        assert result["containers"]["sim"][0]["name"] == "s1"
        assert isinstance(result["elapsed_ms"], int)

    def test_check_connection_reports_failure(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
            [], 255, "", "Connection refused"))
        result = check_connection(ssh_cluster())
        assert result["ok"] is False
        assert "Connection refused" in result["error"]
        assert result["state"] == "unknown"


class TestSharedLogicWithAgent:
    def test_ssh_mode_runs_the_very_same_script(self):
        """两种模式必须跑同一份代码，否则五态判定会慢慢漂移。"""
        assert collector_ssh.AGENT_SCRIPT.name == "hil_agent.py"
        source = collector_ssh.AGENT_SCRIPT.read_text(encoding="utf-8")
        assert "hil.monitor.v1" in source
        assert "def collect()" in source

    def test_real_script_executes_locally_and_emits_event(self, tmp_path):
        """把脚本喂给本机 python，验证 SSH 模式依赖的执行方式确实可行。"""
        import subprocess as sp
        import sys

        result = sp.run(
            [sys.executable, "-", "--once", "--print-event"],
            input=collector_ssh.AGENT_SCRIPT.read_text(encoding="utf-8"),
            capture_output=True, text=True, timeout=90,
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "HIL_MACHINE_NAME": "unit", "HIL_DATA_DIR": str(tmp_path),
                "HIL_DRY_RUN": "1", "HIL_STATE_FILE": str(tmp_path / "s.json"),
                "HIL_LOG_FILE": str(tmp_path / "e.jsonl"), "LOG_LEVEL": "ERROR",
            },
        )
        payload = parse_event_json(result.stdout)
        assert payload["cluster"] == "unit"
        assert payload["schema"] == "hil.monitor.v1"
