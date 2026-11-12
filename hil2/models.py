"""领域模型：监控事件、集群快照、预约上下文。

``hil.monitor.v1`` 是集群侧采集脚本与协调服务之间的唯一契约。
解析保持宽松（未知字段忽略、缺失字段降级），但绝不把解析失败当成空闲——
无法可靠解析的事件一律落到 UNKNOWN。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

SCHEMA = "hil.monitor.v1"


class MonitorState(str, Enum):
    BUSY = "busy"
    IDLE = "idle"
    RESERVED = "reserved"
    SUSPECTED_IDLE = "suspected_idle"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return _STATE_LABEL[self]

    @classmethod
    def parse(cls, raw: object) -> "MonitorState":
        """未知状态一律降级为 UNKNOWN，绝不猜成空闲。"""
        try:
            return cls(str(raw).strip().lower())
        except ValueError:
            return cls.UNKNOWN


_STATE_LABEL = {
    MonitorState.BUSY: "使用中",
    MonitorState.IDLE: "明确空闲",
    MonitorState.RESERVED: "保留中",
    MonitorState.SUSPECTED_IDLE: "疑似空闲",
    MonitorState.UNKNOWN: "采集异常",
}


class EventParseError(ValueError):
    """事件无法解析。调用方必须按采集异常处理，不得忽略。"""


def parse_datetime(raw: object) -> datetime:
    """解析 ISO8601；无时区视为错误，避免跨时区把过期算成新鲜。"""
    if isinstance(raw, datetime):
        parsed = raw
    else:
        text = str(raw or "").strip().replace("Z", "+00:00")
        if not text:
            raise EventParseError("缺少时间字段")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise EventParseError(f"时间格式错误：{text!r}") from exc
    if parsed.tzinfo is None:
        raise EventParseError(f"时间必须带时区：{raw!r}")
    return parsed.astimezone(timezone.utc)


def _str_tuple(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, Iterable):
        return tuple(str(item) for item in raw)
    return (str(raw),)


@dataclass(frozen=True)
class ContainerFact:
    name: str
    state: str

    @property
    def running(self) -> bool:
        return self.state.lower() == "running"

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "running": self.running}


@dataclass(frozen=True)
class GpuFact:
    index: int
    utilization: int | None = None
    memory_used_mb: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "utilization": self.utilization,
            "memory_used_mb": self.memory_used_mb,
        }


@dataclass(frozen=True)
class ClusterFacts:
    """采集到的机器事实。仅供展示与排障，不参与闲忙判定。

    交接文件第 2 条：GPU 高不能证明在用，GPU 低不能证明空闲。
    """

    hostname: str = ""
    primary_ip: str = ""
    ipv4_addresses: tuple[str, ...] = ()
    network_note: str = ""
    agent_version: str = ""
    containers: dict[str, tuple[ContainerFact, ...]] = field(default_factory=dict)
    pytest_active: bool | None = None
    recent_data: bool | None = None
    data_dir: str = ""
    gpus: tuple[GpuFact, ...] = ()
    gpu_note: str = ""
    # 登录会话只用于辅助定位“谁可能在机器上”，绝不单独决定闲忙。
    login_session_count: int | None = None
    login_users: tuple[str, ...] = ()
    login_note: str = ""

    @classmethod
    def from_dict(cls, raw: object) -> "ClusterFacts":
        if not isinstance(raw, dict):
            return cls()
        containers: dict[str, tuple[ContainerFact, ...]] = {}
        raw_containers = raw.get("containers")
        # 事实字段只用于展示；结构不对就当没有，不能让整条事件解析失败。
        for role, items in (raw_containers if isinstance(raw_containers, dict) else {}).items():
            if not isinstance(items, list):
                continue
            containers[str(role)] = tuple(
                ContainerFact(str(item.get("name", "")), str(item.get("state", "")))
                for item in items
                if isinstance(item, dict)
            )
        gpus: list[GpuFact] = []
        raw_gpus = raw.get("gpus")
        for item in (raw_gpus if isinstance(raw_gpus, list) else []):
            if not isinstance(item, dict):
                continue
            try:
                gpus.append(
                    GpuFact(
                        index=int(item.get("index", len(gpus))),
                        utilization=_opt_int(item.get("utilization")),
                        memory_used_mb=_opt_int(item.get("memory_used_mb")),
                    )
                )
            except (TypeError, ValueError):
                continue
        return cls(
            hostname=str(raw.get("hostname", "")),
            primary_ip=str(raw.get("primary_ip", "")),
            ipv4_addresses=_str_tuple(raw.get("ipv4_addresses")),
            network_note=str(raw.get("network_note", "")),
            agent_version=str(raw.get("agent_version", "")),
            containers=containers,
            pytest_active=_opt_bool(raw.get("pytest_active")),
            recent_data=_opt_bool(raw.get("recent_data")),
            data_dir=str(raw.get("data_dir", "")),
            gpus=tuple(gpus),
            gpu_note=str(raw.get("gpu_note", "")),
            login_session_count=_opt_int(raw.get("login_session_count")),
            login_users=_str_tuple(raw.get("login_users")),
            login_note=str(raw.get("login_note", "")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "primary_ip": self.primary_ip,
            "ipv4_addresses": list(self.ipv4_addresses),
            "network_note": self.network_note,
            "agent_version": self.agent_version,
            "containers": {
                role: [item.as_dict() for item in items]
                for role, items in self.containers.items()
            },
            "pytest_active": self.pytest_active,
            "recent_data": self.recent_data,
            "data_dir": self.data_dir,
            "gpus": [gpu.as_dict() for gpu in self.gpus],
            "gpu_note": self.gpu_note,
            "login_session_count": self.login_session_count,
            "login_users": list(self.login_users),
            "login_note": self.login_note,
        }


def _opt_bool(raw: object) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes"}


def _opt_int(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class MonitorEvent:
    event_id: str
    cluster: str
    state: MonitorState
    observed_at: datetime
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    suspected_idle_minutes: int = 0
    facts: ClusterFacts = field(default_factory=ClusterFacts)
    source: str = "unknown"

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, source: str = "unknown") -> "MonitorEvent":
        if not isinstance(raw, dict):
            raise EventParseError("事件必须是 JSON 对象")
        schema = str(raw.get("schema", ""))
        if schema and schema != SCHEMA:
            raise EventParseError(f"不支持的 schema：{schema!r}")

        cluster = str(raw.get("cluster", "")).strip()
        if not cluster:
            raise EventParseError("缺少 cluster")

        observed_at = parse_datetime(raw.get("observed_at"))
        state = MonitorState.parse(raw.get("state"))

        event_id = str(raw.get("event_id", "")).strip()
        if not event_id:
            # 缺 event_id 时用内容摘要兜底，仍然保证幂等去重可用。
            digest = hashlib.sha256(
                f"{cluster}|{state.value}|{observed_at.isoformat()}".encode("utf-8")
            ).hexdigest()[:24]
            event_id = f"{cluster}-auto-{digest}"

        return cls(
            event_id=event_id,
            cluster=cluster,
            state=state,
            observed_at=observed_at,
            reasons=_str_tuple(raw.get("reasons")),
            warnings=_str_tuple(raw.get("warnings")),
            suspected_idle_minutes=_opt_int(raw.get("suspected_idle_minutes")) or 0,
            facts=ClusterFacts.from_dict(raw.get("facts")),
            source=source,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "event_id": self.event_id,
            "cluster": self.cluster,
            "state": self.state.value,
            "observed_at": self.observed_at.isoformat(),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "suspected_idle_minutes": self.suspected_idle_minutes,
            "facts": self.facts.as_dict(),
            "source": self.source,
        }


@dataclass(frozen=True)
class Reservation:
    """预约队列中的一条记录。协调服务只读取占用人与下一位，不猜测。"""

    cluster: str
    current_open_id: str | None = None
    current_name: str = ""
    next_open_id: str | None = None
    next_name: str = ""
    handoff_at: datetime | None = None
    # 卡片「继续使用 30/60 分钟」写入的保留到期时间。
    reserved_until: datetime | None = None

    @property
    def handoff_due(self) -> bool:
        if self.handoff_at is None:
            return False
        return datetime.now(timezone.utc) >= self.handoff_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "cluster": self.cluster,
            "current_open_id": self.current_open_id,
            "current_name": self.current_name,
            "next_open_id": self.next_open_id,
            "next_name": self.next_name,
            "handoff_at": self.handoff_at.isoformat() if self.handoff_at else None,
            "reserved_until": (
                self.reserved_until.isoformat() if self.reserved_until else None
            ),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Reservation":
        def _opt_dt(key: str) -> datetime | None:
            value = raw.get(key)
            if value in (None, ""):
                return None
            try:
                return parse_datetime(value)
            except EventParseError:
                # 时间坏了就当没有该约束，绝不因此宣告可用。
                return None

        return cls(
            cluster=str(raw.get("cluster", "")).strip(),
            current_open_id=(raw.get("current_open_id") or None),
            current_name=str(raw.get("current_name", "") or ""),
            next_open_id=(raw.get("next_open_id") or None),
            next_name=str(raw.get("next_name", "") or ""),
            handoff_at=_opt_dt("handoff_at"),
            reserved_until=_opt_dt("reserved_until"),
        )
