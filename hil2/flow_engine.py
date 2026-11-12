"""HIL2.0 飞书交接决策引擎。

本模块不含任何飞书 SDK 调用：输入监控事件与预约上下文，输出显式动作列表。
风险最高的部分因此可以被完整单测覆盖。

安全边界（交接文件第 12 条，不得修改）：
1. 采集异常只告警管理员，绝不降级为空闲。
2. 只有强空闲信号（核心容器全停）才允许直接通知下一位。
3. 任何路径都不会自动释放集群——超时只提醒和升级。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from .models import MonitorEvent, MonitorState, Reservation

# 疑似空闲达到该分钟数才允许打扰当前占用人。
# 采集间隔 5 分钟 × 连续 3 次 ≈ 15 分钟，与交接文件第 4 节一致。
DEFAULT_CONFIRM_MINUTES = 15


class ActionKind(str, Enum):
    AUDIT_ONLY = "audit_only"
    ASK_CURRENT_USER = "ask_current_user"
    NOTIFY_NEXT_AVAILABLE = "notify_next_available"
    ALERT_ADMIN = "alert_admin"
    EXTEND_RESERVATION = "extend_reservation"
    RELEASE_AND_NOTIFY_NEXT = "release_and_notify_next"
    REJECT_STALE_OR_UNAUTHORIZED = "reject_stale_or_unauthorized"
    REMIND_CURRENT_USER = "remind_current_user"


@dataclass(frozen=True)
class QueueContext:
    """判定所需的预约上下文。由预约数据源和卡片状态合成。"""

    cluster: str
    current_open_id: str | None = None
    next_open_id: str | None = None
    handoff_due: bool = False
    current_card_token: str | None = None
    # 卡片「继续使用 30/60 分钟」写入；到期前不重复催问。
    reserved_until: datetime | None = None

    @property
    def reservation_active(self) -> bool:
        if self.reserved_until is None:
            return False
        return self.reserved_until > datetime.now(timezone.utc)

    @classmethod
    def from_reservation(
        cls,
        reservation: Reservation,
        *,
        current_card_token: str | None = None,
    ) -> "QueueContext":
        return cls(
            cluster=reservation.cluster,
            current_open_id=reservation.current_open_id,
            next_open_id=reservation.next_open_id,
            handoff_due=reservation.handoff_due,
            current_card_token=current_card_token,
            reserved_until=reservation.reserved_until,
        )


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    cluster: str
    target_open_id: str | None = None
    note: str = ""
    minutes: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "cluster": self.cluster,
            "target_open_id": self.target_open_id,
            "note": self.note,
            "minutes": self.minutes,
        }


def decide(
    event: MonitorEvent,
    queue: QueueContext,
    *,
    confirm_minutes: int = DEFAULT_CONFIRM_MINUTES,
) -> list[Action]:
    """把一次监控事件转换成副作用；任何分支都不会自动释放。"""
    if event.cluster != queue.cluster:
        return [Action(ActionKind.ALERT_ADMIN, event.cluster, note="队列与事件集群不一致")]

    # 1) 采集异常优先级最高：只告警管理员，绝不判空闲。
    if event.state is MonitorState.UNKNOWN:
        detail = "；".join(event.reasons) or "关键采集失败"
        return [
            Action(ActionKind.ALERT_ADMIN, event.cluster, note=f"采集异常，禁止判空闲：{detail}")
        ]

    # 2) 强空闲信号：核心容器全停。此时不打扰上一位，直接告知下一位。
    if event.state is MonitorState.IDLE:
        if queue.reservation_active:
            return [
                Action(
                    ActionKind.AUDIT_ONLY,
                    event.cluster,
                    note="核心容器已停止，但用户确认的保留期未到，不通知下一位",
                )
            ]
        if queue.next_open_id:
            return [
                Action(
                    ActionKind.NOTIFY_NEXT_AVAILABLE,
                    event.cluster,
                    target_open_id=queue.next_open_id,
                    note="已由强空闲信号确认可用；不打扰上一位用户",
                )
            ]
        return [Action(ActionKind.AUDIT_ONLY, event.cluster, note="明确空闲，但当前无人排队")]

    # 3) 疑似空闲：只能请人工确认，确认前不得通知下一位。
    if event.state is MonitorState.SUSPECTED_IDLE:
        if event.suspected_idle_minutes < confirm_minutes:
            return [
                Action(ActionKind.AUDIT_ONLY, event.cluster, note="疑似空闲尚未达到确认阈值")
            ]
        if queue.reservation_active:
            return [
                Action(ActionKind.AUDIT_ONLY, event.cluster, note="用户已确认保留，到期前不重复催问")
            ]
        if queue.current_card_token:
            return [
                Action(ActionKind.AUDIT_ONLY, event.cluster, note="确认卡片已发出，等待当前占用人回复")
            ]
        if queue.current_open_id:
            return [
                Action(
                    ActionKind.ASK_CURRENT_USER,
                    event.cluster,
                    target_open_id=queue.current_open_id,
                    note="容器仍在，但无 pytest、无近期数据写入；请人工确认",
                )
            ]
        return [Action(ActionKind.ALERT_ADMIN, event.cluster, note="疑似空闲，但找不到当前占用人")]

    # 4) 使用中：有真实活动就不要每轮打扰，只在交接到期且有人等待时询问。
    if event.state is MonitorState.BUSY:
        if queue.reservation_active:
            return [Action(ActionKind.AUDIT_ONLY, event.cluster, note="有效保留期内，存在真实活动")]
        if queue.current_card_token:
            return [Action(ActionKind.AUDIT_ONLY, event.cluster, note="确认卡片已发出，等待回复")]
        if queue.handoff_due and queue.next_open_id and queue.current_open_id:
            return [
                Action(
                    ActionKind.ASK_CURRENT_USER,
                    event.cluster,
                    target_open_id=queue.current_open_id,
                    note="检测到真实活动，但预约已到交接时间且下一位正在等待",
                )
            ]
        return [Action(ActionKind.AUDIT_ONLY, event.cluster, note="存在真实活动，无需打扰")]

    # 5) 保留中：到期前不打扰。
    return [Action(ActionKind.AUDIT_ONLY, event.cluster, note="有效保留期内")]


def handle_card_action(
    *,
    action_value: dict[str, Any],
    operator_open_id: str,
    queue: QueueContext,
    now: datetime | None = None,
) -> list[Action]:
    """校验一次卡片点击，返回给预约服务执行的动作。

    四道校验缺一不可：集群一致、点击人是当前占用人、存在有效卡片、token 匹配。
    """
    now = now or datetime.now(timezone.utc)
    token = str(action_value.get("card_token", ""))
    cluster = str(action_value.get("cluster", ""))
    operation = str(action_value.get("operation", ""))

    if (
        cluster != queue.cluster
        or not operator_open_id
        or operator_open_id != queue.current_open_id
        or not queue.current_card_token
        or token != queue.current_card_token
    ):
        return [
            Action(
                ActionKind.REJECT_STALE_OR_UNAUTHORIZED,
                queue.cluster,
                target_open_id=operator_open_id or None,
                note="卡片已过期，或点击人不是当前占用人",
            )
        ]

    if operation in {"continue_30", "continue_60"}:
        minutes = 30 if operation.endswith("30") else 60
        expires_at = now + timedelta(minutes=minutes)
        return [
            Action(
                ActionKind.EXTEND_RESERVATION,
                queue.cluster,
                target_open_id=operator_open_id,
                minutes=minutes,
                note=f"保留至 {expires_at.astimezone().isoformat(timespec='minutes')}",
            )
        ]

    if operation == "released":
        return [
            Action(
                ActionKind.RELEASE_AND_NOTIFY_NEXT,
                queue.cluster,
                target_open_id=queue.next_open_id,
                note="当前用户已人工确认释放",
            )
        ]

    return [
        Action(
            ActionKind.REJECT_STALE_OR_UNAUTHORIZED,
            queue.cluster,
            target_open_id=operator_open_id or None,
            note="未知的卡片操作",
        )
    ]


def decide_timeout(
    *,
    cluster: str,
    current_open_id: str | None,
    admin_open_ids: list[str],
    waited_minutes: int,
    already_reminded: bool,
    already_escalated: bool,
    remind_after: int = 10,
    escalate_after: int = 20,
) -> list[Action]:
    """卡片超时未回复的处理。无论多久都不释放集群。"""
    if waited_minutes >= escalate_after and not already_escalated:
        return [
            Action(
                ActionKind.ALERT_ADMIN,
                cluster,
                target_open_id=admin_open_ids[0] if admin_open_ids else None,
                note=f"占用确认卡片已 {waited_minutes} 分钟无人回复，需人工处理；系统不会自动释放",
                minutes=waited_minutes,
            )
        ]
    if waited_minutes >= remind_after and not already_reminded and current_open_id:
        return [
            Action(
                ActionKind.REMIND_CURRENT_USER,
                cluster,
                target_open_id=current_open_id,
                note=f"已 {waited_minutes} 分钟未回复，提醒一次；系统不会自动释放集群",
                minutes=waited_minutes,
            )
        ]
    return []
