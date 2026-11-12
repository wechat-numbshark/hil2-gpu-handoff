"""大盘视图模型：分组、排序、过期判定、未纳管集群。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hil2 import dashboard
from hil2.store import Store

from .conftest import make_event, make_reservation, utc


def build(store, settings, **kwargs):
    return dashboard.build(store, settings, **kwargs)


def item_for(data, cluster):
    return next(item for item in data["clusters"] if item["cluster"] == cluster)


class TestGrouping:
    def test_no_data_for_managed_cluster_without_events(self, store, settings):
        data = build(store, settings)
        assert data["counters"]["no_data"] == 3
        assert item_for(data, "52")["state_label"] == "无数据"

    def test_each_state_maps_to_its_group(self, store, settings):
        pairs = {"52": ("busy", "busy"), "58": ("idle", "idle"), "60": ("unknown", "unknown")}
        for cluster, (state, _) in pairs.items():
            store.record_event(make_event(cluster, state, event_id=f"e-{cluster}"))
        data = build(store, settings)
        for cluster, (_, group) in pairs.items():
            assert item_for(data, cluster)["group"] == group

    def test_active_hold_shows_as_reserved_even_when_idle(self, store, settings):
        # 用户点了「继续 30 分钟」，即便采集说空闲，大盘也要显示已保留。
        store.record_event(make_event("52", "idle", event_id="e1"))
        store.upsert_reservation(make_reservation("52", reserved_minutes=25))
        assert item_for(build(store, settings), "52")["group"] == "reserved"

    def test_unmanaged_cluster_still_shown(self, store, settings):
        store.record_event(make_event("56", "busy", event_id="e56"))
        item = item_for(build(store, settings), "56")
        assert item["managed"] is False
        assert build(store, settings)["counters"]["total"] == 4


class TestOrdering:
    def test_urgent_groups_come_first(self, store, settings):
        store.record_event(make_event("52", "idle", event_id="a"))
        store.record_event(make_event("58", "unknown", event_id="b"))
        store.record_event(make_event("60", "suspected_idle", event_id="c"))
        order = [item["cluster"] for item in build(store, settings)["clusters"]]
        assert order[:2] == ["58", "60"]

    def test_numeric_clusters_sort_numerically(self, store, settings):
        for cluster in ("7", "52", "108"):
            store.record_event(make_event(cluster, "busy", event_id=f"e{cluster}"))
        busy = [i["cluster"] for i in build(store, settings)["clusters"] if i["group"] == "busy"]
        assert busy == ["7", "52", "108"]

    def test_non_numeric_cluster_sorts_after(self, store, settings):
        store.record_event(make_event("52", "busy", event_id="a"))
        store.record_event(make_event("benchA", "busy", event_id="b"))
        busy = [i["cluster"] for i in build(store, settings)["clusters"] if i["group"] == "busy"]
        assert busy == ["52", "benchA"]


class TestFreshness:
    def test_recent_data_not_stale(self, store, settings):
        store.record_event(make_event("52", "busy", event_id="a", observed_at=utc(minutes=-2)))
        item = item_for(build(store, settings), "52")
        assert item["stale"] is False
        assert 0 <= item["age_seconds"] < 200

    def test_old_data_marked_stale(self, store, settings):
        store.record_event(make_event("52", "busy", event_id="a", observed_at=utc(minutes=-180)))
        assert item_for(build(store, settings), "52")["stale"] is True

    def test_stale_keeps_last_known_state_not_idle(self, store, settings):
        # 过期只是「不知道现在怎样」，绝不能因此显示成空闲可用。
        store.record_event(make_event("52", "busy", event_id="a", observed_at=utc(minutes=-999)))
        item = item_for(build(store, settings), "52")
        assert item["group"] == "busy"
        assert item["stale"] is True

    def test_threshold_from_settings(self, store, settings):
        store.record_event(make_event("52", "busy", event_id="a", observed_at=utc(minutes=-25)))
        tight = settings.model_copy(update={"stale_after_minutes": 10})
        loose = settings.model_copy(update={"stale_after_minutes": 60})
        assert item_for(build(store, tight), "52")["stale"] is True
        assert item_for(build(store, loose), "52")["stale"] is False


class TestContainerView:
    def test_all_three_roles_always_present(self, store, settings):
        store.record_event(make_event("52", "busy", event_id="a", facts={
            "containers": {"sim": [{"name": "s", "state": "running"}]}
        }))
        roles = [c["role"] for c in item_for(build(store, settings), "52")["containers"]]
        assert roles == ["sim", "proxy", "tester"]

    def test_unmatched_role_flagged(self, store, settings):
        store.record_event(make_event("52", "busy", event_id="a", facts={
            "containers": {"sim": [{"name": "s", "state": "running"}]}
        }))
        containers = {c["role"]: c for c in item_for(build(store, settings), "52")["containers"]}
        assert containers["sim"]["matched"] is True
        assert containers["proxy"]["matched"] is False

    def test_running_count(self, store, settings):
        store.record_event(make_event("52", "busy", event_id="a", facts={
            "containers": {"tester": [
                {"name": "t1", "state": "running"},
                {"name": "t2", "state": "exited"},
            ]}
        }))
        tester = next(
            c for c in item_for(build(store, settings), "52")["containers"] if c["role"] == "tester"
        )
        assert (tester["running"], tester["total"]) == (1, 2)

    def test_login_sessions_are_visible_as_auxiliary_facts(self, store, settings):
        store.record_event(make_event("52", "busy", event_id="sessions", facts={
            "login_session_count": 2, "login_users": ["jwj", "zcx"]
        }))
        item = item_for(build(store, settings), "52")
        assert item["login_session_count"] == 2
        assert item["login_users"] == ["jwj", "zcx"]

    def test_network_identity_is_visible(self, store, settings):
        store.record_event(make_event("52", "busy", event_id="network", facts={
            "hostname": "hil-52",
            "primary_ip": "10.20.30.52",
            "ipv4_addresses": ["10.20.30.52", "172.18.0.1"],
        }))
        item = item_for(build(store, settings), "52")
        assert item["primary_ip"] == "10.20.30.52"
        assert item["ipv4_addresses"] == ["10.20.30.52", "172.18.0.1"]


class TestCountersAndPanels:
    def test_open_incidents_counted(self, store, settings):
        store.open_incident(cluster="52", kind="collection_failed", detail="d")
        data = build(store, settings)
        assert data["counters"]["open_incidents"] == 1
        assert data["incidents"][0]["cluster"] == "52"

    def test_pending_cards_counted(self, store, settings):
        store.create_card(cluster="52", current_open_id="ou_a", reason="r")
        data = build(store, settings)
        assert data["counters"]["pending_cards"] == 1
        assert item_for(data, "52")["pending_card"] is not None

    def test_notifications_included(self, store, settings):
        store.log_notification(cluster="52", kind="alert_admin", note="n", delivered=True)
        assert build(store, settings)["notifications"][0]["kind"] == "alert_admin"

    def test_safety_flags_always_true(self, store, settings):
        safety = build(store, settings)["safety"]
        assert safety["read_only"] is True and safety["never_auto_release"] is True

    def test_reservation_surfaced_with_handoff_flag(self, store, settings):
        store.upsert_reservation(make_reservation("52", handoff_minutes=-5))
        reservation = item_for(build(store, settings), "52")["reservation"]
        assert reservation["current_open_id"] == "ou_current"
        assert reservation["handoff_due"] is True

    def test_json_serialisable(self, store, settings):
        import json

        store.record_event(make_event("52", "busy", event_id="a", facts={
            "gpus": [{"index": 0, "utilization": 90, "memory_used_mb": 1024}]
        }))
        store.open_incident(cluster="52", kind="delivery_failed")
        # 大盘接口直接返回这个结构，必须能被 JSON 序列化。
        json.dumps(build(store, settings), ensure_ascii=False)
