"""飞书 HTTP 客户端：token 缓存、错误码处理、消息发送。

用 httpx.MockTransport 走真实的 httpx 请求路径，只是不出网。
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from hil2.feishu.client import FeishuClient, FeishuError


class Backend:
    """可编程的假飞书后端，记录全部请求。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tenant_token = "t-1"
        self.tenant_expire = 7200
        self.tenant_code = 0
        self.message_code = 0
        self.message_calls = 0
        self.tenant_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path.endswith("/auth/v3/tenant_access_token/internal"):
            self.tenant_calls += 1
            if self.tenant_code:
                return httpx.Response(200, json={"code": self.tenant_code, "msg": "bad app"})
            return httpx.Response(200, json={
                "code": 0, "tenant_access_token": self.tenant_token, "expire": self.tenant_expire
            })

        if path.endswith("/auth/v3/app_access_token/internal"):
            return httpx.Response(200, json={
                "code": 0, "app_access_token": "a-1", "expire": 7200
            })

        if path.endswith("/jssdk/ticket/get"):
            return httpx.Response(200, json={
                "code": 0, "data": {"ticket": "tk-1", "expire_in": 7200}
            })

        if path.endswith("/im/v1/messages") and request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"items": [
                {"message_id": "om_read", "msg_type": "text"}
            ]}})

        if path.endswith("/im/v1/messages"):
            self.message_calls += 1
            if self.message_code:
                return httpx.Response(200, json={"code": self.message_code, "msg": "denied"})
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_1"}})

        if "/im/v1/messages/" in path:
            return httpx.Response(200, json={"code": 0})

        if path.endswith("/authen/v2/oauth/token"):
            return httpx.Response(200, json={"code": 0, "access_token": "u-1"})

        if path.endswith("/authen/v1/user_info"):
            return httpx.Response(200, json={
                "code": 0, "data": {"open_id": "ou_a", "name": "张工"}
            })

        return httpx.Response(404, json={"code": 404, "msg": "not found"})


@pytest.fixture
def rig():
    backend = Backend()
    client = FeishuClient(app_id="cli_x", app_secret="sec_x", base_url="https://open.invalid/open-apis")
    client._client = httpx.Client(  # noqa: SLF001  注入 mock transport
        base_url="https://open.invalid/open-apis",
        transport=httpx.MockTransport(backend.handler),
    )
    yield client, backend
    client.close()


class TestTokenCache:
    def test_first_call_fetches(self, rig):
        client, backend = rig
        assert client.tenant_access_token() == "t-1"
        assert backend.tenant_calls == 1

    def test_second_call_uses_cache(self, rig):
        client, backend = rig
        client.tenant_access_token()
        client.tenant_access_token()
        client.tenant_access_token()
        assert backend.tenant_calls == 1

    def test_short_expiry_forces_refetch(self, rig):
        client, backend = rig
        # expire 小于提前量时应立刻过期，而不是缓存一个已死的 token。
        backend.tenant_expire = 60
        client.tenant_access_token()
        client.tenant_access_token()
        assert backend.tenant_calls == 2

    def test_failure_is_not_cached(self, rig):
        client, backend = rig
        backend.tenant_code = 10003
        with pytest.raises(FeishuError):
            client.tenant_access_token()
        backend.tenant_code = 0
        assert client.tenant_access_token() == "t-1"
        assert backend.tenant_calls == 2

    def test_invalidate_clears_cache(self, rig):
        client, backend = rig
        client.tenant_access_token()
        client.invalidate_tokens()
        client.tenant_access_token()
        assert backend.tenant_calls == 2

    def test_app_token_and_jsapi_ticket(self, rig):
        client, _ = rig
        assert client.app_access_token() == "a-1"
        assert client.jsapi_ticket() == "tk-1"

    def test_unconfigured_client_refuses(self):
        client = FeishuClient(app_id="", app_secret="")
        assert client.configured is False
        with pytest.raises(FeishuError):
            client.tenant_access_token()
        client.close()


class TestMessages:
    def test_send_card_returns_message_id(self, rig):
        client, backend = rig
        assert client.send_card(chat_id="oc_1", card={"header": {}}) == "om_1"
        request = backend.requests[-1]
        assert request.url.params["receive_id_type"] == "chat_id"
        body = json.loads(request.content)
        assert body["msg_type"] == "interactive"
        assert body["receive_id"] == "oc_1"

    def test_card_content_is_json_string(self, rig):
        client, backend = rig
        client.send_card(chat_id="oc_1", card={"header": {"title": "中文"}})
        body = json.loads(backend.requests[-1].content)
        # 飞书要求 content 是字符串化的 JSON，且中文不能被转义成 \\u。
        assert isinstance(body["content"], str)
        assert "中文" in body["content"]

    def test_send_text(self, rig):
        client, backend = rig
        client.send_text(chat_id="oc_1", text='<at user_id="ou_a"></at> 可用')
        body = json.loads(backend.requests[-1].content)
        assert body["msg_type"] == "text"
        assert "ou_a" in body["content"]

    def test_list_chat_messages(self, rig):
        client, backend = rig
        assert client.list_chat_messages(chat_id="oc_1") == [
            {"message_id": "om_read", "msg_type": "text"}
        ]
        request = backend.requests[-1]
        assert request.method == "GET"
        assert request.url.params["container_id"] == "oc_1"

    def test_update_card_uses_patch(self, rig):
        client, backend = rig
        client.update_card(message_id="om_9", card={"header": {}})
        assert backend.requests[-1].method == "PATCH"
        assert backend.requests[-1].url.path.endswith("/im/v1/messages/om_9")

    def test_error_code_raises(self, rig):
        client, backend = rig
        backend.message_code = 230001
        with pytest.raises(FeishuError) as excinfo:
            client.send_card(chat_id="oc_1", card={})
        assert excinfo.value.code == 230001

    def test_expired_token_code_clears_cache(self, rig):
        client, backend = rig
        client.tenant_access_token()
        backend.message_code = 99991663
        with pytest.raises(FeishuError):
            client.send_card(chat_id="oc_1", card={})
        # token 失效码必须清缓存，否则后续请求会一直用死 token。
        client.tenant_access_token()
        assert backend.tenant_calls == 2

    def test_authorization_header_sent(self, rig):
        client, backend = rig
        client.send_card(chat_id="oc_1", card={})
        assert backend.requests[-1].headers["Authorization"] == "Bearer t-1"

    def test_network_error_wrapped(self):
        def boom(request):
            raise httpx.ConnectError("no route")

        client = FeishuClient(app_id="a", app_secret="b")
        client._client = httpx.Client(transport=httpx.MockTransport(boom))  # noqa: SLF001
        with pytest.raises(FeishuError):
            client.tenant_access_token()
        client.close()

    def test_non_json_response_wrapped(self, rig):
        client, backend = rig

        def html(request):
            backend.requests.append(request)
            if request.url.path.endswith("tenant_access_token/internal"):
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "t",
                                                 "expire": 7200})
            return httpx.Response(200, text="<html>502</html>")

        client._client = httpx.Client(  # noqa: SLF001
            base_url="https://open.invalid/open-apis", transport=httpx.MockTransport(html)
        )
        with pytest.raises(FeishuError, match="非 JSON"):
            client.send_card(chat_id="oc_1", card={})


class TestUserAuth:
    def test_oauth_token_includes_redirect_when_given(self, rig):
        client, backend = rig
        client.get_user_access_token("code-1", "https://app.invalid/cb")
        body = json.loads(backend.requests[-1].content)
        assert body["redirect_uri"] == "https://app.invalid/cb"
        assert body["grant_type"] == "authorization_code"

    def test_oauth_token_omits_redirect_for_jsapi_code(self, rig):
        client, backend = rig
        client.get_user_access_token("code-1")
        assert "redirect_uri" not in json.loads(backend.requests[-1].content)

    def test_user_info_uses_user_token_not_tenant(self, rig):
        client, backend = rig
        assert client.get_user_info("u-at-9")["open_id"] == "ou_a"
        assert backend.requests[-1].headers["Authorization"] == "Bearer u-at-9"
