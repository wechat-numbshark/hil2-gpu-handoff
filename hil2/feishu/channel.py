"""飞书长连接入站适配器。

使用独立的 ``lark-channel-sdk``，把 WebSocket 收到的消息与卡片事件归一化成
项目已有的 :class:`InboundEnvelope`。业务处理仍由 ``api.py`` 的同一条分发路径完成，
因此 HTTP 回调与长连接不会产生两套行为。

这里刻意不使用旧版 ``lark_oapi.ws.Client``：交接文件要求使用独立 Channel SDK，
且长连接模式不需要公网 IP、域名或回调地址。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from typing import Any, Awaitable, Callable, Union
from urllib.parse import urlsplit

from .callback import InboundEnvelope

logger = logging.getLogger(__name__)

EnvelopeHandler = Callable[[InboundEnvelope], Union[Any, Awaitable[Any]]]


def _isolate_sdk_ws_loop(ws_client_module: Any) -> Any:
    """避免 Channel SDK 1.4 的模块级 loop 接管 ASGI 主循环。

    SDK 的 ``lark_channel.ws.client`` 在导入时保存
    ``asyncio.get_event_loop()``，而 ``connect_until_ready()`` 又在执行器线程里
    对该 loop 调用 ``run_until_complete``。如果它恰好是 Uvicorn 已运行的
    loop，真实 WebSocket 连接会报 ``event loop is already running``，并且
    断开时会把 ASGI 主循环停掉。

    项目只建立一条飞书 Channel；仅当 SDK 捕获的正是当前主循环
    时，为它换一个专用 loop。SDK 原有的后台线程会运行该 loop。
    """
    sdk_loop = getattr(ws_client_module, "loop", None)
    try:
        host_loop = asyncio.get_running_loop()
    except RuntimeError:
        host_loop = None
    if sdk_loop is not None and sdk_loop is host_loop:
        sdk_loop = asyncio.new_event_loop()
        ws_client_module.loop = sdk_loop
        logger.info("Channel SDK WebSocket 已使用独立事件循环")
    return sdk_loop


def _field(value: Any, name: str, default: Any = "") -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _operator_open_id(operator: Any) -> str:
    """取操作人 open_id。

    飞书原始事件里是 ``operator.operator_id.open_id``，而 Channel SDK 有时会摊平成
    ``operator.open_id``。两种都要认——取不到就是空串，上层会把点击判成「不是当前
    占用人」并拒绝，报错原因会完全误导排查方向。
    """
    direct = _field(operator, "open_id", "")
    if direct:
        return str(direct)
    nested = _field(operator, "operator_id", None)
    if nested is not None:
        return str(_field(nested, "open_id", "") or "")
    return ""


def _event_id(raw: Any) -> str:
    header = _field(raw, "header", {}) or {}
    return str(_field(header, "event_id", "") or _field(raw, "event_id", "") or "")


def _stable_card_event_id(event: Any, value: dict[str, Any]) -> str:
    """SDK 未暴露 header 时仍给卡片点击一个稳定幂等键。"""
    explicit = _event_id(_field(event, "raw", {}) or {})
    if explicit:
        return explicit
    material = json.dumps(
        {
            "message_id": _field(event, "message_id", ""),
            "operator": _operator_open_id(_field(event, "operator", {})),
            "value": value,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "ws-card-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def message_envelope(message: Any) -> InboundEnvelope:
    """把 Channel SDK 的 ``InboundMessage`` 变成项目统一事件。"""
    raw = _field(message, "raw", {}) or {}
    message_id = str(_field(message, "message_id", "") or _field(message, "id", ""))
    # body_text 已去掉机器人自身的 @，最适合解析预约指令；监控事件的
    # EVENT_JSON 正文不受影响。老 SDK 没该字段时回退到 content_text。
    text = str(
        _field(message, "body_text", "")
        or _field(message, "content_text", "")
        or ""
    )
    sender = _field(message, "sender", {}) or {}
    conversation = _field(message, "conversation", {}) or {}
    return InboundEnvelope(
        kind="message",
        event_id=_event_id(raw) or message_id,
        event_type="im.message.receive_v1",
        sender_open_id=str(
            _field(message, "sender_id", "") or _field(sender, "open_id", "") or ""
        ),
        chat_id=str(
            _field(message, "chat_id", "")
            or _field(conversation, "chat_id", "")
            or _field(conversation, "id", "")
            or ""
        ),
        text=text,
        raw=raw if isinstance(raw, dict) else {},
    )


def card_envelope(event: Any) -> InboundEnvelope:
    """把 Channel SDK 的 ``CardActionEvent`` 变成项目统一事件。"""
    action = _field(event, "action", {}) or {}
    value = _field(action, "value", {})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    if not isinstance(value, dict):
        value = {}
    operator = _field(event, "operator", {}) or {}
    raw = _field(event, "raw", {}) or {}
    return InboundEnvelope(
        kind="card_action",
        event_id=_stable_card_event_id(event, value),
        event_type="card.action.trigger",
        card_action_value=value,
        operator_open_id=_operator_open_id(operator),
        raw=raw if isinstance(raw, dict) else {},
    )


class FeishuLongConnection:
    """拥有一个 Channel SDK 长连接，并把事件交给应用分发器。"""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        api_base_url: str,
        monitor_chat_id: str,
        business_chat_id: str,
        admin_open_ids: list[str],
        security_mode: str,
        connect_timeout_seconds: int,
        on_envelope: EnvelopeHandler,
        channel_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_base_url = api_base_url
        self.monitor_chat_id = monitor_chat_id
        self.business_chat_id = business_chat_id
        self.admin_open_ids = admin_open_ids
        self.security_mode = security_mode
        self.connect_timeout_seconds = connect_timeout_seconds
        self.on_envelope = on_envelope
        self._factory = channel_factory
        self.channel: Any | None = None
        self.ready = False
        self.last_error = ""

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @property
    def status(self) -> dict[str, Any]:
        connection = None
        if self.channel is not None:
            getter = getattr(self.channel, "get_connection_status", None)
            if not callable(getter):
                getter = getattr(self.channel, "connection_snapshot", None)
            if callable(getter):
                try:
                    snapshot = getter()
                    state = _field(snapshot, "state", snapshot)
                    connection = str(state) if state is not None else None
                except Exception:
                    connection = "unavailable"
        return {
            "mode": "long_connection",
            "configured": self.configured,
            "ready": self.ready,
            "connection": connection,
            "last_error": self.last_error,
        }

    def _build_channel(self) -> Any:
        if self._factory is not None:
            return self._factory(
                app_id=self.app_id,
                app_secret=self.app_secret,
                domain=self._domain(),
            )

        # lark-channel-sdk 1.4 在导入时会捕获当前事件循环。先把
        # WebSocket 模块级 loop 与 ASGI 主循环隔离，再构造 Channel。
        from lark_channel.ws import client as sdk_ws_client

        _isolate_sdk_ws_loop(sdk_ws_client)
        from lark_channel import (  # imported lazily for clearer startup errors
            BotLoopGuardConfig,
            FeishuChannel,
            PolicyConfig,
            SecurityConfig,
        )

        chat_ids = [item for item in (self.monitor_chat_id, self.business_chat_id) if item]
        return FeishuChannel(
            app_id=self.app_id,
            app_secret=self.app_secret,
            domain=self._domain(),
            policy=PolicyConfig(
                dm_policy="disabled",
                group_policy="allowlist" if chat_ids else "disabled",
                group_allowlist=chat_ids,
                require_mention=True,
                admins=self.admin_open_ids,
                bot_loop_guard=BotLoopGuardConfig(enabled=True, on_trip="reject"),
            ),
            security=SecurityConfig(mode=self.security_mode),
        )

    def _domain(self) -> str:
        parsed = urlsplit(self.api_base_url)
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else self.api_base_url

    async def _dispatch(self, envelope: InboundEnvelope) -> Any:
        result = self.on_envelope(envelope)
        return await result if inspect.isawaitable(result) else result

    async def _on_message(self, message: Any) -> None:
        await self._dispatch(message_envelope(message))

    async def _on_card_action(self, event: Any) -> None:
        await self._dispatch(card_envelope(event))

    async def _on_error(self, error: Any) -> None:
        self.last_error = type(error).__name__ + (f": {error}" if str(error) else "")
        logger.error("飞书长连接错误：%s", self.last_error)

    async def start(self) -> None:
        if not self.configured:
            self.last_error = "飞书应用凭据未配置"
            return
        self.channel = self._build_channel()
        self.channel.on("message", self._on_message)
        self.channel.on("cardAction", self._on_card_action)
        self.channel.on("error", self._on_error)
        try:
            await self.channel.connect_until_ready(timeout=self.connect_timeout_seconds)
            self.ready = True
            self.last_error = ""
            logger.info("飞书长连接已就绪（无需公网回调地址）")
        except Exception as exc:
            self.ready = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("飞书长连接启动失败；HTTP 直连采集仍可用")

    async def stop(self) -> None:
        if self.channel is None:
            return
        try:
            await self.channel.disconnect()
        finally:
            self.ready = False
