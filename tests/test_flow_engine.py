"""决策引擎：交接文件第 4/5 节的每一条分支。

这是风险最高的模块，所有分支都必须显式覆盖，尤其是三条安全边界。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hil2.flow_engine import (
    Action,
    ActionKind,
    QueueContext,
    decide,
    decide_timeout,
    handle_card_action,
)
from hil2.models import MonitorState

from .conftest import make_event, make_reservation, utc


def kinds(actions: list[Action]) -> list[ActionKind]:
    return [action.kind for action in actions]


def queue(**kwargs) -> QueueContext:
    base = {"cluster": "52", "current_open_id": "ou_current", "next_open_id": "ou_next"}
    base.update(kwargs)
    return QueueContext(**base)


# ----------------------------------------------------------------------
# 采集异常
# ----------------------------------------------------------------------
class TestUnknownNeverBecomesIdle:
    def test_unknown_alerts_admin_only(self):
        event = make_event("52", "unknown", reasons=("无法读取 Docker 容器",))
        actions = decide(event, queue())
        assert kinds(actions) == [ActionKind.ALERT_ADMIN]
        assert "禁止判空闲" in actions[0].note

    def test_unknown_never_notifies_next_even_with_queue(self):
        event = make_event("52", "unknown")
        actions = decide(event, queue(next_open_id="ou_waiting"))
        assert ActionKind.NOTIFY_NEXT_AVAILABLE not in kinds(actions)

    def test_unknown_carries_reason_to_admin(self):
        event = make_event("52", "unknown", reasons=("/data1 不可读", "docker 权限不足"))
        note = decide(event, queue())[0].note
        assert "/data1 不可读" in note and "docker 权限不足" in note

    def test_cluster_mismatch_alerts_admin(self):
        # 队列和事件对不上说明配置错了，不能按任何一边的结论行事。
        actions = decide(make_event("58", "idle"), queue(cluster="52"))
        assert kinds(actions) == [ActionKind.ALERT_ADMIN]
        assert "不一致" in actions[0].note


# ----------------------------------------------------------------------
# 明确空闲
# ----------------------------------------------------------------------
class TestIdle:
    def test_idle_notifies_next_without_disturbing_previous(self):
        actions = decide(make_event("52", "idle"), queue())
        assert kinds(actions) == [ActionKind.NOTIFY_NEXT_AVAILABLE]
        assert actions[0].target_open_id == "ou_next"
        assert ActionKind.ASK_CURRENT_USER not in kinds(actions)

    def test_idle_without_queue_is_audit_only(self):
        actions = decide(make_event("52", "idle"), queue(next_open_id=None))
        assert kinds(actions) == [ActionKind.AUDIT_ONLY]

    def test_idle_during_active_hold_does_not_notify_next(self):
        # 用户刚点了「继续使用 30 分钟」，即便容器停了也要等保留期结束。
        actions = decide(make_event("52", "idle"), queue(reserved_until=utc(minutes=20)))
        assert kinds(actions) == [ActionKind.AUDIT_ONLY]

    def test_expired_hold_no_longer_blocks(self):
        actions = decide(make_event("52", "idle"), queue(reserved_until=utc(minutes=-1)))
        assert kinds(actions) == [ActionKind.NOTIFY_NEXT_AVAILABLE]


# ----------------------------------------------------------------------
# 疑似空闲
# ----------------------------------------------------------------------
class TestSuspectedIdle:
    def test_below_threshold_stays_silent(self):
        event = make_event("52", "suspected_idle", suspected_idle_minutes=10)
        assert kinds(decide(event, queue())) == [ActionKind.AUDIT_ONLY]

    def test_at_threshold_asks_current_user(self):
        event = make_event("52", "suspected_idle", suspected_idle_minutes=15)
        actions = decide(event, queue())
        assert kinds(actions) == [ActionKind.ASK_CURRENT_USER]
        assert actions[0].target_open_id == "ou_current"

    def test_never_notifies_next_before_confirmation(self):
        # 交接文件第 4 节：确认前不得通知下一位。
        event = make_event("52", "suspected_idle", suspected_idle_minutes=99)
        assert ActionKind.NOTIFY_NEXT_AVAILABLE not in kinds(decide(event, queue()))

    def test_no_current_user_escalates_to_admin(self):
        event = make_event("52", "suspected_idle", suspected_idle_minutes=20)
        actions = decide(event, queue(current_open_id=None))
        assert kinds(actions) == [ActionKind.ALERT_ADMIN]
        assert "找不到当前占用人" in actions[0].note

    def test_does_not_resend_while_card_pending(self):
        event = make_event("52", "suspected_idle", suspected_idle_minutes=40)
        actions = decide(event, queue(current_card_token="tok-1"))
        assert kinds(actions) == [ActionKind.AUDIT_ONLY]
        assert "等待当前占用人回复" in actions[0].note

    def test_active_hold_suppresses_repeat_prompt(self):
        event = make_event("52", "suspected_idle", suspected_idle_minutes=40)
        actions = decide(event, queue(reserved_until=utc(minutes=25)))
        assert kinds(actions) == [ActionKind.AUDIT_ONLY]
        assert "不重复催问" in actions[0].note

    def test_custom_confirm_minutes_respected(self):
        event = make_event("52", "suspected_idle", suspected_idle_minutes=8)
        assert kinds(decide(event, queue(), confirm_minutes=5)) == [ActionKind.ASK_CURRENT_USER]


# ----------------------------------------------------------------------
# 使用中
# ----------------------------------------------------------------------
class TestBusy:
    def test_busy_is_quiet_by_default(self):
        actions = decide(make_event("52", "busy"), queue())
        assert kinds(actions) == [ActionKind.AUDIT_ONLY]

    def test_busy_at_handoff_with_waiting_next_asks_current(self):
        actions = decide(make_event("52", "busy"), queue(handoff_due=True))
        assert kinds(actions) == [ActionKind.ASK_CURRENT_USER]
        assert "交接时间" in actions[0].note

    def test_busy_at_handoff_without_next_stays_quiet(self):
        actions = decide(make_event("52", "busy"), queue(handoff_due=True, next_open_id=None))
        assert kinds(actions) == [ActionKind.AUDIT_ONLY]

    def test_busy_at_handoff_without_current_stays_quiet(self):
        # 不知道该 @ 谁就不要乱 @。交接文件第 12 条。
        actions = decide(make_event("52", "busy"), queue(handoff_due=True, current_open_id=None))
        assert kinds(actions) == [ActionKind.AUDIT_ONLY]

    def test_busy_during_hold_stays_quiet(self):
        actions = decide(
            make_event("52", "busy"), queue(handoff_due=True, reserved_until=utc(minutes=15))
        )
        assert kinds(actions) == [ActionKind.AUDIT_ONLY]

    def test_busy_does_not_resend_while_card_pending(self):
        actions = decide(
            make_event("52", "busy"), queue(handoff_due=True, current_card_token="tok-1")
        )
        assert kinds(actions) == [ActionKind.AUDIT_ONLY]


class TestReserved:
    def test_reserved_is_audit_only(self):
        actions = decide(make_event("52", "reserved"), queue())
        assert kinds(actions) == [ActionKind.AUDIT_ONLY]
        assert "保留" in actions[0].note


# ----------------------------------------------------------------------
# 卡片点击
# ----------------------------------------------------------------------
class TestCardAction:
    def _queue(self, **kwargs):
        return queue(current_card_token="tok-1", **kwargs)

    def _value(self, operation="continue_30", *, cluster="52", token="tok-1"):
        return {"operation": operation, "cluster": cluster, "card_token": token}

    def test_continue_30_extends(self):
        actions = handle_card_action(
            action_value=self._value("continue_30"),
            operator_open_id="ou_current",
            queue=self._queue(),
        )
        assert kinds(actions) == [ActionKind.EXTEND_RESERVATION]
        assert actions[0].minutes == 30

    def test_continue_60_extends(self):
        actions = handle_card_action(
            action_value=self._value("continue_60"),
            operator_open_id="ou_current",
            queue=self._queue(),
        )
        assert actions[0].minutes == 60

    def test_released_notifies_next_and_touches_nothing_else(self):
        actions = handle_card_action(
            action_value=self._value("released"),
            operator_open_id="ou_current",
            queue=self._queue(),
        )
        assert kinds(actions) == [ActionKind.RELEASE_AND_NOTIFY_NEXT]
        assert actions[0].target_open_id == "ou_next"

    def test_wrong_person_rejected(self):
        actions = handle_card_action(
            action_value=self._value(),
            operator_open_id="ou_someone_else",
            queue=self._queue(),
        )
        assert kinds(actions) == [ActionKind.REJECT_STALE_OR_UNAUTHORIZED]

    def test_empty_operator_rejected(self):
        actions = handle_card_action(
            action_value=self._value(), operator_open_id="", queue=self._queue()
        )
        assert kinds(actions) == [ActionKind.REJECT_STALE_OR_UNAUTHORIZED]

    def test_stale_token_rejected(self):
        actions = handle_card_action(
            action_value=self._value(token="tok-OLD"),
            operator_open_id="ou_current",
            queue=self._queue(),
        )
        assert kinds(actions) == [ActionKind.REJECT_STALE_OR_UNAUTHORIZED]

    def test_no_active_card_rejected(self):
        actions = handle_card_action(
            action_value=self._value(),
            operator_open_id="ou_current",
            queue=queue(current_card_token=None),
        )
        assert kinds(actions) == [ActionKind.REJECT_STALE_OR_UNAUTHORIZED]

    def test_wrong_cluster_rejected(self):
        actions = handle_card_action(
            action_value=self._value(cluster="58"),
            operator_open_id="ou_current",
            queue=self._queue(),
        )
        assert kinds(actions) == [ActionKind.REJECT_STALE_OR_UNAUTHORIZED]

    def test_unknown_operation_rejected(self):
        actions = handle_card_action(
            action_value=self._value("shutdown_cluster"),
            operator_open_id="ou_current",
            queue=self._queue(),
        )
        assert kinds(actions) == [ActionKind.REJECT_STALE_OR_UNAUTHORIZED]

    def test_release_without_next_still_releases(self):
        actions = handle_card_action(
            action_value=self._value("released"),
            operator_open_id="ou_current",
            queue=self._queue(next_open_id=None),
        )
        assert kinds(actions) == [ActionKind.RELEASE_AND_NOTIFY_NEXT]
        assert actions[0].target_open_id is None

    def test_no_action_can_stop_a_machine(self):
        # 穷举所有可能的按钮值，确认引擎永远不会产出「操作机器」类动作。
        allowed = set(ActionKind)
        for operation in ("continue_30", "continue_60", "released", "kill", "", "restart"):
            actions = handle_card_action(
                action_value=self._value(operation),
                operator_open_id="ou_current",
                queue=self._queue(),
            )
            assert all(action.kind in allowed for action in actions)


# ----------------------------------------------------------------------
# 超时
# ----------------------------------------------------------------------
class TestTimeout:
    def _call(self, waited, **kwargs):
        params = {
            "cluster": "52",
            "current_open_id": "ou_current",
            "admin_open_ids": ["ou_admin"],
            "waited_minutes": waited,
            "already_reminded": False,
            "already_escalated": False,
        }
        params.update(kwargs)
        return decide_timeout(**params)

    def test_before_10_minutes_nothing_happens(self):
        assert self._call(9) == []

    def test_10_minutes_reminds_once(self):
        actions = self._call(10)
        assert kinds(actions) == [ActionKind.REMIND_CURRENT_USER]
        assert "不会自动释放" in actions[0].note

    def test_reminder_not_repeated(self):
        assert self._call(15, already_reminded=True) == []

    def test_20_minutes_escalates_to_admin(self):
        actions = self._call(20)
        assert kinds(actions) == [ActionKind.ALERT_ADMIN]
        assert actions[0].target_open_id == "ou_admin"

    def test_escalation_not_repeated(self):
        assert self._call(40, already_reminded=True, already_escalated=True) == []

    def test_never_releases_no_matter_how_long(self):
        # 交接文件第 5 节与第 12 条：无论多久未回复都不能自动释放。
        for waited in (10, 20, 60, 600, 86_400):
            actions = self._call(waited)
            assert ActionKind.RELEASE_AND_NOTIFY_NEXT not in kinds(actions)
            assert ActionKind.NOTIFY_NEXT_AVAILABLE not in kinds(actions)

    def test_escalation_without_admin_still_alerts(self):
        actions = self._call(25, admin_open_ids=[])
        assert kinds(actions) == [ActionKind.ALERT_ADMIN]
        assert actions[0].target_open_id is None


class TestQueueContext:
    def test_built_from_reservation(self):
        reservation = make_reservation("52", handoff_minutes=-5, reserved_minutes=30)
        ctx = QueueContext.from_reservation(reservation, current_card_token="tok")
        assert ctx.cluster == "52"
        assert ctx.handoff_due is True
        assert ctx.reservation_active is True
        assert ctx.current_card_token == "tok"

    def test_reservation_active_false_when_absent_or_past(self):
        assert QueueContext(cluster="52").reservation_active is False
        assert QueueContext(cluster="52", reserved_until=utc(minutes=-1)).reservation_active is False
