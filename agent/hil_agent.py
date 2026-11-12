#!/usr/bin/env python3
"""HIL2.0 集群侧采集脚本（只读）。

对被监控环境严格只读：只执行 ``docker ps -a``、容器内 ``pgrep -af pytest``、
``find /data1 -mmin``、``nvidia-smi`` 和 ``who`` 五类命令。
绝不停止容器、绝不杀进程、绝不删除文件、绝不释放集群。

零第三方依赖，Python 3.8+ 即可运行，方便直接扔到任意集群上。

双通道投递（交接文件第 7 节关键兼容方案）：
  1. Feishu Webhook  → 独立监控群，折叠审计副本，显式 @ 业务机器人。
  2. HTTP POST       → 协调服务 /api/ingest，主通道。
两条通道发同一份 JSON，靠 event_id 幂等去重；任意一条可用系统就能工作。

灰度用法（交接文件第 10 节 P1）：
    HIL_DRY_RUN=1 python3 hil_agent.py        # 只写本地 JSONL，不发任何消息
    python3 hil_agent.py --once --print-event # 单次采集，打印事件后退出
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

AGENT_VERSION = "1.2.0"
SCHEMA = "hil.monitor.v1"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


MACHINE_NAME = _env("HIL_MACHINE_NAME", "52")
DATA_DIR = Path(_env("HIL_DATA_DIR", "/data1"))
WEBHOOK_URL = _env("FEISHU_WEBHOOK")
CONSUMER_BOT_OPEN_ID = _env("HIL_CONSUMER_BOT_OPEN_ID")
COORDINATOR_URL = _env("HIL_COORDINATOR_URL")       # 例：http://10.0.0.5:8848/api/ingest
COORDINATOR_TOKEN = _env("HIL_INGEST_TOKEN")
CLAIM_FILE = Path(_env("HIL_CLAIM_FILE", "/run/hil-monitor/claim.json"))
STATE_FILE = Path(_env("HIL_STATE_FILE", "/var/lib/hil-monitor/state.json"))
LOG_FILE = Path(_env("HIL_LOG_FILE", "/var/log/hil-monitor/events.jsonl"))

SAMPLE_SECONDS = _env_int("HIL_SAMPLE_SECONDS", 300)
IDLE_MINUTES = _env_int("HIL_IDLE_MINUTES", 15)
CONFIRM_SAMPLES = _env_int("HIL_CONFIRM_SAMPLES", 3)
HEARTBEAT_SAMPLES = _env_int("HIL_HEARTBEAT_SAMPLES", 12)
COMMAND_TIMEOUT = _env_int("HIL_COMMAND_TIMEOUT", 30)
DELIVERY_RETRIES = _env_int("HIL_DELIVERY_RETRIES", 3)
DRY_RUN = _env("HIL_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}

# 现场确认真实容器名之前，先用正则匹配；确认后建议改成精确名字列表。
ROLE_PATTERNS = {
    "sim": re.compile(_env("HIL_SIM_PATTERN", r"(^|[-_])sim(ulator)?($|[-_])"), re.I),
    "proxy": re.compile(_env("HIL_PROXY_PATTERN", r"(^|[-_])proxy($|[-_])"), re.I),
    "tester": re.compile(_env("HIL_TESTER_PATTERN", r"tester|autotest|simlib|logsim"), re.I),
}

# 除 pytest 外可能存在的其他真实测试进程；现场确认后在这里补充。
# 留空表示只认 pytest。多个用逗号分隔，例如 "robot,unittest"。
EXTRA_TEST_PROCESSES = [
    item.strip() for item in _env("HIL_EXTRA_TEST_PROCESSES", "").split(",") if item.strip()
]

logger = logging.getLogger("hil-agent")


class State(str, Enum):
    BUSY = "busy"
    IDLE = "idle"
    RESERVED = "reserved"
    SUSPECTED_IDLE = "suspected_idle"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            State.BUSY: "使用中",
            State.IDLE: "明确空闲",
            State.RESERVED: "保留中",
            State.SUSPECTED_IDLE: "疑似空闲（需人工确认）",
            State.UNKNOWN: "采集异常",
        }[self]

    @property
    def icon(self) -> str:
        return {
            State.BUSY: "🔴",
            State.IDLE: "🟢",
            State.RESERVED: "🔵",
            State.SUSPECTED_IDLE: "🟡",
            State.UNKNOWN: "⚠️",
        }[self]


class CommandResult:
    __slots__ = ("argv", "returncode", "stdout", "stderr", "error")

    def __init__(
        self,
        argv: Sequence[str],
        returncode: Optional[int],
        stdout: str = "",
        stderr: str = "",
        error: str = "",
    ) -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error == "" and self.returncode == 0

    @property
    def detail(self) -> str:
        return self.error or self.stderr or "exit={}".format(self.returncode)


class Container:
    __slots__ = ("name", "state")

    def __init__(self, name: str, state: str) -> None:
        self.name = name
        self.state = state

    @property
    def running(self) -> bool:
        return self.state.lower() == "running"

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "state": self.state}


class Claim:
    __slots__ = ("owner", "expires_at", "note")

    def __init__(self, owner: str, expires_at: datetime, note: str = "") -> None:
        self.owner = owner
        self.expires_at = expires_at
        self.note = note

    @property
    def active(self) -> bool:
        return self.expires_at > datetime.now(timezone.utc)


class Snapshot:
    def __init__(
        self,
        state: State,
        reasons: Optional[List[str]] = None,
        warnings: Optional[List[str]] = None,
        containers: Optional[Dict[str, List[Container]]] = None,
        pytest_active: Optional[bool] = None,
        recent_data: Optional[bool] = None,
        gpus: Optional[List[Dict[str, Any]]] = None,
        gpu_note: str = "",
        login_session_count: Optional[int] = None,
        login_users: Optional[List[str]] = None,
        login_note: str = "",
        primary_ip: str = "",
        ipv4_addresses: Optional[List[str]] = None,
        network_note: str = "",
        claim: Optional[Claim] = None,
    ) -> None:
        self.state = state
        self.reasons = reasons or []
        self.warnings = warnings or []
        self.containers = containers or {}
        self.pytest_active = pytest_active
        self.recent_data = recent_data
        self.gpus = gpus or []
        self.gpu_note = gpu_note
        self.login_session_count = login_session_count
        self.login_users = login_users or []
        self.login_note = login_note
        self.primary_ip = primary_ip
        self.ipv4_addresses = ipv4_addresses or []
        self.network_note = network_note
        self.claim = claim


# ----------------------------------------------------------------------
# 只读采集
# ----------------------------------------------------------------------
def run(argv: Sequence[str], timeout: int = COMMAND_TIMEOUT) -> CommandResult:
    args = tuple(argv)
    try:
        completed = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
        return CommandResult(
            args, completed.returncode, completed.stdout.strip(), completed.stderr.strip()
        )
    except FileNotFoundError:
        return CommandResult(args, None, error="命令不存在：{}".format(args[0]))
    except subprocess.TimeoutExpired:
        return CommandResult(args, None, error="命令超时：{}".format(args[0]))
    except Exception as exc:  # 采集失败必须冒泡成 UNKNOWN，绝不当成空闲
        return CommandResult(args, None, error="命令异常：{}".format(type(exc).__name__))


def list_core_containers() -> Tuple[Dict[str, List[Container]], List[str]]:
    result = run(("docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}"))
    if not result.ok:
        return {}, ["无法读取 Docker 容器：{}".format(result.detail)]

    all_containers: List[Container] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            all_containers.append(Container(parts[0].strip(), parts[1].strip()))

    by_role = {
        role: [item for item in all_containers if pattern.search(item.name)]
        for role, pattern in ROLE_PATTERNS.items()
    }
    if not any(by_role.values()):
        # 一个都没匹配上说明正则和现场不符，属于采集异常，不能当成空闲。
        return by_role, ["未匹配到任何核心容器，请核对容器名称正则"]
    return by_role, []


def check_test_processes(tester_containers: List[Container]) -> Tuple[Optional[bool], List[str]]:
    """只读检查 tester 容器里是否有测试进程。

    返回 (True 有 / False 无 / None 检查失败, 错误列表)。
    None 必须导致 UNKNOWN——检查不了不等于没有人在用。
    """
    running_testers = [item for item in tester_containers if item.running]
    if not running_testers:
        return False, []

    patterns = ["pytest"] + EXTRA_TEST_PROCESSES
    errors: List[str] = []
    for container in running_testers:
        for pattern in patterns:
            result = run(("docker", "exec", container.name, "pgrep", "-af", pattern))
            if result.error:
                errors.append("{} 内 {} 检查失败：{}".format(container.name, pattern, result.detail))
                break
            if result.returncode == 0:
                return True, []
            if result.returncode == 1:
                continue  # pgrep 语义：无匹配进程
            errors.append("{} 内 {} 检查失败：{}".format(container.name, pattern, result.detail))
            break
    if errors:
        return None, errors
    return False, []


def check_recent_data() -> Tuple[Optional[bool], List[str]]:
    if not DATA_DIR.is_dir():
        return None, ["数据目录不存在或不可读：{}".format(DATA_DIR)]
    result = run(
        (
            "find",
            str(DATA_DIR),
            "-xdev",
            "-type",
            "f",
            "-mmin",
            "-{}".format(IDLE_MINUTES),
            "-print",
            "-quit",
        )
    )
    if not result.ok:
        return None, ["数据活动检查失败：{}".format(result.detail)]
    return bool(result.stdout), []


def read_gpus() -> Tuple[List[Dict[str, Any]], str]:
    """GPU 仅供展示参考，永远不参与闲忙判定。"""
    result = run(
        (
            "nvidia-smi",
            "--query-gpu=index,utilization.gpu,memory.used",
            "--format=csv,noheader,nounits",
        )
    )
    if not result.ok:
        return [], "采集失败（不影响闲忙结论）"
    gpus: List[Dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = [item.strip() for item in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            gpus.append(
                {
                    "index": int(parts[0]),
                    "utilization": int(parts[1]),
                    "memory_used_mb": int(parts[2]),
                }
            )
        except ValueError:
            continue
    return gpus, "" if gpus else "无数据"


def read_login_sessions() -> Tuple[Optional[int], List[str], str]:
    """读取当前交互式登录会话。

    ``who`` 不修改系统，也不需要 root。它只能说明“谁登录着”，不能证明对方
    正在跑测试，因此结果只作为定位线索，不参与五态判定。
    """
    result = run(("who",))
    if not result.ok:
        return None, [], "登录会话采集失败（不影响闲忙结论）"
    rows = [line for line in result.stdout.splitlines() if line.strip()]
    users = sorted({line.split()[0] for line in rows if line.split()})
    return len(rows), users, ""


def read_network_identity() -> Tuple[str, List[str], str]:
    """读取服务器网络标识；失败只影响展示，不参与闲忙判定。

    默认路由地址用于区分批量部署的服务器；同时保留全部非回环 IPv4，
    方便双网卡、管理网/业务网并存的现场排障。
    """
    addresses: List[str] = []
    result = run(("ip", "-o", "-4", "addr", "show", "scope", "global"))
    if result.ok:
        for line in result.stdout.splitlines():
            parts = line.split()
            if "inet" not in parts:
                continue
            try:
                address = parts[parts.index("inet") + 1].split("/", 1)[0]
            except (IndexError, ValueError):
                continue
            if address and not address.startswith("127.") and address not in addresses:
                addresses.append(address)

    primary = ""
    try:
        # UDP connect 只让内核选择默认路由，不发送数据包。
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("223.5.5.5", 53))
            primary = str(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass

    if primary.startswith("127."):
        primary = ""
    if primary and primary not in addresses:
        addresses.insert(0, primary)
    if not primary and addresses:
        primary = addresses[0]

    note = "" if primary else "未读取到非回环 IPv4（不影响闲忙结论）"
    return primary, addresses, note


def read_claim() -> Tuple[Optional[Claim], List[str]]:
    """读取用户维护的保留声明。采集脚本只读，绝不写这个文件。"""
    if not CLAIM_FILE.exists():
        return None, []
    try:
        payload = json.loads(CLAIM_FILE.read_text(encoding="utf-8"))
        raw = str(payload["expires_at"]).replace("Z", "+00:00")
        expires_at = datetime.fromisoformat(raw)
        if expires_at.tzinfo is None:
            return None, ["保留声明 expires_at 必须包含时区"]
        claim = Claim(
            owner=str(payload.get("owner", "未填写")),
            expires_at=expires_at.astimezone(timezone.utc),
            note=str(payload.get("note", "")),
        )
        return (claim if claim.active else None), []
    except Exception as exc:
        return None, ["保留声明格式错误：{}".format(type(exc).__name__)]


def collect() -> Snapshot:
    """五态判定。判定顺序即交接文件第 4 节的优先级，不得调整。"""
    gpus, gpu_note = read_gpus()
    login_session_count, login_users, login_note = read_login_sessions()
    primary_ip, ipv4_addresses, network_note = read_network_identity()

    def snapshot(state: State, **kwargs: Any) -> Snapshot:
        """所有分支都带上辅助登录事实，避免早退时信息丢失。"""
        return Snapshot(
            state,
            gpus=gpus,
            gpu_note=gpu_note,
            login_session_count=login_session_count,
            login_users=login_users,
            login_note=login_note,
            primary_ip=primary_ip,
            ipv4_addresses=ipv4_addresses,
            network_note=network_note,
            **kwargs
        )

    # 1) 关键采集失败 → UNKNOWN
    by_role, docker_errors = list_core_containers()
    if docker_errors:
        return snapshot(State.UNKNOWN, reasons=docker_errors)

    claim, claim_warnings = read_claim()
    matched_roles = {role for role, items in by_role.items() if items}
    warnings = list(claim_warnings)
    missing_roles = set(ROLE_PATTERNS) - matched_roles
    if missing_roles:
        warnings.append("未匹配容器角色：" + "、".join(sorted(missing_roles)))

    all_roles_found = len(matched_roles) == len(ROLE_PATTERNS)
    all_core_stopped = all_roles_found and all(
        not item.running for items in by_role.values() for item in items
    )

    # 2) 核心容器全部停止 → 有保留则 RESERVED，否则 IDLE
    if all_core_stopped:
        if claim:
            return snapshot(
                State.RESERVED,
                reasons=["核心容器已停止，但 {} 的保留声明仍有效".format(claim.owner)],
                warnings=warnings,
                containers=by_role,
                claim=claim,
            )
        return snapshot(
            State.IDLE,
            reasons=["sim / proxy / tester 核心容器均已停止"],
            warnings=warnings,
            containers=by_role,
        )

    pytest_active, pytest_errors = check_test_processes(by_role.get("tester", []))
    recent_data, data_errors = check_recent_data()
    fatal_errors = pytest_errors + data_errors
    if fatal_errors:
        return snapshot(
            State.UNKNOWN,
            reasons=fatal_errors,
            warnings=warnings,
            containers=by_role,
            pytest_active=pytest_active,
            recent_data=recent_data,
            claim=claim,
        )

    # 3) 存在测试进程或近期数据写入 → BUSY
    if pytest_active or recent_data:
        reasons = []
        if pytest_active:
            reasons.append("检测到测试进程（pytest 等）")
        if recent_data:
            reasons.append("{} 最近 {} 分钟有文件写入".format(DATA_DIR, IDLE_MINUTES))
        return snapshot(
            State.BUSY,
            reasons=reasons,
            warnings=warnings,
            containers=by_role,
            pytest_active=pytest_active,
            recent_data=recent_data,
            claim=claim,
        )

    # 4) 无活动但有有效保留 → RESERVED
    if claim:
        return snapshot(
            State.RESERVED,
            reasons=[
                "{} 已声明保留到 {}".format(
                    claim.owner, claim.expires_at.astimezone().isoformat(timespec="minutes")
                )
            ],
            warnings=warnings,
            containers=by_role,
            pytest_active=False,
            recent_data=False,
            claim=claim,
        )

    # 5) 容器仍在、无活动、无保留 → SUSPECTED_IDLE（只能请人工确认）
    return snapshot(
        State.SUSPECTED_IDLE,
        reasons=["无测试进程，且 {} 最近 {} 分钟无写入".format(DATA_DIR, IDLE_MINUTES)],
        warnings=warnings,
        containers=by_role,
        pytest_active=False,
        recent_data=False,
    )


# ----------------------------------------------------------------------
# 事件构造
# ----------------------------------------------------------------------
def build_event(snapshot: Snapshot, suspect_streak: int, observed_at: datetime) -> Dict[str, Any]:
    suspected_minutes = suspect_streak * SAMPLE_SECONDS // 60
    return {
        "schema": SCHEMA,
        "event_id": "{}-{}-{}".format(
            MACHINE_NAME, int(observed_at.timestamp()), snapshot.state.value
        ),
        "cluster": MACHINE_NAME,
        "state": snapshot.state.value,
        "reasons": snapshot.reasons,
        "warnings": snapshot.warnings,
        "suspected_idle_minutes": suspected_minutes,
        "observed_at": observed_at.isoformat(timespec="seconds"),
        "facts": {
            "hostname": socket.gethostname(),
            "primary_ip": snapshot.primary_ip,
            "ipv4_addresses": snapshot.ipv4_addresses,
            "network_note": snapshot.network_note,
            "agent_version": AGENT_VERSION,
            "data_dir": str(DATA_DIR),
            "pytest_active": snapshot.pytest_active,
            "recent_data": snapshot.recent_data,
            "containers": {
                role: [item.as_dict() for item in items]
                for role, items in snapshot.containers.items()
            },
            "gpus": snapshot.gpus,
            "gpu_note": snapshot.gpu_note,
            "login_session_count": snapshot.login_session_count,
            "login_users": snapshot.login_users,
            "login_note": snapshot.login_note,
        },
    }


def format_message(event: Dict[str, Any], snapshot: Snapshot, suspect_streak: int) -> str:
    lines = [
        "【HIL2.0 监控事件｜供机器人处理】",
        "{} {} 集群：{}".format(snapshot.state.icon, MACHINE_NAME, snapshot.state.label),
        "主机：{}｜主 IP：{}".format(socket.gethostname(), snapshot.primary_ip or "未读取到"),
        "依据：" + "；".join(snapshot.reasons),
    ]
    if snapshot.state is State.SUSPECTED_IDLE:
        lines.append("已连续疑似空闲约 {} 分钟".format(event["suspected_idle_minutes"]))
        if suspect_streak >= CONFIRM_SAMPLES:
            lines.append("请当前使用者人工确认：仍在使用 / 已释放。系统不会自动释放资源。")
    if snapshot.warnings:
        lines.append("注意：" + "；".join(snapshot.warnings))
    gpu_text = snapshot.gpu_note or "; ".join(
        "GPU{}: {}% {}MiB".format(g["index"], g["utilization"], g["memory_used_mb"])
        for g in snapshot.gpus[:8]
    )
    lines.append("GPU参考（不参与判定）：{}".format(gpu_text or "无数据"))
    if snapshot.login_session_count is None:
        lines.append("登录会话（仅辅助定位）：采集失败")
    else:
        users = " / ".join(snapshot.login_users) or "无"
        lines.append(
            "登录会话（仅辅助定位）：{} 个；用户 {}".format(
                snapshot.login_session_count, users
            )
        )
    lines.append("采集时间：{}".format(event["observed_at"]))
    lines.append("EVENT_JSON=" + json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 投递
# ----------------------------------------------------------------------
def _post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
        parsed = json.loads(body) if body.strip() else {}
        code = parsed.get("code", parsed.get("StatusCode", 0))
        if code not in (0, None):
            return False, "接口返回失败 code={}".format(code)
        return True, ""
    except urllib.error.HTTPError as exc:
        return False, "HTTP {}".format(exc.code)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, type(exc).__name__


def _with_retry(label: str, sender) -> bool:
    delay = 2.0
    for attempt in range(1, DELIVERY_RETRIES + 1):
        ok, detail = sender()
        if ok:
            return True
        logger.warning("%s 投递失败（第 %s/%s 次）：%s", label, attempt, DELIVERY_RETRIES, detail)
        if attempt < DELIVERY_RETRIES:
            time.sleep(delay)
            delay *= 2
    return False


def push_webhook(text: str) -> bool:
    """监控群审计副本。显式 @ 业务机器人，否则 bot-to-bot 可能静默不投递。"""
    if not WEBHOOK_URL:
        return False
    if CONSUMER_BOT_OPEN_ID:
        message = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": "HIL2.0 监控事件｜{}".format(MACHINE_NAME),
                        "content": [
                            [
                                {"tag": "at", "user_id": CONSUMER_BOT_OPEN_ID},
                                {"tag": "text", "text": "\n" + text},
                            ]
                        ],
                    }
                }
            },
        }
    else:
        logger.warning("HIL_CONSUMER_BOT_OPEN_ID 未配置，监控群消息不会 @ 业务机器人")
        message = {"msg_type": "text", "content": {"text": text}}

    return _with_retry(
        "飞书 Webhook",
        lambda: _post_json(
            WEBHOOK_URL, message, {"Content-Type": "application/json; charset=utf-8"}
        ),
    )


def push_coordinator(event: Dict[str, Any]) -> bool:
    """直连协调服务（主通道）。"""
    if not COORDINATOR_URL:
        return False
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if COORDINATOR_TOKEN:
        headers["Authorization"] = "Bearer {}".format(COORDINATOR_TOKEN)
    return _with_retry("协调服务", lambda: _post_json(COORDINATOR_URL, event, headers))


def append_local_log(event: Dict[str, Any]) -> None:
    """本地 JSONL 落盘。灰度阶段可只开这一个通道跑 2～3 天做人工对照。"""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("本地事件日志写入失败：%s", type(exc).__name__)


# ----------------------------------------------------------------------
# 疑似空闲连击计数持久化（重启不清零）
# ----------------------------------------------------------------------
def load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("状态文件写入失败：%s", type(exc).__name__)


# ----------------------------------------------------------------------
# 主循环
# ----------------------------------------------------------------------
def run_once(persisted: Dict[str, Any], *, print_event: bool = False) -> Dict[str, Any]:
    observed_at = datetime.now().astimezone()
    snapshot = collect()

    suspect_streak = int(persisted.get("suspect_streak", 0))
    suspect_streak = suspect_streak + 1 if snapshot.state is State.SUSPECTED_IDLE else 0
    sample_count = int(persisted.get("sample_count", 0)) + 1
    last_state = persisted.get("last_state")

    event = build_event(snapshot, suspect_streak, observed_at)
    append_local_log(event)

    state_changed = snapshot.state.value != last_state
    needs_confirmation = (
        snapshot.state is State.SUSPECTED_IDLE and suspect_streak == CONFIRM_SAMPLES
    )
    heartbeat_due = HEARTBEAT_SAMPLES > 0 and sample_count % HEARTBEAT_SAMPLES == 0
    should_push = state_changed or needs_confirmation or heartbeat_due

    text = format_message(event, snapshot, suspect_streak)
    if print_event:
        print(text)

    delivered = {"webhook": False, "coordinator": False}
    if should_push and not DRY_RUN:
        delivered["coordinator"] = push_coordinator(event)
        delivered["webhook"] = push_webhook(text)
        if not any(delivered.values()):
            logger.error("两条通道都投递失败，事件只留在本地日志：%s", event["event_id"])
    elif should_push:
        logger.info("[DRY-RUN] 本应推送事件：%s", event["event_id"])

    logger.info(
        "%s %s streak=%s push=%s %s",
        snapshot.state.icon,
        snapshot.state.label,
        suspect_streak,
        should_push,
        "；".join(snapshot.reasons),
    )

    new_state = {
        "last_state": snapshot.state.value,
        "suspect_streak": suspect_streak,
        "sample_count": sample_count,
        "last_event_id": event["event_id"],
        "last_observed_at": event["observed_at"],
        "last_delivered": delivered,
    }
    save_state(new_state)
    return new_state


def main() -> None:
    parser = argparse.ArgumentParser(description="HIL2.0 集群侧只读采集")
    parser.add_argument("--once", action="store_true", help="只采集一次后退出")
    parser.add_argument("--print-event", action="store_true", help="把事件打印到标准输出")
    parser.add_argument("--dry-run", action="store_true", help="只写本地日志，不发任何消息")
    args = parser.parse_args()

    global DRY_RUN
    DRY_RUN = DRY_RUN or args.dry_run

    logging.basicConfig(
        level=_env("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    logger.info(
        "hil-agent %s 启动｜集群=%s 数据目录=%s 采集间隔=%ss dry_run=%s",
        AGENT_VERSION,
        MACHINE_NAME,
        DATA_DIR,
        SAMPLE_SECONDS,
        DRY_RUN,
    )
    if not COORDINATOR_URL and not WEBHOOK_URL and not DRY_RUN:
        logger.warning("未配置 HIL_COORDINATOR_URL 或 FEISHU_WEBHOOK，事件只会写本地日志")

    persisted = load_state()
    if args.once:
        run_once(persisted, print_event=args.print_event)
        return

    while True:
        started = time.monotonic()
        try:
            persisted = run_once(persisted, print_event=args.print_event)
        except Exception:
            # 采集脚本必须活着；单轮异常不能让整个监控停掉。
            logger.exception("采集轮次异常，等待下一轮")
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, SAMPLE_SECONDS - elapsed))


if __name__ == "__main__":
    sys.exit(main())
