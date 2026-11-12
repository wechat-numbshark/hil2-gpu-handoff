"""飞书长连接：SDK 归一化、启动配置和统一业务分发。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hil2.feishu.channel import (
    FeishuLongConnection,
    _isolate_sdk_ws_loop,
    card_envelope,
    message_envelope,
)


class FakeChannel:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.handlers = {}
        self.connected = False
        self.disconnected = False

    def on(self, name, handler):
        self.handlers[name] = handler

    async def connect_until_ready(self, timeout):
        self.timeout = timeout
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    def get_connection_status(self):
        return "connected" if self.connected else "closed"


@pytest.mark.asyncio
async def test_sdk_ws_loop_isolated_from_running_asgi_loop():
    import asyncio

    host_loop = asyncio.get_running_loop()
    module = SimpleNamespace(loop=host_loop)
    isolated = _isolate_sdk_ws_loop(module)
    try:
        assert isolated is module.loop
        assert isolated is not host_loop
        assert isolated.is_running() is False
    finally:
        isolated.close()


def test_message_envelope_prefers_body_text_and_preserves_identity():
    event = SimpleNamespace(
        message_id="om_1",
        chat_id="oc_business",
        sender_id="ou_user",
        body_text="预约 52 明天 14:00-16:00",
        content_text="@机器人 预约 52 明天 14:00-16:00",
        raw={"header": {"event_id": "evt_1"}},
    )
    envelope = message_envelope(event)
    assert envelope.event_id == "evt_1"
    assert envelope.sender_open_id == "ou_user"
    assert envelope.text.startswith("预约 52")


def test_message_envelope_matches_real_sdk_nested_identity_and_conversation():
    """lark-channel-sdk 1.4 把群和发送人放在嵌套数据类中。"""
    event = SimpleNamespace(
        id="om_real_shape",
        conversation=SimpleNamespace(chat_id="oc_business"),
        sender=SimpleNamespace(open_id="ou_user"),
        body_text="查看 52",
        raw={"header": {"event_id": "evt_real_shape"}},
    )
    envelope = message_envelope(event)
    assert envelope.event_id == "evt_real_shape"
    assert envelope.chat_id == "oc_business"
    assert envelope.sender_open_id == "ou_user"


def test_card_envelope_has_stable_fallback_event_id():
    event = SimpleNamespace(
        message_id="om_card",
        operator=SimpleNamespace(open_id="ou_current"),
        action=SimpleNamespace(value={"operation": "continue_30", "cluster": "52"}),
        raw={},
    )
    first = card_envelope(event)
    second = card_envelope(event)
    assert first.event_id == second.event_id
    assert first.event_id.startswith("ws-card-")
    assert first.operator_open_id == "ou_current"


@pytest.mark.asyncio
async def test_long_connection_registers_both_handlers_and_dispatches():
    received = []
    fake = FakeChannel()

    async def on_envelope(envelope):
        received.append(envelope)

    service = FeishuLongConnection(
        app_id="cli_test",
        app_secret="secret",
        api_base_url="https://open.feishu.cn/open-apis",
        monitor_chat_id="oc_monitor",
        business_chat_id="oc_business",
        admin_open_ids=["ou_admin"],
        security_mode="strict",
        connect_timeout_seconds=7,
        on_envelope=on_envelope,
        channel_factory=lambda **kwargs: fake,
    )
    await service.start()
    assert service.ready is True
    assert set(fake.handlers) == {"message", "cardAction", "error"}
    assert fake.timeout == 7

    await fake.handlers["message"](SimpleNamespace(
        message_id="om_2", chat_id="oc_monitor", sender_id="ou_hook",
        body_text="EVENT_JSON={}", raw={},
    ))
    assert received[0].kind == "message"

    await service.stop()
    assert fake.disconnected is True
    assert service.ready is False


@pytest.mark.asyncio
async def test_unconfigured_long_connection_stays_fail_closed():
    service = FeishuLongConnection(
        app_id="", app_secret="", api_base_url="https://open.feishu.cn/open-apis",
        monitor_chat_id="", business_chat_id="", admin_open_ids=[],
        security_mode="strict", connect_timeout_seconds=1,
        on_envelope=lambda envelope: None,
    )
    await service.start()
    assert service.ready is False
    assert "未配置" in service.last_error


def test_card_envelope_accepts_both_operator_shapes():
    """飞书原始事件用 operator.operator_id.open_id，SDK 有时摊平成 operator.open_id。

    只认一种的话，另一种会得到空 open_id，点击被判成「不是当前占用人」——
    用户看到的报错和真实原因完全对不上。
    """
    from hil2.feishu.channel import card_envelope

    flat = card_envelope(SimpleNamespace(
        action=SimpleNamespace(value={"operation": "released", "cluster": "52",
                                      "card_token": "t1"}),
        operator=SimpleNamespace(open_id="ou_flat"),
        raw={"header": {"event_id": "evt_flat"}},
    ))
    assert flat.operator_open_id == "ou_flat"
    assert flat.card_action_value["operation"] == "released"

    nested = card_envelope(SimpleNamespace(
        action=SimpleNamespace(value={"operation": "continue_30", "cluster": "52",
                                      "card_token": "t2"}),
        operator=SimpleNamespace(operator_id=SimpleNamespace(open_id="ou_nested")),
        raw={"header": {"event_id": "evt_nested"}},
    ))
    assert nested.operator_open_id == "ou_nested"

    # 两种都取不到时必须是空串，让上层明确拒绝，而不是抛异常
    missing = card_envelope(SimpleNamespace(
        action=SimpleNamespace(value={}), operator=SimpleNamespace(), raw={}))
    assert missing.operator_open_id == ""


def test_card_envelope_handles_dict_shaped_event():
    """有的 SDK 版本直接给 dict，不是数据类。"""
    from hil2.feishu.channel import card_envelope

    env = card_envelope({
        "action": {"value": '{"operation":"released","cluster":"58","card_token":"t3"}'},
        "operator": {"operator_id": {"open_id": "ou_dict"}},
        "raw": {"header": {"event_id": "evt_dict"}},
    })
    assert env.operator_open_id == "ou_dict"
    assert env.card_action_value["cluster"] == "58"   # 字符串 value 也要能解开
