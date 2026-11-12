"""SSH 定时拉取采集。

与 agent 推送模式的区别只有「谁发起」：

* agent 推送：集群上常驻进程，到点自己采集并上报。
* SSH 拉取：协调服务到点 ssh 过去，**把同一份采集脚本喂给远端 python 执行**，
  读回它打印的 ``EVENT_JSON``。集群上什么都不用装，也不留下任何文件。

「同一份脚本」很重要：两种模式的五态判定逻辑因此不可能走偏，
不存在两套实现慢慢漂移的问题。

安全边界与 agent 完全一致：
- 远端只执行 docker ps / pgrep / find / nvidia-smi 四类读命令。
- 远端强制 ``HIL_DRY_RUN=1`` 且不下发任何 webhook 地址，脚本无从对外发消息。
- ssh 连不上、超时、脚本报错，一律产出 UNKNOWN 事件，**绝不当成空闲**。
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import EventParseError, MonitorEvent
from .registry import ClusterConfig

logger = logging.getLogger(__name__)

AGENT_SCRIPT = Path(__file__).resolve().parent.parent / "agent" / "hil_agent.py"

# ssh 自身的连接开销之外还要留出脚本执行时间
SSH_CONNECT_TIMEOUT = 10
EXTRA_TIMEOUT_SECONDS = 30

# 远端只写这两个临时文件；写不进去脚本也能跑，不影响判定。
REMOTE_STATE_FILE = "/tmp/.hil-pull-state.json"
REMOTE_LOG_FILE = "/dev/null"


class SshCollectError(RuntimeError):
    """SSH 采集失败。调用方必须转成 UNKNOWN 事件，不得忽略。"""


def build_ssh_argv(config: ClusterConfig) -> list[str]:
    argv = [
        "ssh",
        "-o", "BatchMode=yes",                    # 绝不交互式要密码，卡住比失败更糟
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "-o", "StrictHostKeyChecking=accept-new",  # 首次连接自动记录，之后变更会拒绝
        "-o", "LogLevel=ERROR",
    ]
    if config.ssh_port and config.ssh_port != 22:
        argv += ["-p", str(config.ssh_port)]
    if config.ssh_key_path:
        argv += ["-i", config.ssh_key_path, "-o", "IdentitiesOnly=yes"]
    if config.ssh_jump_host:
        # 显式 ProxyCommand 支持堡垒机与目标机使用不同私钥；所有动态值都
        # 作为独立参数生成并经 shlex.quote，避免落入 shell 注入路径。
        proxy = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "LogLevel=ERROR",
        ]
        if config.ssh_jump_port and config.ssh_jump_port != 22:
            proxy += ["-p", str(config.ssh_jump_port)]
        if config.ssh_jump_key_path:
            proxy += ["-i", config.ssh_jump_key_path, "-o", "IdentitiesOnly=yes"]
        proxy += ["-W", "%h:%p"]
        jump_target = (
            f"{config.ssh_jump_user}@{config.ssh_jump_host}"
            if config.ssh_jump_user else config.ssh_jump_host
        )
        proxy.append(jump_target)
        argv += ["-o", "ProxyCommand=" + " ".join(shlex.quote(item) for item in proxy)]
    target = f"{config.ssh_user}@{config.ssh_host}" if config.ssh_user else config.ssh_host
    argv.append(target)
    return argv


def build_remote_command(config: ClusterConfig) -> str:
    """远端要执行的命令。所有取值都经过 shlex 引用，正则里的 | ( ) 不会被 shell 吃掉。"""
    env = {
        "HIL_MACHINE_NAME": config.cluster,
        "HIL_DATA_DIR": config.data_dir,
        "HIL_IDLE_MINUTES": str(config.idle_minutes),
        "HIL_SAMPLE_SECONDS": str(config.sample_seconds),
        "HIL_CONFIRM_SAMPLES": str(config.confirm_samples),
        "HIL_COMMAND_TIMEOUT": str(config.command_timeout),
        "HIL_SIM_PATTERN": config.sim_pattern,
        "HIL_PROXY_PATTERN": config.proxy_pattern,
        "HIL_TESTER_PATTERN": config.tester_pattern,
        "HIL_EXTRA_TEST_PROCESSES": config.extra_test_processes,
        # 远端一律只读只算，不许对外发任何消息
        "HIL_DRY_RUN": "1",
        "HIL_COORDINATOR_URL": "",
        "FEISHU_WEBHOOK": "",
        "HIL_STATE_FILE": REMOTE_STATE_FILE,
        "HIL_LOG_FILE": REMOTE_LOG_FILE,
        "LOG_LEVEL": "ERROR",
    }
    assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    python = shlex.quote(config.ssh_python or "python3")
    # 脚本从 stdin 喂进去，远端不落任何文件
    inner = f"env {assignments} {python} - --once --print-event"
    if config.ssh_use_sudo:
        # 只有读 docker 需要提权时才用；配合最小 sudo 规则
        inner = f"sudo -n {inner}"
    return inner


def run_remote(config: ClusterConfig, *, script: str | None = None) -> str:
    """执行远端采集，返回标准输出。任何失败都抛 SshCollectError。"""
    if script is None:
        try:
            script = AGENT_SCRIPT.read_text(encoding="utf-8")
        except OSError as exc:
            raise SshCollectError(f"读不到采集脚本 {AGENT_SCRIPT}：{type(exc).__name__}") from exc

    argv = build_ssh_argv(config) + [build_remote_command(config)]
    timeout = SSH_CONNECT_TIMEOUT + config.command_timeout + EXTRA_TIMEOUT_SECONDS

    try:
        completed = subprocess.run(
            argv,
            input=script,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SshCollectError("本机没有 ssh 命令") from exc
    except subprocess.TimeoutExpired as exc:
        raise SshCollectError(f"ssh 执行超时（{timeout}s）") from exc
    except Exception as exc:
        raise SshCollectError(f"ssh 执行异常：{type(exc).__name__}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        message = detail[-1][:200] if detail else f"exit={completed.returncode}"
        raise SshCollectError(f"ssh 返回失败：{message}")
    return completed.stdout


def parse_event_json(output: str) -> dict[str, Any]:
    for line in output.splitlines():
        if line.startswith("EVENT_JSON="):
            try:
                payload = json.loads(line[len("EVENT_JSON="):])
            except json.JSONDecodeError as exc:
                raise SshCollectError("远端返回的 EVENT_JSON 无法解析") from exc
            if not isinstance(payload, dict):
                raise SshCollectError("远端返回的 EVENT_JSON 不是对象")
            return payload
    raise SshCollectError("远端输出里没有 EVENT_JSON，检查目标机 Python 版本是否 3.8+")


def unknown_event(config: ClusterConfig, reason: str) -> MonitorEvent:
    """采集失败时的兜底事件。永远是 UNKNOWN，绝不会是空闲。"""
    observed_at = datetime.now(timezone.utc)
    return MonitorEvent.from_dict(
        {
            "schema": "hil.monitor.v1",
            "event_id": f"{config.cluster}-pull-{int(observed_at.timestamp())}-unknown",
            "cluster": config.cluster,
            "state": "unknown",
            "observed_at": observed_at.isoformat(),
            "reasons": [f"SSH 拉取失败：{reason}"],
            "facts": {
                "hostname": config.ssh_host,
                "data_dir": config.data_dir,
                "gpu_note": "未采集",
            },
        },
        source="ssh_pull",
    )


def collect(
    config: ClusterConfig,
    *,
    suspected_idle_minutes: int | None = None,
    script: str | None = None,
) -> tuple[MonitorEvent, str]:
    """采集一台集群。返回 (事件, 错误信息)，错误为空串表示成功。

    ``suspected_idle_minutes`` 由调用方按连续次数算好传入：
    远端是一次性执行，自己不知道已经连续疑似空闲多久了。
    """
    try:
        output = run_remote(config, script=script)
        payload = parse_event_json(output)
    except SshCollectError as exc:
        logger.warning("集群 %s SSH 采集失败：%s", config.cluster, exc)
        return unknown_event(config, str(exc)), str(exc)

    if suspected_idle_minutes is not None:
        payload["suspected_idle_minutes"] = suspected_idle_minutes

    try:
        return MonitorEvent.from_dict(payload, source="ssh_pull"), ""
    except EventParseError as exc:
        logger.warning("集群 %s 返回的事件无法解析：%s", config.cluster, exc)
        return unknown_event(config, f"事件格式错误：{exc}"), str(exc)


def check_connection(config: ClusterConfig) -> dict[str, Any]:
    """后台「测试连接」按钮。只跑一次采集，把结果原样回给管理员看。"""
    started = datetime.now(timezone.utc)
    event, error = collect(config)
    elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return {
        "ok": not error,
        "error": error,
        "elapsed_ms": elapsed_ms,
        "state": event.state.value,
        "state_label": event.state.label,
        "reasons": list(event.reasons),
        "warnings": list(event.warnings),
        "containers": {
            role: [item.as_dict() for item in items]
            for role, items in event.facts.containers.items()
        },
        "hostname": event.facts.hostname,
        "login_session_count": event.facts.login_session_count,
        "login_users": list(event.facts.login_users),
        "login_note": event.facts.login_note,
    }
