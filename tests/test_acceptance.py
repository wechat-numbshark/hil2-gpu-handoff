"""验收清单：逐条对应交接文件第 11 节的 12 项。

每个测试类名带编号，方便跟客户逐条对照勾选。
全部走真实 FastAPI 应用 + 真实 SQLite + 真实决策引擎，
只把飞书投递和预约数据源换成可断言的替身。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from hil2.api import create_app
from hil2.models import Reservation

from .conftest import FakeFeishuClient, StaticReservationSource
from .test_feishu import ENCRYPT_KEY, encrypt_payload

AUTH = {"Authorization": "Bearer ingest_test_token"}
BUSINESS = "oc_business"
MONITOR = "oc_monitor"


@pytest.fixture
def rig(settings):
    """一套完整的运行环境，附带便捷操作方法。"""
    settings = settings.model_copy(update={"feishu_encrypt_key": ENCRYPT_KEY})
    app = create_app(settings)
    with TestClient(app) as client:
        state = app.state.hil2
        state.timeout_worker.stop()
        state.reservation_worker.stop()
        fake = FakeFeishuClient()
        source = StaticReservationSource()
        state.client = fake
        state.coordinator.client = fake
        state.coordinator.reservation_source = source
        yield Rig(client, state, fake, source)


class Rig:
    def __init__(self, client, state, fake, source):
        self.client = client
        self.state = state
        self.fake = fake
        self.source = source
        self._seq = 0

    def queue(self, cluster="52", current="ou_zhang", nxt="ou_li",
              handoff_minutes=None, current_name="张工", next_name="李工"):
        self.source.reservations = [Reservation(
            cluster=cluster,
            current_open_id=current, current_name=current_name if current else "",
            next_open_id=nxt, next_name=next_name if nxt else "",
            handoff_at=(datetime.now(timezone.utc) + timedelta(minutes=handoff_minutes))
            if handoff_minutes is not None else None,
        )]
        self.state.coordinator._reservations_synced_at = None  # noqa: SLF001
        self.state.coordinator.sync_reservations(force=True)

    def report(self, cluster="52", state="busy", *, minutes=0, reasons=("测试",), facts=None):
        self._seq += 1
        payload = {
            "schema": "hil.monitor.v1",
            "event_id": f"{cluster}-acc-{self._seq}",
            "cluster": cluster,
            "state": state,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "reasons": list(reasons),
            "suspected_idle_minutes": minutes,
            "facts": facts or {},
        }
        response = self.client.post("/api/ingest", json=payload, headers=AUTH)
        assert response.status_code == 200, response.text
        return response.json()

    def click(self, operation, *, cluster="52", token=None, operator="ou_zhang", event_id=None):
        self._seq += 1
        token = token if token is not None else self.state.store.active_card_token(cluster)
        return self.client.post("/api/feishu/callback", json={
            "token": "vtoken",
            "header": {"event_id": event_id or f"cb-{self._seq}",
                       "event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": operator},
                "action": {"value": {"operation": operation, "cluster": cluster,
                                     "card_token": token}},
            },
        }).json()

    def age_card(self, minutes, cluster="52"):
        token = self.state.store.active_card_token(cluster)
        created = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        with self.state.store._tx() as conn:  # noqa: SLF001
            conn.execute("UPDATE cards SET created_at = ? WHERE card_token = ?",
                         (created.isoformat(), token))
        return token

    # 断言辅助
    def business_titles(self):
        return [c["card"]["header"]["title"]["content"] for c in self.fake.cards_to(BUSINESS)]

    def monitor_titles(self):
        return [c["card"]["header"]["title"]["content"] for c in self.fake.cards_to(MONITOR)]

    def business_bodies(self):
        return [json.dumps(c["card"], ensure_ascii=False) for c in self.fake.cards_to(BUSINESS)]


# ======================================================================
class Test01_PytestRunningIsBusyAndQuiet:
    """[1] pytest 运行 → BUSY，未到交接点不打扰大群。"""

    def test(self, rig: Rig):
        rig.queue(handoff_minutes=60)
        result = rig.report(state="busy", reasons=("检测到 pytest 测试进程",))
        assert result["state"] == "busy"
        assert result["actions"][0]["kind"] == "audit_only"
        assert rig.business_titles() == []


class Test02_RecentDataWriteIsBusy:
    """[2] 无 pytest 但 /data1 近期写入 → BUSY。"""

    def test(self, rig: Rig):
        rig.queue(handoff_minutes=60)
        result = rig.report(
            state="busy",
            reasons=("/data1 最近 15 分钟有文件写入",),
            facts={"pytest_active": False, "recent_data": True},
        )
        assert result["state"] == "busy"
        assert rig.business_titles() == []
        item = next(c for c in rig.client.get("/api/dashboard").json()["clusters"]
                    if c["cluster"] == "52")
        assert item["pytest_active"] is False and item["recent_data"] is True


class Test03_HighGpuNoActivityAsksCurrentUser:
    """[3] GPU 高、连续 15 分钟无 pytest/写入 → SUSPECTED_IDLE，给当前占用人发卡片。"""

    def test(self, rig: Rig):
        rig.queue()
        gpus = [{"index": i, "utilization": 98, "memory_used_mb": 24000} for i in range(8)]
        result = rig.report(
            state="suspected_idle", minutes=15,
            facts={"pytest_active": False, "recent_data": False, "gpus": gpus},
        )
        assert result["actions"][0]["kind"] == "ask_current_user"
        assert result["actions"][0]["target_open_id"] == "ou_zhang"
        assert any("占用确认" in title for title in rig.business_titles())
        # GPU 满载也绝不能因此通知下一位。
        assert not any("可以使用" in title for title in rig.business_titles())

    def test_below_threshold_stays_quiet(self, rig: Rig):
        rig.queue()
        rig.report(state="suspected_idle", minutes=10)
        assert rig.business_titles() == []


class Test04_AllContainersStoppedNotifiesNext:
    """[4] 核心容器全部停止、无保留 → IDLE，直接 @下一位。"""

    def test(self, rig: Rig):
        rig.queue()
        result = rig.report(state="idle", reasons=("sim / proxy / tester 核心容器均已停止",))
        assert result["actions"][0]["kind"] == "notify_next_available"
        assert result["actions"][0]["target_open_id"] == "ou_li"
        assert any("可以使用" in title for title in rig.business_titles())
        # 不打扰上一位：没有给当前占用人发任何卡片。
        assert not any("占用确认" in title for title in rig.business_titles())

    def test_no_next_in_queue_stays_quiet(self, rig: Rig):
        rig.queue(nxt=None)
        result = rig.report(state="idle")
        assert result["actions"][0]["kind"] == "audit_only"
        assert rig.business_titles() == []


class Test05_ContinueExtendsAndOldCardDies:
    """[5] 用户选择继续 30/60 分钟 → 预约延期，旧卡片不能重复操作。"""

    @pytest.mark.parametrize("operation,minutes", [("continue_30", 30), ("continue_60", 60)])
    def test_extends(self, rig: Rig, operation, minutes):
        rig.queue()
        rig.report(state="suspected_idle", minutes=20)
        token = rig.state.store.active_card_token("52")

        body = rig.click(operation, token=token)
        assert body["accepted"] is True
        reserved = rig.state.store.get_reservation("52").reserved_until
        assert reserved > datetime.now(timezone.utc) + timedelta(minutes=minutes - 2)

        # 旧卡片立刻失效
        assert rig.state.store.active_card_token("52") is None
        again = rig.click(operation, token=token, event_id="second-click")
        assert again["accepted"] is False

    def test_hold_suppresses_further_prompts(self, rig: Rig):
        rig.queue()
        rig.report(state="suspected_idle", minutes=20)
        rig.click("continue_30")
        before = len(rig.fake.cards_to(BUSINESS))
        rig.report(state="suspected_idle", minutes=40)
        # 保留期内不重复催问。
        assert len(rig.fake.cards_to(BUSINESS)) == before


class Test06_ReleaseNotifiesNextWithoutTouchingMachine:
    """[6] 用户选择已使用完 → 预约释放并通知下一位，但不操作机器。"""

    def test(self, rig: Rig):
        rig.queue()
        rig.report(state="suspected_idle", minutes=20)
        body = rig.click("released")
        assert body["accepted"] is True

        reservation = rig.state.store.get_reservation("52")
        assert reservation.current_open_id is None
        assert any("可以使用" in title for title in rig.business_titles())

        # 审计里必须留下「只标记预约释放，未操作机器」的证据。
        notes = [n["note"] for n in rig.state.store.recent_notifications()]
        assert any("未操作机器" in note for note in notes)

    def test_no_machine_command_is_ever_issued(self, rig: Rig):
        # 协调服务只有发消息的能力，替身客户端记录了它做过的一切。
        rig.queue()
        rig.report(state="suspected_idle", minutes=20)
        rig.click("released")
        assert set(vars(rig.fake)) >= {"sent_cards", "sent_texts", "updated"}
        assert rig.fake.sent_texts == []


class Test07_TimeoutRemindsThenEscalatesNeverReleases:
    """[7] 10/20 分钟未回复 → 提醒/升级管理员，不自动释放。"""

    def test_reminder_at_10(self, rig: Rig):
        rig.queue()
        rig.report(state="suspected_idle", minutes=20)
        token = rig.age_card(11)
        rig.state.coordinator.scan_timeouts()
        assert any("提醒" in title for title in rig.business_titles())
        assert rig.state.store.get_card(token)["reminded_at"] is not None

    def test_escalation_at_20(self, rig: Rig):
        rig.queue()
        rig.report(state="suspected_idle", minutes=20)
        token = rig.age_card(21)
        rig.state.coordinator.scan_timeouts()
        assert any("人工处理" in title for title in rig.monitor_titles())
        assert rig.state.store.get_card(token)["escalated_at"] is not None

    def test_never_releases_after_a_day(self, rig: Rig):
        rig.queue()
        rig.report(state="suspected_idle", minutes=20)
        rig.age_card(60 * 24)
        for _ in range(3):
            rig.state.coordinator.scan_timeouts()
        reservation = rig.state.store.get_reservation("52")
        assert reservation.current_open_id == "ou_zhang"
        assert rig.state.store.active_card_token("52") is not None
        assert not any("可以使用" in title for title in rig.business_titles())


class Test08_CollectionFailureNeverDeclaresAvailable:
    """[8] Docker 权限不足、/data1 不可读 → UNKNOWN，不宣告可用。"""

    @pytest.mark.parametrize("reason", [
        "无法读取 Docker 容器：permission denied",
        "数据目录不存在或不可读：/data1",
        "hil-tester-1 内 pytest 检查失败：命令超时",
    ])
    def test(self, rig: Rig, reason):
        rig.queue()
        result = rig.report(state="unknown", reasons=(reason,))
        assert result["actions"][0]["kind"] == "alert_admin"
        assert rig.business_titles() == []
        assert any("人工处理" in title for title in rig.monitor_titles())
        incidents = rig.state.store.open_incidents()
        assert [i["kind"] for i in incidents] == ["collection_failed"]

    def test_dashboard_never_shows_idle_for_unknown(self, rig: Rig):
        rig.queue()
        rig.report(state="unknown", reasons=("docker 不可读",))
        item = next(c for c in rig.client.get("/api/dashboard").json()["clusters"]
                    if c["cluster"] == "52")
        assert item["group"] == "unknown"
        assert item["state_label"] == "采集异常"


class Test09_MonitorHeartbeatDoesNotSpamBusinessChat:
    """[9] 监控群持续有心跳 → 业务大群不刷屏。"""

    def test(self, rig: Rig):
        rig.queue(handoff_minutes=120)
        for _ in range(12):
            rig.report(state="busy")
        assert rig.business_titles() == []
        # 每一轮都留下了审计记录，只是没打扰任何人。
        audits = [n for n in rig.state.store.recent_notifications() if n["kind"] == "audit_only"]
        assert len(audits) >= 12


class Test10_StaleOrUnauthorizedClickRejected:
    """[10] 非当前占用人或过期卡片点击 → 拒绝。"""

    def test_wrong_person(self, rig: Rig):
        rig.queue()
        rig.report(state="suspected_idle", minutes=20)
        body = rig.click("released", operator="ou_intruder")
        assert body["accepted"] is False
        assert rig.state.store.get_reservation("52").current_open_id == "ou_zhang"

    def test_stale_token(self, rig: Rig):
        rig.queue()
        rig.report(state="suspected_idle", minutes=20)
        body = rig.click("released", token="tok-from-yesterday")
        assert body["accepted"] is False

    def test_superseded_card_rejected(self, rig: Rig):
        rig.queue()
        rig.report(state="suspected_idle", minutes=20)
        old = rig.state.store.active_card_token("52")
        # 用户续期后又超时，产生了新卡片；旧卡片必须失效。
        rig.click("continue_30", token=old)
        rig.state.store.extend_reservation("52", -1)
        rig.report(state="suspected_idle", minutes=40)
        assert rig.click("released", token=old, event_id="old-card")["accepted"] is False

    def test_unknown_operation_rejected(self, rig: Rig):
        rig.queue()
        rig.report(state="suspected_idle", minutes=20)
        assert rig.click("force_release")["accepted"] is False


class Test11_BotDoesNotConsumeItsOwnMessages:
    """[11] 机器人不会循环消费自己发出的消息。"""

    def test(self, rig: Rig):
        payload = {
            "schema": "hil.monitor.v1", "event_id": "self-loop-1", "cluster": "52",
            "state": "idle", "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        body = rig.client.post("/api/feishu/callback", json={
            "token": "vtoken",
            "header": {"event_id": "m1", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_bot"}},
                "message": {
                    "chat_id": MONITOR, "message_type": "text",
                    "content": json.dumps({"text": "EVENT_JSON=" + json.dumps(payload)}),
                },
            },
        }).json()
        assert body == {"ignored": "loop_guard"}
        assert rig.state.store.seen_event("self-loop-1") is False


class Test12_DirectChannelWorksWhenBotToBotFails:
    """[12] Bot-to-Bot 消息无法投递时，直接 HTTP 事件通道仍可工作。"""

    def test_direct_only(self, rig: Rig):
        rig.queue()
        # 监控群一条消息都没来，只有直连上报。
        result = rig.report(state="idle")
        assert result["actions"][0]["kind"] == "notify_next_available"
        assert any("可以使用" in title for title in rig.business_titles())

    def test_both_channels_deduplicated(self, rig: Rig):
        rig.queue()
        payload = {
            "schema": "hil.monitor.v1", "event_id": "dual-acc-1", "cluster": "52",
            "state": "idle", "observed_at": datetime.now(timezone.utc).isoformat(),
            "reasons": ["核心容器均已停止"],
        }
        rig.client.post("/api/ingest", json=payload, headers=AUTH)
        relayed = rig.client.post("/api/feishu/callback", json={
            "token": "vtoken",
            "header": {"event_id": "m2", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_webhook"}},
                "message": {
                    "chat_id": MONITOR, "message_type": "text",
                    "content": json.dumps({"text": "EVENT_JSON=" + json.dumps(payload)}),
                },
            },
        }).json()
        assert relayed["duplicate"] is True
        # 双通道投递同一份事件，下一位只会被通知一次。
        assert len([t for t in rig.business_titles() if "可以使用" in t]) == 1


# ======================================================================
class TestSafetyBoundaries:
    """三条不可逾越的边界，单独兜底。"""

    def test_no_path_ever_produces_a_machine_command(self, rig: Rig):
        """协调服务对集群完全没有写能力。"""
        rig.queue()
        for state, minutes in [("busy", 0), ("idle", 0), ("suspected_idle", 30),
                               ("unknown", 0), ("reserved", 0)]:
            rig.report(state=state, minutes=minutes)
        # 唯一的对外能力就是发消息 / 改卡片。
        assert rig.fake.sent_texts == []
        assert all("header" in c["card"] for c in rig.fake.sent_cards)

    def test_unknown_never_reaches_business_chat_as_available(self, rig: Rig):
        rig.queue()
        for i in range(5):
            rig.report(state="unknown", reasons=(f"第 {i} 次采集失败",))
        assert not any("可以使用" in title for title in rig.business_titles())

    def test_health_endpoint_advertises_invariants(self, rig: Rig):
        safety = rig.client.get("/api/health").json()["safety"]
        assert safety == {"read_only": True, "never_auto_release": True}
