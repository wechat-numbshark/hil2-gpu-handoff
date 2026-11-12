"""飞书事件与卡片回调的解密、验签和解析。

覆盖三类入站：
1. ``url_verification`` 挑战（明文或加密）。
2. ``im.message.receive_v1``：监控群里 Webhook 机器人发来的 ``hil.monitor.v1`` 事件。
3. ``card.action.trigger``：占用确认卡片按钮点击。

安全要求（交接文件第 5 节）：
- 校验 verification token / 签名。
- ``event_id`` 幂等去重由上层 Store 负责，这里只负责取出 event_id。
- loop guard：绝不消费自己发出的消息。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

# 集群脚本把结构化事件挂在消息末尾，形如 EVENT_JSON={"schema":"hil.monitor.v1",...}
EVENT_JSON_PREFIX = "EVENT_JSON="


class CallbackError(ValueError):
    """回调不可信或无法解析。上层应返回 4xx 且不产生任何副作用。"""


def decrypt(encrypt_key: str, encrypted: str) -> str:
    """AES-256-CBC 解密飞书事件。key=SHA256(encrypt_key)，IV 为密文前 16 字节。"""
    if not encrypt_key:
        raise CallbackError("未配置 FEISHU_ENCRYPT_KEY，无法解密事件")
    try:
        raw = base64.b64decode(encrypted)
    except Exception as exc:
        raise CallbackError("encrypt 字段不是合法 base64") from exc
    if len(raw) <= 16:
        raise CallbackError("encrypt 字段长度异常")

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv, ciphertext = raw[:16], raw[16:]
    if len(ciphertext) % 16 != 0:
        raise CallbackError("密文长度不是 16 的倍数")

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad = padded[-1]
    if not 1 <= pad <= 16 or padded[-pad:] != bytes([pad]) * pad:
        raise CallbackError("解密结果填充不合法")
    try:
        return padded[:-pad].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CallbackError("解密结果不是 UTF-8") from exc


def compute_signature(*, timestamp: str, nonce: str, encrypt_key: str, body: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(timestamp.encode("utf-8"))
    digest.update(nonce.encode("utf-8"))
    digest.update(encrypt_key.encode("utf-8"))
    digest.update(body)
    return digest.hexdigest()


def verify_signature(
    *, timestamp: str, nonce: str, encrypt_key: str, body: bytes, signature: str
) -> bool:
    if not (timestamp and nonce and encrypt_key and signature):
        return False
    import hmac

    expected = compute_signature(
        timestamp=timestamp, nonce=nonce, encrypt_key=encrypt_key, body=body
    )
    return hmac.compare_digest(expected, signature)


@dataclass(frozen=True)
class InboundEnvelope:
    """归一化后的入站回调。"""

    kind: str  # url_verification / message / card_action / other
    event_id: str = ""
    event_type: str = ""
    challenge: str = ""
    sender_open_id: str = ""
    chat_id: str = ""
    text: str = ""
    card_action_value: dict[str, Any] = field(default_factory=dict)
    operator_open_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def unwrap(
    payload: dict[str, Any],
    *,
    encrypt_key: str = "",
    verification_token: str = "",
) -> dict[str, Any]:
    """解密（如有）并校验 verification token，返回明文 payload。"""
    if not isinstance(payload, dict):
        raise CallbackError("回调必须是 JSON 对象")

    if "encrypt" in payload:
        plaintext = decrypt(encrypt_key, str(payload["encrypt"]))
        try:
            payload = json.loads(plaintext)
        except json.JSONDecodeError as exc:
            raise CallbackError("解密后的内容不是合法 JSON") from exc

    if verification_token:
        token = payload.get("token") or (payload.get("header") or {}).get("token", "")
        if token != verification_token:
            raise CallbackError("verification token 不匹配")
    return payload


def _message_text(content_json: str, message_type: str) -> str:
    """把 text / post 消息内容拍平成纯文本。"""
    try:
        content = json.loads(content_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    if message_type == "text":
        return str(content.get("text", ""))
    if message_type == "post":
        chunks: list[str] = []
        # post 内容形如 {"title": "...", "content": [[{tag, text}, ...], ...]}
        # 有的租户会再包一层语言键 {"zh_cn": {...}}。
        body = content
        if "content" not in body:
            for value in content.values():
                if isinstance(value, dict) and "content" in value:
                    body = value
                    break
        for line in body.get("content") or []:
            for node in line or []:
                if isinstance(node, dict) and node.get("tag") == "text":
                    chunks.append(str(node.get("text", "")))
            chunks.append("\n")
        return "".join(chunks)
    return ""


def parse(payload: dict[str, Any]) -> InboundEnvelope:
    """把明文 payload 归一化成 InboundEnvelope。"""
    if payload.get("type") == "url_verification" or "challenge" in payload:
        return InboundEnvelope(
            kind="url_verification", challenge=str(payload.get("challenge", "")), raw=payload
        )

    header = payload.get("header") or {}
    event = payload.get("event") or {}
    event_id = str(header.get("event_id") or payload.get("uuid") or "")
    event_type = str(header.get("event_type") or payload.get("type") or "")

    # 卡片按钮点击。v2 走 header.event_type，旧版直接是顶层 action。
    if event_type == "card.action.trigger" or "action" in event:
        action = event.get("action") or {}
        operator = event.get("operator") or {}
        value = action.get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}
        return InboundEnvelope(
            kind="card_action",
            event_id=event_id,
            event_type=event_type or "card.action.trigger",
            card_action_value=value if isinstance(value, dict) else {},
            operator_open_id=str(
                operator.get("open_id") or operator.get("operator_id", {}).get("open_id", "")
            ),
            raw=payload,
        )

    if event_type.startswith("im.message.receive"):
        message = event.get("message") or {}
        sender = (event.get("sender") or {}).get("sender_id") or {}
        return InboundEnvelope(
            kind="message",
            event_id=event_id or str(message.get("message_id", "")),
            event_type=event_type,
            sender_open_id=str(sender.get("open_id", "")),
            chat_id=str(message.get("chat_id", "")),
            text=_message_text(message.get("content", ""), str(message.get("message_type", ""))),
            raw=payload,
        )

    return InboundEnvelope(kind="other", event_id=event_id, event_type=event_type, raw=payload)


def extract_monitor_payload(text: str) -> dict[str, Any] | None:
    """从监控群消息中取出 ``EVENT_JSON=`` 后面的结构化事件。

    没有该标记返回 None（普通聊天消息，直接忽略，不产生任何副作用）。
    """
    if EVENT_JSON_PREFIX not in text:
        return None
    tail = text.split(EVENT_JSON_PREFIX, 1)[1]
    # 事件是单行紧凑 JSON；取到行尾即可，容忍后面还有别的内容。
    line = tail.splitlines()[0] if tail.splitlines() else ""
    line = re.sub(r"\s+$", "", line)
    decoder = json.JSONDecoder()
    try:
        payload, _ = decoder.raw_decode(line)
    except json.JSONDecodeError:
        logger.warning("监控群消息里的 EVENT_JSON 无法解析，已忽略")
        return None
    return payload if isinstance(payload, dict) else None
