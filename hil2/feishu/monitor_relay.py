"""飞书监控群中继。

GPU 服务器只需出站访问飞书自定义机器人 Webhook；位于客户内网的协调服务
通过开放 API 读取监控群最近消息并消费 ``EVENT_JSON``。这样不要求 GPU 网与
内网互通，也不依赖飞书不支持的“机器人消息触发另一个机器人事件”。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from ..models import EventParseError, MonitorEvent
from .callback import extract_monitor_payload
from .client import FeishuClient

logger = logging.getLogger(__name__)


def _text_fragments(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_text_fragments(item))
        return result
    if isinstance(value, dict):
        result = []
        for key, item in value.items():
            if key in {"text", "content", "post", "zh_cn", "en_us"}:
                result.extend(_text_fragments(item))
        return result
    return []


def message_text(item: dict[str, Any]) -> str:
    """把 text/post 两种消息正文还原成可搜索文本。"""
    raw = (item.get("body") or {}).get("content", "")
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return raw
    else:
        value = raw
    return "\n".join(fragment for fragment in _text_fragments(value) if fragment)


class FeishuMonitorRelay:
    def __init__(
        self,
        *,
        client: FeishuClient,
        chat_id: str,
        ingest: Callable[[MonitorEvent], object],
    ) -> None:
        self.client = client
        self.chat_id = chat_id
        self.ingest = ingest
        self.last_scanned = 0
        self.last_ingested = 0
        self.last_error = ""

    @property
    def configured(self) -> bool:
        return self.client.configured and bool(self.chat_id)

    @property
    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "last_scanned": self.last_scanned,
            "last_ingested": self.last_ingested,
            "last_error": self.last_error,
        }

    def run_once(self) -> int:
        if not self.configured:
            return 0
        ingested = 0
        try:
            items = self.client.list_chat_messages(chat_id=self.chat_id)
            self.last_scanned = len(items)
            # API 默认倒序；按时间正序消费，方便审计时间线阅读。
            for item in reversed(items):
                payload = extract_monitor_payload(message_text(item))
                if payload is None:
                    continue
                try:
                    event = MonitorEvent.from_dict(payload, source="feishu_monitor_poll")
                except EventParseError as exc:
                    logger.warning("飞书监控群轮询消息无法解析：%s", exc)
                    continue
                self.ingest(event)
                ingested += 1
            self.last_ingested = ingested
            self.last_error = ""
            return ingested
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
