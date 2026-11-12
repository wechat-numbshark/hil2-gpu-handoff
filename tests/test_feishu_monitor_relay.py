from __future__ import annotations

import json
from datetime import datetime, timezone

from hil2.feishu.monitor_relay import FeishuMonitorRelay, message_text


def event(event_id: str = "relay-1") -> dict:
    return {
        "schema": "hil.monitor.v1",
        "event_id": event_id,
        "cluster": "52",
        "state": "unknown",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def post_item(payload: dict) -> dict:
    text = "监控事件\nEVENT_JSON=" + json.dumps(payload, ensure_ascii=False)
    content = {"post": {"zh_cn": {"content": [[{"tag": "at", "user_id": "ou_bot"}, {"tag": "text", "text": text}]]}}}
    return {"body": {"content": json.dumps(content, ensure_ascii=False)}}


def test_message_text_flattens_webhook_post():
    assert "EVENT_JSON=" in message_text(post_item(event()))


def test_relay_ingests_event_json_and_ignores_other_messages():
    class Client:
        configured = True

        def list_chat_messages(self, *, chat_id):
            assert chat_id == "oc_monitor"
            return [post_item(event()), {"body": {"content": '{"text":"hello"}'}}]

    received = []
    relay = FeishuMonitorRelay(client=Client(), chat_id="oc_monitor", ingest=received.append)
    assert relay.run_once() == 1
    assert received[0].event_id == "relay-1"
    assert received[0].source == "feishu_monitor_poll"
    assert relay.status["last_scanned"] == 2


def test_unconfigured_relay_is_noop():
    class Client:
        configured = False

    relay = FeishuMonitorRelay(client=Client(), chat_id="", ingest=lambda _: None)
    assert relay.run_once() == 0


def test_webhook_group_message_updates_system_and_opens_unreserved_incident(
    coordinator, client, store
):
    payload = event("relay-e2e")
    payload.update({
        "state": "busy",
        "reasons": ["检测到测试进程"],
        "facts": {
            "login_session_count": 1,
            "login_users": ["jwj"],
        },
    })
    client.list_chat_messages = lambda *, chat_id: [post_item(payload)]
    relay = FeishuMonitorRelay(
        client=client, chat_id="oc_monitor", ingest=coordinator.ingest
    )

    assert relay.run_once() == 1
    snapshot = store.get_snapshot("52")
    assert snapshot["source"] == "feishu_monitor_poll"
    assert snapshot["facts"].login_users == ("jwj",)
    assert any(item["kind"] == "unreserved_usage" for item in store.open_incidents())
