"""协调服务：动作执行、fail-closed 降级、超时扫描。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hil2.coordinator import (
    INCIDENT_COLLECTION,
    INCIDENT_DELIVERY,
    INCIDENT_RESERVATION,
    INCIDENT_UNRESERVED_USAGE,
    Coordinator,
)
from hil2.flow_engine import ActionKind

from .conftest import FakeFeishuClient, make_event, make_reservation, utc


def action_kinds(result: dict) -> list[str]:
    return [item["kind"] for item in result["actions"]]


class TestIngest:
    def test_duplicate_event_is_dropped(self, coordinator: Coordinator):
        event = make_event("52", "busy", event_id="dup")
        assert coordinator.ingest(event)["accepted"] is True
        second = coordinator.ingest(event)
        assert second["accepted"] is False
        assert second["duplicate"] is True
        assert second["actions"] == []

    def test_busy_stays_silent_in_business_chat(self, coordinator: Coordinator, client, source):
        source.reservations = [make_reservation("52")]
        coordinator.ingest(make_event("52", "busy"))
        assert client.cards_to("oc_business") == []

    def test_idle_sends_next_available_card(self, coordinator: Coordinator, client, source):
        source.reservations = [make_reservation("52")]
        result = coordinator.ingest(make_event("52", "idle"))
        assert action_kinds(result) == [ActionKind.NOTIFY_NEXT_AVAILABLE.value]
        cards = client.cards_to("oc_business")
        assert len(cards) == 1
        assert "可以使用" in cards[0]["card"]["header"]["title"]["content"]

    def test_suspected_idle_sends_confirmation_card(self, coordinator: Coordinator, client, source):
        source.reservations = [make_reservation("52")]
        result = coordinator.ingest(
            make_event("52", "suspected_idle", suspected_idle_minutes=20)
        )
        assert action_kinds(result) == [ActionKind.ASK_CURRENT_USER.value]
        card = client.cards_to("oc_business")[0]["card"]
        buttons = card["elements"][2]["actions"]
        assert [b["value"]["operation"] for b in buttons] == [
            "continue_30", "continue_60", "released"
        ]
        # 卡片必须写明系统不会自动释放。
        assert "不会因未回复而自动释放" in card["elements"][0]["content"]

    def test_unknown_alerts_admin_and_opens_incident(self, coordinator: Coordinator, client, store):
        coordinator.ingest(make_event("52", "unknown", reasons=("docker 不可读",)))
        alerts = client.cards_to("oc_monitor")
        assert len(alerts) == 1
        incidents = store.open_incidents()
        assert [item["kind"] for item in incidents] == [INCIDENT_COLLECTION]
        # 绝不能有任何「可以使用」的通知。
        assert client.cards_to("oc_business") == []

    def test_recovery_closes_collection_incident(self, coordinator: Coordinator, store):
        coordinator.ingest(make_event("52", "unknown", event_id="u1"))
        assert len(store.open_incidents()) == 1
        coordinator.ingest(make_event("52", "busy", event_id="b1"))
        assert not any(
            item["kind"] == INCIDENT_COLLECTION for item in store.open_incidents()
        )

    def test_busy_without_booking_alerts_once_and_shows_login_users(
        self, coordinator: Coordinator, store, client
    ):
        first = coordinator.ingest(make_event(
            "52", "busy", event_id="unbooked-1", reasons=("检测到 pytest",),
            facts={"login_session_count": 2, "login_users": ["jwj", "zcx"]},
        ))
        second = coordinator.ingest(make_event(
            "52", "busy", event_id="unbooked-2", reasons=("检测到 pytest",),
            facts={"login_session_count": 2, "login_users": ["jwj", "zcx"]},
        ))
        assert action_kinds(first) == [ActionKind.ALERT_ADMIN.value]
        assert action_kinds(second) == [ActionKind.AUDIT_ONLY.value]
        incident = next(
            item for item in store.open_incidents()
            if item["kind"] == INCIDENT_UNRESERVED_USAGE
        )
        assert "jwj / zcx" in incident["detail"]
        assert len(client.cards_to("oc_monitor")) == 1

    def test_booking_or_stopping_activity_closes_unreserved_incident(
        self, coordinator: Coordinator, source, store
    ):
        coordinator.ingest(make_event("52", "busy", event_id="unbooked-open"))
        source.reservations = [make_reservation("52")]
        coordinator._reservations_synced_at = None  # noqa: SLF001
        coordinator.ingest(make_event("52", "busy", event_id="unbooked-close"))
        assert not any(
            item["kind"] == INCIDENT_UNRESERVED_USAGE for item in store.open_incidents()
        )

    def test_card_token_persisted_and_active(self, coordinator: Coordinator, store, source):
        source.reservations = [make_reservation("52")]
        coordinator.ingest(make_event("52", "suspected_idle", suspected_idle_minutes=20))
        token = store.active_card_token("52")
        assert token is not None
        assert store.get_card(token)["message_id"] == "om_test_1"

    def test_second_suspected_event_does_not_spam(self, coordinator: Coordinator, client, source):
        source.reservations = [make_reservation("52")]
        coordinator.ingest(make_event("52", "suspected_idle", event_id="s1",
                                      suspected_idle_minutes=20))
        coordinator.ingest(make_event("52", "suspected_idle", event_id="s2",
                                      suspected_idle_minutes=25))
        # 卡片已经在等回复了，不能再发一张。
        assert len(client.cards_to("oc_business")) == 1


class TestFailClosed:
    def test_reservation_source_failure_keeps_last_known(self, coordinator: Coordinator, source, store):
        source.reservations = [make_reservation("52")]
        coordinator.sync_reservations(force=True)
        source.should_fail = True
        assert coordinator.sync_reservations(force=True) is False
        # 沿用上一次数据，而不是把集群当成无人占用。
        assert store.get_reservation("52").current_open_id == "ou_current"
        assert [i["kind"] for i in store.open_incidents()] == [INCIDENT_RESERVATION]

    def test_reservation_recovery_closes_incident(self, coordinator: Coordinator, source, store):
        source.should_fail = True
        coordinator.sync_reservations(force=True)
        source.should_fail = False
        coordinator.sync_reservations(force=True)
        assert store.open_incidents() == []

    def test_feishu_delivery_failure_recorded_not_swallowed(self, settings, store, source):
        source.reservations = [make_reservation("52")]
        failing = FakeFeishuClient(fail=True)
        coordinator = Coordinator(
            settings=settings, store=store, client=failing, reservation_source=source
        )
        result = coordinator.ingest(make_event("52", "idle"))
        assert result["actions"][0]["result"]["delivered"] is False
        assert [i["kind"] for i in store.open_incidents()] == [INCIDENT_DELIVERY]
        failed = [n for n in store.recent_notifications() if not n["delivered"]]
        assert failed and failed[0]["error"].startswith("FeishuError")

    def test_works_without_feishu_configured(self, settings, store, source):
        # 灰度阶段：不配飞书也要能跑，只写本地审计。
        source.reservations = [make_reservation("52")]
        coordinator = Coordinator(
            settings=settings, store=store, client=None, reservation_source=source
        )
        result = coordinator.ingest(make_event("52", "idle"))
        assert result["accepted"] is True
        assert result["actions"][0]["result"]["delivered"] is False
        assert store.recent_notifications()[0]["kind"] == "notify_next_available"

    def test_reservation_cache_avoids_hammering_source(self, coordinator: Coordinator, source):
        coordinator.sync_reservations(force=True)
        for _ in range(5):
            coordinator.sync_reservations()
        assert source.fetch_count == 1


class TestCardClick:
    def _prepare(self, coordinator, source, cluster="52"):
        source.reservations = [make_reservation(cluster)]
        coordinator.ingest(
            make_event(cluster, "suspected_idle", suspected_idle_minutes=20)
        )
        return coordinator.store.active_card_token(cluster)

    def test_continue_30_extends_and_invalidates_card(self, coordinator, source, store, client):
        token = self._prepare(coordinator, source)
        result = coordinator.handle_card_click(
            action_value={"operation": "continue_30", "cluster": "52", "card_token": token},
            operator_open_id="ou_current",
            operator_name="张工",
        )
        assert result["accepted"] is True
        assert store.get_reservation("52").reserved_until is not None
        assert store.active_card_token("52") is None
        # 原卡片被替换成结果态，按钮消失。
        assert client.updated and "已处理" in client.updated[0]["card"]["header"]["title"]["content"]

    def test_released_notifies_next_without_touching_machine(self, coordinator, source, store, client):
        token = self._prepare(coordinator, source)
        result = coordinator.handle_card_click(
            action_value={"operation": "released", "cluster": "52", "card_token": token},
            operator_open_id="ou_current",
        )
        assert result["accepted"] is True
        assert store.get_reservation("52").current_open_id is None
        titles = [c["card"]["header"]["title"]["content"] for c in client.cards_to("oc_business")]
        assert any("可以使用" in title for title in titles)
        # 审计里必须写明没有操作机器。
        notes = [n["note"] for n in store.recent_notifications()]
        assert any("未操作机器" in note for note in notes)

    def test_repeat_click_rejected(self, coordinator, source):
        token = self._prepare(coordinator, source)
        payload = {"operation": "continue_30", "cluster": "52", "card_token": token}
        coordinator.handle_card_click(action_value=payload, operator_open_id="ou_current")
        second = coordinator.handle_card_click(action_value=payload, operator_open_id="ou_current")
        assert second["accepted"] is False
        assert "已经处理过" in second["toast"]

    def test_wrong_person_rejected_and_nothing_changes(self, coordinator, source, store):
        token = self._prepare(coordinator, source)
        result = coordinator.handle_card_click(
            action_value={"operation": "released", "cluster": "52", "card_token": token},
            operator_open_id="ou_intruder",
        )
        assert result["accepted"] is False
        assert store.active_card_token("52") == token
        assert store.get_reservation("52").current_open_id == "ou_current"

    def test_stale_token_rejected(self, coordinator, source, store):
        self._prepare(coordinator, source)
        result = coordinator.handle_card_click(
            action_value={"operation": "released", "cluster": "52", "card_token": "tok-old"},
            operator_open_id="ou_current",
        )
        assert result["accepted"] is False
        assert store.get_reservation("52").current_open_id == "ou_current"


class TestTimeoutScan:
    def _pending_card(self, coordinator, source, age_minutes: int):
        source.reservations = [make_reservation("52")]
        coordinator.ingest(make_event("52", "suspected_idle", suspected_idle_minutes=20))
        token = coordinator.store.active_card_token("52")
        created = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
        with coordinator.store._tx() as conn:  # noqa: SLF001  测试内直接改时间
            conn.execute(
                "UPDATE cards SET created_at = ? WHERE card_token = ?",
                (created.isoformat(), token),
            )
        return token

    def test_quiet_before_10_minutes(self, coordinator, source):
        self._pending_card(coordinator, source, 5)
        assert coordinator.scan_timeouts() == []

    def test_reminds_at_10_minutes(self, coordinator, source, store, client):
        token = self._pending_card(coordinator, source, 11)
        performed = coordinator.scan_timeouts()
        assert [item["kind"] for item in performed] == [ActionKind.REMIND_CURRENT_USER.value]
        assert store.get_card(token)["reminded_at"] is not None
        assert any("提醒" in t for t in client.card_titles())

    def test_reminder_sent_once(self, coordinator, source):
        self._pending_card(coordinator, source, 11)
        coordinator.scan_timeouts()
        assert coordinator.scan_timeouts() == []

    def test_escalates_at_20_minutes(self, coordinator, source, store, client):
        token = self._pending_card(coordinator, source, 21)
        performed = coordinator.scan_timeouts()
        assert [item["kind"] for item in performed] == [ActionKind.ALERT_ADMIN.value]
        assert store.get_card(token)["escalated_at"] is not None
        assert any(i["kind"] == "unattended_confirmation" for i in store.open_incidents())

    def test_never_auto_releases_however_long(self, coordinator, source, store):
        self._pending_card(coordinator, source, 60 * 24)
        for _ in range(3):
            coordinator.scan_timeouts()
        reservation = store.get_reservation("52")
        # 一天没人回复，集群依然属于当前占用人。
        assert reservation.current_open_id == "ou_current"
        assert store.active_card_token("52") is not None
        kinds = {n["kind"] for n in store.recent_notifications()}
        assert ActionKind.RELEASE_AND_NOTIFY_NEXT.value not in kinds
        assert ActionKind.NOTIFY_NEXT_AVAILABLE.value not in kinds

    def test_answer_clears_unattended_incident(self, coordinator, source, store):
        token = self._pending_card(coordinator, source, 21)
        coordinator.scan_timeouts()
        assert any(i["kind"] == "unattended_confirmation" for i in store.open_incidents())
        coordinator.handle_card_click(
            action_value={"operation": "continue_60", "cluster": "52", "card_token": token},
            operator_open_id="ou_current",
        )
        assert not any(i["kind"] == "unattended_confirmation" for i in store.open_incidents())
