"""HTTP 接口：直连上报、飞书回调、大盘数据、SSO 端点、鉴权。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from hil2.api import create_app
from hil2.feishu.sso import issue_jwt
from hil2.models import Reservation

from .conftest import FakeFeishuClient, StaticReservationSource, make_reservation
from .test_feishu import ENCRYPT_KEY, encrypt_payload


@pytest.fixture
def app_client(settings, monkeypatch):
    """启动真实应用，但把飞书客户端和预约数据源换成测试替身。"""
    settings = settings.model_copy(update={"feishu_encrypt_key": ENCRYPT_KEY})
    app = create_app(settings)
    with TestClient(app) as test_client:
        state = app.state.hil2
        fake = FakeFeishuClient()
        source = StaticReservationSource([make_reservation("52")])
        state.client = fake
        state.coordinator.client = fake
        state.coordinator.reservation_source = source
        state.coordinator._reservations_synced_at = None  # noqa: SLF001
        # 后台线程在测试里会干扰断言，测试内手动触发。
        state.timeout_worker.stop()
        state.reservation_worker.stop()
        test_client.fake = fake
        test_client.source = source
        test_client.state = state
        yield test_client


def event_payload(cluster="52", state="busy", event_id="e1", **extra):
    payload = {
        "schema": "hil.monitor.v1",
        "event_id": event_id,
        "cluster": cluster,
        "state": state,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "reasons": ["测试"],
    }
    payload.update(extra)
    return payload


AUTH = {"Authorization": "Bearer ingest_test_token"}


class TestHealthAndPage:
    def test_health_reports_safety_invariants(self, app_client):
        body = app_client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["safety"] == {"read_only": True, "never_auto_release": True}
        assert body["managed_clusters"] == ["52", "58", "60"]

    def test_dashboard_page_renders(self, app_client):
        response = app_client.get("/")
        assert response.status_code == 200
        assert "HIL2.0 集群预约" in response.text
        assert "只读监控" in response.text

    def test_static_assets_served(self, app_client):
        for path in ("/static/styles.css", "/static/app.js", "/static/favicon.svg"):
            assert app_client.get(path).status_code == 200


class TestIngest:
    def test_accepts_valid_event(self, app_client):
        response = app_client.post("/api/ingest", json=event_payload(), headers=AUTH)
        assert response.status_code == 200
        assert response.json()["accepted"] is True

    def test_rejects_missing_token(self, app_client):
        assert app_client.post("/api/ingest", json=event_payload()).status_code == 401

    def test_rejects_wrong_token(self, app_client):
        response = app_client.post(
            "/api/ingest", json=event_payload(), headers={"Authorization": "Bearer nope"}
        )
        assert response.status_code == 401

    def test_duplicate_is_idempotent(self, app_client):
        payload = event_payload(event_id="dup-1")
        app_client.post("/api/ingest", json=payload, headers=AUTH)
        second = app_client.post("/api/ingest", json=payload, headers=AUTH).json()
        assert second["duplicate"] is True

    def test_unparsable_event_returns_400_not_silent_success(self, app_client):
        # 静默吞掉坏事件会让集群看起来「没消息就是好消息」，必须显式报错。
        for bad in ({"cluster": "52"}, {"cluster": "", "observed_at": "x"},
                    {"cluster": "52", "observed_at": "2026-01-01T00:00:00"}):
            response = app_client.post("/api/ingest", json=bad, headers=AUTH)
            assert response.status_code == 400

    def test_non_json_body_returns_400(self, app_client):
        response = app_client.post(
            "/api/ingest", content=b"not json",
            headers={**AUTH, "Content-Type": "application/json"},
        )
        assert response.status_code == 400

    def test_unknown_state_ingests_as_unknown_and_alerts(self, app_client):
        body = app_client.post(
            "/api/ingest", json=event_payload(state="totally_free"), headers=AUTH
        ).json()
        assert body["state"] == "unknown"
        assert body["actions"][0]["kind"] == "alert_admin"


class TestFeishuCallback:
    def _post(self, app_client, payload, *, encrypted=False):
        body = {"encrypt": encrypt_payload(json.dumps(payload))} if encrypted else payload
        return app_client.post("/api/feishu/callback", json=body)

    def test_url_verification_echoes_challenge(self, app_client):
        response = self._post(app_client, {
            "token": "vtoken", "type": "url_verification", "challenge": "chal-1"
        })
        assert response.json() == {"challenge": "chal-1"}

    def test_encrypted_url_verification(self, app_client):
        response = self._post(app_client, {
            "token": "vtoken", "type": "url_verification", "challenge": "chal-2"
        }, encrypted=True)
        assert response.json() == {"challenge": "chal-2"}

    def test_bad_verification_token_rejected(self, app_client):
        response = self._post(app_client, {"token": "wrong", "type": "url_verification"})
        assert response.status_code == 401

    def test_bad_signature_rejected(self, app_client):
        response = app_client.post(
            "/api/feishu/callback",
            json={"token": "vtoken", "type": "url_verification", "challenge": "c"},
            headers={
                "X-Lark-Request-Timestamp": "1",
                "X-Lark-Request-Nonce": "n",
                "X-Lark-Signature": "deadbeef",
            },
        )
        assert response.status_code == 401

    def _message(self, text, *, chat_id="oc_monitor", sender="ou_webhook", event_id="m1"):
        return {
            "token": "vtoken",
            "header": {"event_id": event_id, "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": sender}},
                "message": {
                    "chat_id": chat_id, "message_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
            },
        }

    def test_monitor_group_event_ingested(self, app_client):
        text = "监控事件\nEVENT_JSON=" + json.dumps(
            event_payload(state="idle", event_id="relay-1"), ensure_ascii=False
        )
        body = self._post(app_client, self._message(text)).json()
        assert body["accepted"] is True
        assert body["actions"][0]["kind"] == "notify_next_available"

    def test_loop_guard_ignores_own_messages(self, app_client):
        # 业务机器人绝不能消费自己发出的消息，否则会自激。
        text = "EVENT_JSON=" + json.dumps(event_payload(event_id="self-1"))
        body = self._post(app_client, self._message(text, sender="ou_bot")).json()
        assert body == {"ignored": "loop_guard"}

    def test_other_chats_ignored(self, app_client):
        text = "EVENT_JSON=" + json.dumps(event_payload(event_id="other-1"))
        body = self._post(app_client, self._message(text, chat_id="oc_random")).json()
        assert body == {"ignored": "not_monitor_chat"}

    def test_plain_chat_message_ignored(self, app_client):
        body = self._post(app_client, self._message("52 今天谁在用？")).json()
        assert body == {"ignored": "no_event_json"}

    def test_malformed_event_json_opens_incident(self, app_client):
        text = 'EVENT_JSON={"cluster":"52","observed_at":"昨天"}'
        body = self._post(app_client, self._message(text)).json()
        assert body["ignored"] == "unparsable"
        assert any(i["kind"] == "malformed_event" for i in app_client.state.store.open_incidents())

    def test_dual_channel_delivery_is_idempotent(self, app_client):
        # 交接文件第 7 节：同一份 JSON 会同时走直连和监控群，只能生效一次。
        payload = event_payload(state="idle", event_id="dual-99")
        app_client.post("/api/ingest", json=payload, headers=AUTH)
        text = "EVENT_JSON=" + json.dumps(payload, ensure_ascii=False)
        body = self._post(app_client, self._message(text)).json()
        assert body["duplicate"] is True
        assert len(app_client.fake.cards_to("oc_business")) == 1

    def test_unsupported_event_ignored(self, app_client):
        body = self._post(app_client, {
            "token": "vtoken",
            "header": {"event_type": "im.chat.updated_v1"},
            "event": {},
        }).json()
        assert body["ignored"] == "im.chat.updated_v1"


class TestCardCallback:
    def _make_pending_card(self, app_client):
        app_client.post(
            "/api/ingest",
            json=event_payload(state="suspected_idle", event_id="s-1",
                               suspected_idle_minutes=20),
            headers=AUTH,
        )
        return app_client.state.store.active_card_token("52")

    def _click(self, app_client, token, operation="continue_30", *,
               operator="ou_current", event_id="c1"):
        return app_client.post("/api/feishu/callback", json={
            "token": "vtoken",
            "header": {"event_id": event_id, "event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": operator},
                "action": {"value": {"operation": operation, "cluster": "52",
                                     "card_token": token}},
            },
        })

    def test_continue_accepted_with_toast(self, app_client):
        token = self._make_pending_card(app_client)
        body = self._click(app_client, token).json()
        assert body["accepted"] is True
        assert body["toast"]["type"] == "success"
        assert app_client.state.store.get_reservation("52").reserved_until is not None

    def test_release_notifies_next(self, app_client):
        token = self._make_pending_card(app_client)
        body = self._click(app_client, token, "released").json()
        assert body["accepted"] is True
        assert app_client.state.store.get_reservation("52").current_open_id is None

    def test_wrong_operator_rejected(self, app_client):
        token = self._make_pending_card(app_client)
        body = self._click(app_client, token, operator="ou_hacker").json()
        assert body["accepted"] is False
        assert body["toast"]["type"] == "error"
        assert app_client.state.store.active_card_token("52") == token

    def test_feishu_retry_replays_first_result(self, app_client):
        token = self._make_pending_card(app_client)
        first = self._click(app_client, token, event_id="retry-1").json()
        second = self._click(app_client, token, event_id="retry-1").json()
        # 同一个 event_id 重试必须回放首次结果，而不是报「已处理过」。
        assert second["replayed"] is True
        assert second["toast"] == first["toast"]

    def test_genuine_second_click_rejected(self, app_client):
        token = self._make_pending_card(app_client)
        self._click(app_client, token, event_id="click-1")
        body = self._click(app_client, token, event_id="click-2").json()
        assert body["accepted"] is False
        assert "已经处理过" in body["toast"]["content"]


class TestDashboardApi:
    def test_returns_all_managed_clusters_even_without_data(self, app_client):
        body = app_client.get("/api/dashboard").json()
        assert {item["cluster"] for item in body["clusters"]} == {"52", "58", "60"}
        assert body["counters"]["no_data"] == 3

    def test_reflects_ingested_state(self, app_client):
        app_client.post("/api/ingest", json=event_payload(state="idle"), headers=AUTH)
        body = app_client.get("/api/dashboard").json()
        item = next(c for c in body["clusters"] if c["cluster"] == "52")
        assert item["group"] == "idle"
        assert item["state_label"] == "明确空闲"

    def test_safety_note_present(self, app_client):
        body = app_client.get("/api/dashboard").json()
        assert body["safety"]["never_auto_release"] is True

    def test_cluster_events_endpoint(self, app_client):
        app_client.post("/api/ingest", json=event_payload(event_id="ev-1"), headers=AUTH)
        body = app_client.get("/api/clusters/52/events").json()
        assert body["events"][0]["event_id"] == "ev-1"

    def test_reservations_endpoint(self, app_client):
        app_client.post("/api/admin/reservations/sync")
        body = app_client.get("/api/admin/reservations").json()
        assert body["data"][0]["cluster"] == "52"


class TestSsoEndpoints:
    def test_dashboard_open_when_sso_disabled(self, app_client):
        assert app_client.get("/api/dashboard").status_code == 200

    def test_dashboard_stays_viewable_when_sso_enabled(self, settings):
        """大盘要挂在墙上给所有人看：开了 SSO 也不该拦住「看状态」。"""
        app = create_app(settings.model_copy(update={"sso_enabled": True}))
        with TestClient(app) as test_client:
            for worker in ("timeout_worker", "reservation_worker",
                           "booking_worker", "pull_worker"):
                getattr(test_client.app.state.hil2, worker).stop()
            assert test_client.get("/api/dashboard").status_code == 200

    def test_writing_still_requires_login_when_sso_enabled(self, settings):
        """能看不等于能改：预约必须登录，且不能靠伪造 open_id 绕过。"""
        app = create_app(settings.model_copy(
            update={"sso_enabled": True, "reservation_source": "booking"}))
        with TestClient(app) as test_client:
            for worker in ("timeout_worker", "reservation_worker",
                           "booking_worker", "pull_worker"):
                getattr(test_client.app.state.hil2, worker).stop()
            response = test_client.post("/api/bookings", json={
                "cluster": "52", "start_at": _slot(1), "end_at": _slot(3),
                "open_id": "forged", "name": "冒名顶替",
            })
            assert response.status_code == 401

    def test_view_can_be_locked_down_when_needed(self, settings):
        """服务暴露在不可信网络时，可以把「看」也管起来。"""
        app = create_app(settings.model_copy(
            update={"sso_enabled": True, "require_login_to_view": True}))
        with TestClient(app) as test_client:
            for worker in ("timeout_worker", "reservation_worker",
                           "booking_worker", "pull_worker"):
                getattr(test_client.app.state.hil2, worker).stop()
            assert test_client.get("/api/dashboard").status_code == 401
            token = issue_jwt({"sub": "ou_a"}, settings.jwt_secret, ttl_seconds=60)
            assert test_client.get(
                "/api/dashboard", headers={"Authorization": f"Bearer {token}"}
            ).status_code == 200

    def test_valid_jwt_accepted_when_sso_enabled(self, settings):
        app = create_app(settings.model_copy(update={"sso_enabled": True}))
        with TestClient(app) as test_client:
            test_client.app.state.hil2.timeout_worker.stop()
            test_client.app.state.hil2.reservation_worker.stop()
            token = issue_jwt({"sub": "ou_a"}, settings.jwt_secret, ttl_seconds=60)
            response = test_client.get(
                "/api/dashboard", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 200

    def test_login_without_code_rejected(self, app_client):
        assert app_client.post("/api/auth/feishu-login", json={}).status_code == 401

    def test_auth_params_requires_url(self, app_client):
        assert app_client.get("/api/auth/feishu-auth-params").status_code == 422

    def test_oauth_url_without_redirect_configured(self, app_client):
        assert app_client.get("/api/auth/feishu-oauth-url").status_code == 400

    def test_me_in_open_mode(self, app_client):
        assert app_client.get("/api/auth/me").json()["data"]["sub"] == "anonymous"


# ======================================================================
# 预约接口
# ======================================================================
@pytest.fixture
def booking_client(settings):
    """booking 模式下的应用，用于预约接口测试。"""
    from .conftest import FakeFeishuClient, StaticReservationSource

    app = create_app(settings.model_copy(update={"reservation_source": "booking"}))
    with TestClient(app) as test_client:
        state = app.state.hil2
        state.timeout_worker.stop()
        state.reservation_worker.stop()
        state.booking_worker.stop()
        fake = FakeFeishuClient()
        state.client = fake
        state.coordinator.client = fake
        state.coordinator.reservation_source = StaticReservationSource()
        test_client.state = state
        test_client.fake = fake
        yield test_client


def _slot(hours_from_now: float):
    from hil2.booking import align_down

    base = align_down(datetime.now(timezone.utc))
    return (base + timedelta(hours=hours_from_now)).isoformat()


def _book(client, cluster="52", h0=1, h1=3, who="local_a", name="张工"):
    return client.post("/api/bookings", json={
        "cluster": cluster, "start_at": _slot(h0), "end_at": _slot(h1),
        "open_id": who, "name": name,
    })


class TestBookingApi:
    def test_empty_board_lists_managed_clusters(self, booking_client):
        body = booking_client.get("/api/bookings").json()
        assert body["clusters"] == ["52", "58", "60"]
        assert body["slot_minutes"] == 30
        assert body["bookings"] == []

    def test_create_and_list(self, booking_client):
        response = _book(booking_client)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["cluster"] == "52" and data["name"] == "张工"

        listed = booking_client.get("/api/bookings").json()["bookings"]
        assert [item["id"] for item in listed] == [data["id"]]
        assert listed[0]["can_cancel"] is False  # 未声明查看者时不能显示取消入口

    def test_conflict_returns_409_with_readable_reason(self, booking_client):
        _book(booking_client)
        response = _book(booking_client, who="local_b", name="李工")
        assert response.status_code == 409
        assert "已被占用" in response.json()["detail"]
        assert "张工" in response.json()["detail"]

    def test_adjacent_booking_allowed(self, booking_client):
        _book(booking_client, h0=1, h1=3)
        assert _book(booking_client, h0=3, h1=5, who="local_b", name="李工").status_code == 200

    def test_unmanaged_cluster_rejected(self, booking_client):
        response = _book(booking_client, cluster="99")
        assert response.status_code == 409
        assert "纳管" in response.json()["detail"]

    def test_past_booking_rejected(self, booking_client):
        response = _book(booking_client, h0=-5, h1=-3)
        assert response.status_code == 409
        assert "过去" in response.json()["detail"]

    def test_too_long_rejected(self, booking_client):
        response = _book(booking_client, h0=1, h1=20)
        assert response.status_code == 409
        assert "最长" in response.json()["detail"]

    def test_missing_identity_rejected(self, booking_client):
        response = booking_client.post("/api/bookings", json={
            "cluster": "52", "start_at": _slot(1), "end_at": _slot(3),
        })
        assert response.status_code == 401

    def test_bad_time_format_rejected(self, booking_client):
        response = booking_client.post("/api/bookings", json={
            "cluster": "52", "start_at": "明天下午", "end_at": _slot(3), "open_id": "local_a",
        })
        assert response.status_code == 400

    def test_cancel_own_booking(self, booking_client):
        booking_id = _book(booking_client).json()["data"]["id"]
        response = booking_client.request(
            "DELETE", f"/api/bookings/{booking_id}", json={"open_id": "local_a"}
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "cancelled"
        assert booking_client.get("/api/bookings").json()["bookings"] == []

    def test_cannot_cancel_others(self, booking_client):
        booking_id = _book(booking_client).json()["data"]["id"]
        response = booking_client.request(
            "DELETE", f"/api/bookings/{booking_id}", json={"open_id": "local_intruder"}
        )
        assert response.status_code == 403
        assert booking_client.get("/api/bookings").json()["bookings"]

    def test_database_admin_can_cancel_others(self, booking_client):
        from hil2.registry import OrgUser

        booking_id = _book(booking_client).json()["data"]["id"]
        booking_client.state.store.upsert_org_users([
            OrgUser(open_id="local_manager", name="管理员")
        ])
        booking_client.state.store.set_admin("local_manager", True, actor="bootstrap")

        board = booking_client.get(
            "/api/bookings?open_id=local_manager"
        ).json()
        assert board["viewer"] == {
            "open_id": "local_manager", "is_admin": True, "role": "admin"
        }
        assert board["bookings"][0]["can_cancel"] is True

        response = booking_client.request(
            "DELETE", f"/api/bookings/{booking_id}",
            json={"open_id": "local_manager"},
        )
        assert response.status_code == 200

    def test_cancelled_slot_becomes_bookable(self, booking_client):
        booking_id = _book(booking_client).json()["data"]["id"]
        booking_client.request("DELETE", f"/api/bookings/{booking_id}",
                               json={"open_id": "local_a"})
        assert _book(booking_client, who="local_b", name="李工").status_code == 200

    def test_mine_filter(self, booking_client):
        _book(booking_client, h0=1, h1=3, who="local_a", name="张工")
        _book(booking_client, h0=3, h1=5, who="local_b", name="李工")
        mine = booking_client.get("/api/bookings?mine=true&open_id=local_a").json()["bookings"]
        assert [item["name"] for item in mine] == ["张工"]

    def test_dashboard_reflects_booking(self, booking_client):
        _book(booking_client, h0=0, h1=3, who="local_a", name="张工")
        _book(booking_client, h0=3, h1=5, who="local_b", name="李工")
        body = booking_client.get("/api/dashboard").json()
        item = next(c for c in body["clusters"] if c["cluster"] == "52")
        # 大盘卡片上的预约必须来自 bookings，而不是旧的 reservations 表
        assert item["reservation"]["current_name"] == "张工"
        assert item["reservation"]["next_name"] == "李工"

    def test_booking_drives_handoff_decision(self, booking_client):
        _book(booking_client, h0=0, h1=3, who="local_a", name="张工")
        _book(booking_client, h0=3, h1=5, who="local_b", name="李工")
        result = booking_client.post("/api/ingest", json=event_payload(state="idle"),
                                     headers=AUTH).json()
        # 明确空闲 + 有人排队 → 通知下一位，目标必须是预约里的那个人
        assert result["actions"][0]["kind"] == "notify_next_available"
        assert result["actions"][0]["target_open_id"] == "local_b"


class TestBookingCommandsOverFeishu:
    def _message(self, text, *, chat_id="oc_business", sender="ou_zhang", event_id="bm1"):
        return {
            "token": "vtoken",
            "header": {"event_id": event_id, "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": sender}},
                "message": {
                    "chat_id": chat_id, "message_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
            },
        }

    def test_book_from_business_chat(self, booking_client):
        body = booking_client.post(
            "/api/feishu/callback", json=self._message("@_user_1 预约 52 明天 14:00-18:00")
        ).json()
        assert body["handled"] == "booking_command"
        assert "预约成功" in body["title"]
        assert booking_client.state.store.list_bookings(cluster="52")

    def test_chitchat_ignored(self, booking_client):
        body = booking_client.post(
            "/api/feishu/callback", json=self._message("今天中午吃什么", event_id="bm2")
        ).json()
        assert body["ignored"] == "not_a_command"

    def test_duplicate_message_not_double_booked(self, booking_client):
        message = self._message("预约 52 明天 14:00-18:00", event_id="dup-bm")
        booking_client.post("/api/feishu/callback", json=message)
        second = booking_client.post("/api/feishu/callback", json=message).json()
        assert second["ignored"] == "duplicate_message"
        assert len(booking_client.state.store.list_bookings(cluster="52")) == 1

    def test_loop_guard_still_applies(self, booking_client):
        body = booking_client.post("/api/feishu/callback", json=self._message(
            "预约 52 明天 14:00-18:00", sender="ou_bot", event_id="bm3"
        )).json()
        assert body == {"ignored": "loop_guard"}
        assert booking_client.state.store.list_bookings(cluster="52") == []

    def test_monitor_chat_still_ingests_events(self, booking_client):
        text = "EVENT_JSON=" + json.dumps(event_payload(state="busy", event_id="mix-1"))
        body = booking_client.post("/api/feishu/callback", json=self._message(
            text, chat_id="oc_monitor", sender="ou_webhook", event_id="bm4"
        )).json()
        # 监控群走事件通路，业务群走指令通路，两者不能串
        assert body["accepted"] is True
        assert body["actions"][0]["kind"] == "alert_admin"
        assert any(
            item["kind"] == "unreserved_usage"
            for item in booking_client.state.store.open_incidents()
        )
