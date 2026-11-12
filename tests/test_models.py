"""事件解析：宽松兼容，但绝不把解析失败当成空闲。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hil2.models import (
    ClusterFacts,
    EventParseError,
    MonitorEvent,
    MonitorState,
    Reservation,
    parse_datetime,
)


class TestStateParsing:
    def test_known_states_round_trip(self):
        for state in MonitorState:
            assert MonitorState.parse(state.value) is state

    def test_unknown_state_string_degrades_to_unknown_not_idle(self):
        # 关键安全属性：认不出来的状态一律当采集异常，绝不猜成空闲。
        for raw in ("free", "available", "", None, "IDLE_MAYBE", 123):
            assert MonitorState.parse(raw) is MonitorState.UNKNOWN

    def test_case_and_whitespace_tolerated(self):
        assert MonitorState.parse("  BUSY ") is MonitorState.BUSY

    def test_labels_are_chinese(self):
        assert MonitorState.BUSY.label == "使用中"
        assert MonitorState.UNKNOWN.label == "采集异常"


class TestDatetimeParsing:
    def test_accepts_offset_and_z(self):
        assert parse_datetime("2026-09-01T16:00:00+08:00").hour == 8
        assert parse_datetime("2026-09-01T08:00:00Z").hour == 8

    def test_naive_datetime_rejected(self):
        # 无时区会让「过期」判断在跨时区部署时静默出错。
        with pytest.raises(EventParseError):
            parse_datetime("2026-09-01T16:00:00")

    def test_garbage_rejected(self):
        for raw in ("", None, "昨天", "2026-13-45T99:99:99+08:00"):
            with pytest.raises(EventParseError):
                parse_datetime(raw)


class TestMonitorEvent:
    def _payload(self, **overrides):
        payload = {
            "schema": "hil.monitor.v1",
            "event_id": "52-1-busy",
            "cluster": "52",
            "state": "busy",
            "observed_at": "2026-09-01T16:00:00+08:00",
            "reasons": ["检测到 pytest"],
        }
        payload.update(overrides)
        return payload

    def test_parses_full_event(self):
        event = MonitorEvent.from_dict(self._payload())
        assert event.cluster == "52"
        assert event.state is MonitorState.BUSY
        assert event.reasons == ("检测到 pytest",)
        assert event.observed_at.tzinfo is timezone.utc

    def test_missing_cluster_rejected(self):
        with pytest.raises(EventParseError):
            MonitorEvent.from_dict(self._payload(cluster=""))

    def test_wrong_schema_rejected(self):
        with pytest.raises(EventParseError):
            MonitorEvent.from_dict(self._payload(schema="hil.monitor.v2"))

    def test_missing_schema_tolerated(self):
        payload = self._payload()
        del payload["schema"]
        assert MonitorEvent.from_dict(payload).cluster == "52"

    def test_missing_event_id_gets_deterministic_fallback(self):
        payload = self._payload()
        del payload["event_id"]
        first = MonitorEvent.from_dict(payload)
        second = MonitorEvent.from_dict(dict(payload))
        # 同一份内容必须产生同一个 id，否则幂等去重会失效。
        assert first.event_id == second.event_id
        assert first.event_id.startswith("52-auto-")

    def test_unknown_state_in_payload_becomes_unknown(self):
        event = MonitorEvent.from_dict(self._payload(state="probably_free"))
        assert event.state is MonitorState.UNKNOWN

    def test_reasons_accepts_string_or_list(self):
        assert MonitorEvent.from_dict(self._payload(reasons="单条")).reasons == ("单条",)
        assert MonitorEvent.from_dict(self._payload(reasons=None)).reasons == ()

    def test_round_trip_through_dict(self):
        event = MonitorEvent.from_dict(self._payload())
        again = MonitorEvent.from_dict(event.as_dict())
        assert again.event_id == event.event_id
        assert again.observed_at == event.observed_at


class TestClusterFacts:
    def test_parses_containers_and_gpus(self):
        facts = ClusterFacts.from_dict(
            {
                "hostname": "hil-52",
                "primary_ip": "10.20.30.52",
                "ipv4_addresses": ["10.20.30.52", "172.18.0.1"],
                "containers": {"sim": [{"name": "hil-sim-1", "state": "running"}]},
                "gpus": [{"index": 0, "utilization": "95", "memory_used_mb": "20480"}],
                "pytest_active": True,
                "login_session_count": 2,
                "login_users": ["jwj", "zcx"],
            }
        )
        assert facts.hostname == "hil-52"
        assert facts.primary_ip == "10.20.30.52"
        assert facts.ipv4_addresses == ("10.20.30.52", "172.18.0.1")
        assert facts.containers["sim"][0].running is True
        assert facts.gpus[0].utilization == 95
        assert facts.pytest_active is True
        assert facts.login_session_count == 2
        assert facts.login_users == ("jwj", "zcx")

    def test_malformed_facts_degrade_quietly(self):
        # 事实字段只用于展示，坏数据不能让整条事件报废。
        facts = ClusterFacts.from_dict({"containers": "oops", "gpus": [1, 2, {"index": "x"}]})
        assert facts.containers == {}
        assert facts.gpus == ()

    def test_non_dict_returns_empty(self):
        assert ClusterFacts.from_dict(None).hostname == ""

    def test_tri_state_activity_preserved(self):
        # None 表示「检查失败」，不能被压成 False。
        assert ClusterFacts.from_dict({"pytest_active": None}).pytest_active is None
        assert ClusterFacts.from_dict({"pytest_active": False}).pytest_active is False


class TestReservation:
    def test_handoff_due_only_when_past(self):
        past = Reservation(cluster="52", handoff_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        future = Reservation(cluster="52", handoff_at=datetime(2099, 1, 1, tzinfo=timezone.utc))
        assert past.handoff_due is True
        assert future.handoff_due is False
        assert Reservation(cluster="52").handoff_due is False

    def test_bad_time_drops_constraint_without_raising(self):
        # 时间坏了就当没有该约束；绝不因此宣告集群可用。
        reservation = Reservation.from_dict({"cluster": "52", "handoff_at": "昨天下午"})
        assert reservation.handoff_at is None
        assert reservation.handoff_due is False

    def test_empty_open_ids_normalise_to_none(self):
        reservation = Reservation.from_dict({"cluster": "52", "current_open_id": ""})
        assert reservation.current_open_id is None
