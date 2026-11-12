"""SQLite 状态存储：幂等去重、卡片生命周期、预约覆盖策略。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hil2.models import MonitorState, Reservation
from hil2.store import Store

from .conftest import make_event, make_reservation, utc


class TestEventIdempotency:
    def test_first_event_accepted(self, store: Store):
        assert store.record_event(make_event(event_id="e1")) is True

    def test_duplicate_event_id_rejected(self, store: Store):
        event = make_event(event_id="e1")
        assert store.record_event(event) is True
        assert store.record_event(event) is False

    def test_duplicate_rejected_across_channels(self, store: Store):
        # 同一份 JSON 会同时走「直连」和「监控群转发」两条通道，只能生效一次。
        payload = make_event(event_id="dual-1", source="direct")
        relayed = make_event(event_id="dual-1", source="feishu_monitor_chat")
        assert store.record_event(payload) is True
        assert store.record_event(relayed) is False

    def test_seen_event_reflects_state(self, store: Store):
        assert store.seen_event("e9") is False
        store.record_event(make_event(event_id="e9"))
        assert store.seen_event("e9") is True


class TestSnapshots:
    def test_snapshot_reflects_latest(self, store: Store):
        store.record_event(make_event("52", "busy", event_id="a", observed_at=utc(minutes=-5)))
        store.record_event(make_event("52", "idle", event_id="b", observed_at=utc(minutes=-1)))
        snapshot = store.get_snapshot("52")
        assert snapshot["state"] is MonitorState.IDLE

    def test_out_of_order_event_does_not_overwrite_newer(self, store: Store):
        store.record_event(make_event("52", "idle", event_id="new", observed_at=utc(minutes=-1)))
        store.record_event(make_event("52", "busy", event_id="old", observed_at=utc(minutes=-30)))
        # 迟到的旧事件仍然入库（审计），但不能把快照倒退回去。
        assert store.get_snapshot("52")["state"] is MonitorState.IDLE
        assert store.seen_event("old") is True

    def test_facts_round_trip(self, store: Store):
        store.record_event(make_event(
            "52", "busy", event_id="f1",
            facts={
                "hostname": "hil-52",
                "containers": {"sim": [{"name": "s1", "state": "running"}]},
                "gpus": [{"index": 0, "utilization": 90, "memory_used_mb": 1024}],
                "pytest_active": True,
            },
        ))
        facts = store.get_snapshot("52")["facts"]
        assert facts.hostname == "hil-52"
        assert facts.containers["sim"][0].running is True
        assert facts.gpus[0].utilization == 90

    def test_all_snapshots_covers_every_cluster(self, store: Store):
        for cluster in ("52", "58", "60"):
            store.record_event(make_event(cluster, "busy", event_id=f"e-{cluster}"))
        assert {item["cluster"] for item in store.all_snapshots()} == {"52", "58", "60"}

    def test_recent_events_newest_first(self, store: Store):
        store.record_event(make_event("52", "busy", event_id="e1", observed_at=utc(minutes=-9)))
        store.record_event(make_event("52", "idle", event_id="e2", observed_at=utc(minutes=-1)))
        assert [item["event_id"] for item in store.recent_events("52")] == ["e2", "e1"]


class TestCards:
    def test_create_returns_unique_token(self, store: Store):
        first = store.create_card(cluster="52", current_open_id="ou_a", reason="r")
        second = store.create_card(cluster="52", current_open_id="ou_a", reason="r")
        assert first != second

    def test_new_card_supersedes_previous(self, store: Store):
        old = store.create_card(cluster="52", current_open_id="ou_a", reason="r")
        new = store.create_card(cluster="52", current_open_id="ou_a", reason="r")
        assert store.get_card(old)["status"] == "superseded"
        assert store.active_card_token("52") == new

    def test_resolve_once_only(self, store: Store):
        token = store.create_card(cluster="52", current_open_id="ou_a", reason="r")
        assert store.resolve_card(token, "extend_reservation") is True
        # 第二次点击必须失败，防止飞书重试造成重复延期 / 重复通知。
        assert store.resolve_card(token, "extend_reservation") is False

    def test_superseded_card_cannot_be_resolved(self, store: Store):
        old = store.create_card(cluster="52", current_open_id="ou_a", reason="r")
        store.create_card(cluster="52", current_open_id="ou_a", reason="r")
        assert store.resolve_card(old, "released") is False

    def test_resolved_card_no_longer_active(self, store: Store):
        token = store.create_card(cluster="52", current_open_id="ou_a", reason="r")
        store.resolve_card(token, "released")
        assert store.active_card_token("52") is None

    def test_pending_cards_excludes_resolved(self, store: Store):
        keep = store.create_card(cluster="58", current_open_id="ou_b", reason="r")
        drop = store.create_card(cluster="60", current_open_id="ou_c", reason="r")
        store.resolve_card(drop, "released")
        assert [card["card_token"] for card in store.pending_cards()] == [keep]

    def test_reminder_and_escalation_marked_once(self, store: Store):
        token = store.create_card(cluster="52", current_open_id="ou_a", reason="r")
        store.mark_card_reminded(token)
        first = store.get_card(token)["reminded_at"]
        store.mark_card_reminded(token)
        assert store.get_card(token)["reminded_at"] == first

        store.mark_card_escalated(token)
        assert store.get_card(token)["escalated_at"] is not None

    def test_message_id_attached(self, store: Store):
        token = store.create_card(cluster="52", current_open_id="ou_a", reason="r")
        store.attach_card_message(token, "om_1")
        assert store.get_card(token)["message_id"] == "om_1"

    def test_cards_are_per_cluster(self, store: Store):
        a = store.create_card(cluster="52", current_open_id="ou_a", reason="r")
        b = store.create_card(cluster="58", current_open_id="ou_b", reason="r")
        assert store.active_card_token("52") == a
        assert store.active_card_token("58") == b


class TestReservations:
    def test_missing_cluster_returns_empty_reservation(self, store: Store):
        reservation = store.get_reservation("99")
        assert reservation.cluster == "99"
        assert reservation.current_open_id is None

    def test_upsert_and_read_back(self, store: Store):
        store.upsert_reservation(make_reservation("52", handoff_minutes=30))
        reservation = store.get_reservation("52")
        assert reservation.current_open_id == "ou_current"
        assert reservation.next_open_id == "ou_next"
        assert reservation.handoff_due is False

    def test_upstream_sync_keeps_local_hold(self, store: Store):
        # 用户刚点了「继续 30 分钟」，上游预约接口不能把这个覆盖掉。
        store.upsert_reservation(make_reservation("52"))
        store.extend_reservation("52", 30)
        held = store.get_reservation("52").reserved_until

        store.upsert_reservation(make_reservation("52"))
        assert store.get_reservation("52").reserved_until == held

    def test_owner_change_drops_local_hold(self, store: Store):
        # 换人了，上一位的保留立刻作废，否则新占用人会被误判成保留中。
        store.upsert_reservation(make_reservation("52", current="ou_a"))
        store.extend_reservation("52", 30)
        store.upsert_reservation(make_reservation("52", current="ou_b"))
        assert store.get_reservation("52").reserved_until is None

    def test_extend_sets_future_expiry(self, store: Store):
        expires = store.extend_reservation("52", 60)
        assert expires > datetime.now(timezone.utc) + timedelta(minutes=59)
        assert store.get_reservation("52").reserved_until is not None

    def test_release_clears_current_but_keeps_next(self, store: Store):
        store.upsert_reservation(make_reservation("52", handoff_minutes=10))
        store.extend_reservation("52", 30)
        store.release_reservation("52")
        reservation = store.get_reservation("52")
        assert reservation.current_open_id is None
        assert reservation.reserved_until is None
        # 下一位仍在队列里，等着被通知。
        assert reservation.next_open_id == "ou_next"


class TestIncidents:
    def test_open_and_resolve(self, store: Store):
        store.open_incident(cluster="52", kind="collection_failed", detail="docker 不可读")
        assert len(store.open_incidents()) == 1
        store.resolve_incidents(cluster="52", kind="collection_failed")
        assert store.open_incidents() == []

    def test_same_kind_deduplicated(self, store: Store):
        store.open_incident(cluster="52", kind="collection_failed", detail="第一次")
        store.open_incident(cluster="52", kind="collection_failed", detail="第二次")
        incidents = store.open_incidents()
        assert len(incidents) == 1
        assert incidents[0]["detail"] == "第二次"

    def test_different_kinds_coexist(self, store: Store):
        store.open_incident(cluster="52", kind="collection_failed")
        store.open_incident(cluster="52", kind="delivery_failed")
        assert len(store.open_incidents()) == 2

    def test_resolve_all_kinds_for_cluster(self, store: Store):
        store.open_incident(cluster="52", kind="a")
        store.open_incident(cluster="52", kind="b")
        store.open_incident(cluster="58", kind="a")
        store.resolve_incidents(cluster="52")
        assert [item["cluster"] for item in store.open_incidents()] == ["58"]


class TestNotifications:
    def test_logged_and_listed_newest_first(self, store: Store):
        store.log_notification(cluster="52", kind="audit_only", note="第一条", delivered=True)
        store.log_notification(cluster="58", kind="alert_admin", note="第二条", delivered=False,
                               error="FeishuError")
        recent = store.recent_notifications()
        assert len(recent) == 2
        assert {item["kind"] for item in recent} == {"audit_only", "alert_admin"}
        failed = [item for item in recent if not item["delivered"]]
        assert failed[0]["error"] == "FeishuError"


class TestCallbackIdempotency:
    def test_first_claim_wins(self, store: Store):
        assert store.claim_callback("evt-1") is True
        assert store.claim_callback("evt-1") is False

    def test_result_replayed_on_retry(self, store: Store):
        store.claim_callback("evt-1")
        store.save_callback_result("evt-1", {"accepted": True, "toast": "已延长"})
        assert store.get_callback_result("evt-1")["toast"] == "已延长"

    def test_unknown_callback_returns_none(self, store: Store):
        assert store.get_callback_result("nope") is None


class TestPersistence:
    def test_survives_reopen(self, tmp_path):
        path = tmp_path / "persist.db"
        first = Store(path)
        first.record_event(make_event("52", "busy", event_id="p1"))
        token = first.create_card(cluster="52", current_open_id="ou_a", reason="r")
        first.close()

        second = Store(path)
        try:
            # 重启后必须仍然记得已处理过的事件和未结束的卡片。
            assert second.record_event(make_event("52", "busy", event_id="p1")) is False
            assert second.active_card_token("52") == token
        finally:
            second.close()
