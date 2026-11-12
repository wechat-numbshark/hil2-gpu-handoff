"""飞书层：卡片构建、回调解密验签解析、loop guard。"""

from __future__ import annotations

import base64
import hashlib
import json
import os

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from hil2.feishu import cards
from hil2.feishu.callback import (
    CallbackError,
    compute_signature,
    decrypt,
    extract_monitor_payload,
    parse,
    unwrap,
    verify_signature,
)

ENCRYPT_KEY = "test-encrypt-key"


def encrypt_payload(plaintext: str, key: str = ENCRYPT_KEY) -> str:
    """按飞书的方式加密，用于构造真实形状的测试输入。"""
    aes_key = hashlib.sha256(key.encode()).digest()
    iv = os.urandom(16)
    data = plaintext.encode("utf-8")
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + encryptor.update(data) + encryptor.finalize()).decode()


# ----------------------------------------------------------------------
# 卡片
# ----------------------------------------------------------------------
class TestCards:
    def test_confirmation_card_has_exactly_three_buttons(self):
        card = cards.build_confirmation_card(
            cluster="52", current_open_id="ou_a", card_token="tok", reason="无活动"
        )
        buttons = card["elements"][2]["actions"]
        assert len(buttons) == 3
        assert [b["value"]["operation"] for b in buttons] == [
            "continue_30", "continue_60", "released"
        ]

    def test_every_button_carries_cluster_and_token(self):
        card = cards.build_confirmation_card(
            cluster="58", current_open_id="ou_a", card_token="tok-9", reason="r"
        )
        for button in card["elements"][2]["actions"]:
            assert button["value"]["cluster"] == "58"
            assert button["value"]["card_token"] == "tok-9"

    def test_confirmation_card_mentions_current_user(self):
        card = cards.build_confirmation_card(
            cluster="52", current_open_id="ou_zhang", current_name="张工",
            card_token="tok", reason="r"
        )
        assert '<at id="ou_zhang">张工</at>' in card["elements"][0]["content"]

    def test_confirmation_card_states_no_auto_release(self):
        card = cards.build_confirmation_card(
            cluster="52", current_open_id="ou_a", card_token="tok", reason="r"
        )
        body = json.dumps(card, ensure_ascii=False)
        assert "不会因未回复而自动释放" in body
        assert "不会停容器" in body

    def test_resolved_card_has_no_buttons(self):
        card = cards.build_resolved_card(cluster="52", operator_name="张工", outcome="继续使用")
        assert not any(el.get("tag") == "action" for el in card["elements"])

    def test_next_available_card_mentions_next_user(self):
        card = cards.build_next_available_card(
            cluster="60", next_open_id="ou_li", next_name="李工", reason="核心容器全停"
        )
        assert '<at id="ou_li">李工</at>' in card["elements"][0]["content"]

    def test_admin_alert_mentions_all_admins(self):
        card = cards.build_admin_alert_card(
            cluster="52", admin_open_ids=["ou_a", "ou_b"], title="采集异常", detail="docker 不可读"
        )
        content = card["elements"][0]["content"]
        assert "ou_a" in content and "ou_b" in content

    def test_admin_alert_without_admins_still_renders(self):
        card = cards.build_admin_alert_card(
            cluster="52", admin_open_ids=[], title="t", detail="d"
        )
        assert "管理员" in card["elements"][0]["content"]

    def test_reminder_card_repeats_no_auto_release(self):
        card = cards.build_reminder_card(
            cluster="52", current_open_id="ou_a", waited_minutes=10
        )
        assert "不会自动释放" in json.dumps(card, ensure_ascii=False)

    def test_cards_disable_forwarding(self):
        built = [
            cards.build_confirmation_card(cluster="52", current_open_id="o", card_token="t", reason="r"),
            cards.build_next_available_card(cluster="52", next_open_id="o"),
            cards.build_reminder_card(cluster="52", current_open_id="o", waited_minutes=10),
        ]
        for card in built:
            assert card["config"]["enable_forward"] is False


# ----------------------------------------------------------------------
# 解密与验签
# ----------------------------------------------------------------------
class TestDecrypt:
    def test_round_trip(self):
        plaintext = json.dumps({"hello": "世界"}, ensure_ascii=False)
        assert decrypt(ENCRYPT_KEY, encrypt_payload(plaintext)) == plaintext

    def test_wrong_key_fails(self):
        with pytest.raises(CallbackError):
            decrypt("another-key", encrypt_payload('{"a":1}'))

    def test_missing_key_fails(self):
        with pytest.raises(CallbackError):
            decrypt("", encrypt_payload('{"a":1}'))

    def test_garbage_fails(self):
        for bad in ("not-base64!!!", base64.b64encode(b"short").decode()):
            with pytest.raises(CallbackError):
                decrypt(ENCRYPT_KEY, bad)


class TestSignature:
    def test_valid_signature_accepted(self):
        body = b'{"a":1}'
        signature = compute_signature(
            timestamp="1725000000", nonce="n1", encrypt_key=ENCRYPT_KEY, body=body
        )
        assert verify_signature(
            timestamp="1725000000", nonce="n1", encrypt_key=ENCRYPT_KEY,
            body=body, signature=signature
        )

    def test_tampered_body_rejected(self):
        signature = compute_signature(
            timestamp="1", nonce="n", encrypt_key=ENCRYPT_KEY, body=b'{"a":1}'
        )
        assert not verify_signature(
            timestamp="1", nonce="n", encrypt_key=ENCRYPT_KEY,
            body=b'{"a":2}', signature=signature
        )

    def test_missing_parts_rejected(self):
        assert not verify_signature(
            timestamp="", nonce="n", encrypt_key=ENCRYPT_KEY, body=b"{}", signature="x"
        )


class TestUnwrap:
    def test_plaintext_with_matching_token(self):
        payload = unwrap({"token": "vt", "type": "x"}, verification_token="vt")
        assert payload["type"] == "x"

    def test_token_mismatch_rejected(self):
        with pytest.raises(CallbackError):
            unwrap({"token": "wrong"}, verification_token="vt")

    def test_v2_header_token_checked(self):
        payload = unwrap({"header": {"token": "vt"}}, verification_token="vt")
        assert payload["header"]["token"] == "vt"

    def test_encrypted_payload_decrypted_then_checked(self):
        inner = json.dumps({"token": "vt", "type": "url_verification", "challenge": "c1"})
        payload = unwrap(
            {"encrypt": encrypt_payload(inner)},
            encrypt_key=ENCRYPT_KEY, verification_token="vt",
        )
        assert payload["challenge"] == "c1"

    def test_non_dict_rejected(self):
        with pytest.raises(CallbackError):
            unwrap(["not", "a", "dict"])


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------
class TestParse:
    def test_url_verification(self):
        envelope = parse({"type": "url_verification", "challenge": "abc"})
        assert envelope.kind == "url_verification"
        assert envelope.challenge == "abc"

    def test_text_message(self):
        envelope = parse({
            "header": {"event_id": "e1", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_webhook"}},
                "message": {
                    "chat_id": "oc_monitor",
                    "message_type": "text",
                    "content": json.dumps({"text": "状态：使用中"}, ensure_ascii=False),
                },
            },
        })
        assert envelope.kind == "message"
        assert envelope.chat_id == "oc_monitor"
        assert envelope.sender_open_id == "ou_webhook"
        assert "使用中" in envelope.text

    def test_post_message_flattened(self):
        content = json.dumps({
            "zh_cn": {
                "title": "HIL2.0",
                "content": [[{"tag": "at", "user_id": "ou_bot"}, {"tag": "text", "text": "行一"}],
                            [{"tag": "text", "text": "行二"}]],
            }
        }, ensure_ascii=False)
        envelope = parse({
            "header": {"event_id": "e2", "event_type": "im.message.receive_v1"},
            "event": {"message": {"message_type": "post", "content": content}},
        })
        assert "行一" in envelope.text and "行二" in envelope.text

    def test_card_action(self):
        envelope = parse({
            "header": {"event_id": "e3", "event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_user"},
                "action": {"value": {"operation": "continue_30", "cluster": "52",
                                     "card_token": "tok"}},
            },
        })
        assert envelope.kind == "card_action"
        assert envelope.operator_open_id == "ou_user"
        assert envelope.card_action_value["operation"] == "continue_30"

    def test_card_action_with_string_value(self):
        # 有的租户把 value 序列化成字符串再发过来。
        envelope = parse({
            "header": {"event_id": "e4", "event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_user"},
                "action": {"value": json.dumps({"operation": "released", "cluster": "52"})},
            },
        })
        assert envelope.card_action_value["operation"] == "released"

    def test_unsupported_event_marked_other(self):
        envelope = parse({"header": {"event_type": "im.chat.updated_v1"}, "event": {}})
        assert envelope.kind == "other"

    def test_malformed_message_content_yields_empty_text(self):
        envelope = parse({
            "header": {"event_type": "im.message.receive_v1"},
            "event": {"message": {"message_type": "text", "content": "not json"}},
        })
        assert envelope.text == ""


class TestExtractMonitorPayload:
    def _text(self, payload: dict) -> str:
        return (
            "【HIL2.0 监控事件｜供机器人处理】\n"
            "🟡 52 集群：疑似空闲\n"
            "EVENT_JSON=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )

    def test_extracts_event(self):
        payload = {"schema": "hil.monitor.v1", "cluster": "52", "state": "suspected_idle"}
        assert extract_monitor_payload(self._text(payload)) == payload

    def test_plain_chat_message_ignored(self):
        assert extract_monitor_payload("今天 52 集群谁在用？") is None

    def test_trailing_content_after_json_tolerated(self):
        payload = {"cluster": "52"}
        text = self._text(payload) + "   \n附言：请注意"
        assert extract_monitor_payload(text) == payload

    def test_broken_json_returns_none_not_raise(self):
        assert extract_monitor_payload("EVENT_JSON={不是合法 JSON") is None

    def test_non_object_json_rejected(self):
        assert extract_monitor_payload('EVENT_JSON=["a","b"]') is None
