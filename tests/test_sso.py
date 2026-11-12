"""飞书免登（SSO）：JWT、JSAPI 签名、登录流程。"""

from __future__ import annotations

import hashlib
import time
from typing import Any

import pytest

from hil2.feishu.client import FeishuError
from hil2.feishu.sso import SsoError, SsoService, issue_jwt, verify_jwt

SECRET = "unit-test-secret"


class FakeClient:
    """只实现 SSO 需要的四个方法。"""

    app_id = "cli_test"

    def __init__(self, *, user: dict[str, Any] | None = None, fail: str = "") -> None:
        self.user = user or {
            "open_id": "ou_zhang",
            "name": "张工",
            "avatar_url": "https://example.invalid/a.png",
            "email": "zhang@example.invalid",
        }
        self.fail = fail
        self.calls: list[tuple[str, Any]] = []

    def jsapi_ticket(self) -> str:
        if self.fail == "ticket":
            raise FeishuError("ticket 获取失败")
        return "ticket_abc"

    def get_user_access_token(self, code: str, redirect_uri: str | None = None) -> dict[str, Any]:
        self.calls.append(("token", {"code": code, "redirect_uri": redirect_uri}))
        if self.fail == "token":
            raise FeishuError("换令牌失败")
        if self.fail == "no_token":
            return {"code": 0}
        return {"code": 0, "access_token": "u-at-1", "refresh_token": "u-rt-1"}

    def get_user_info(self, token: str) -> dict[str, Any]:
        self.calls.append(("info", token))
        if self.fail == "info":
            raise FeishuError("取用户信息失败")
        return self.user


@pytest.fixture
def sso() -> SsoService:
    return SsoService(FakeClient(), jwt_secret=SECRET, redirect_uri="https://hil.example/cb")


class TestJwt:
    def test_round_trip(self):
        token = issue_jwt({"sub": "ou_a", "name": "张工"}, SECRET, ttl_seconds=60)
        claims = verify_jwt(token, SECRET)
        assert claims["sub"] == "ou_a"
        assert claims["name"] == "张工"
        assert claims["exp"] > time.time()

    def test_tampered_signature_rejected(self):
        token = issue_jwt({"sub": "ou_a"}, SECRET, ttl_seconds=60)
        head, claims, _ = token.split(".")
        with pytest.raises(SsoError, match="签名"):
            verify_jwt(f"{head}.{claims}.deadbeef", SECRET)

    def test_wrong_secret_rejected(self):
        token = issue_jwt({"sub": "ou_a"}, SECRET, ttl_seconds=60)
        with pytest.raises(SsoError):
            verify_jwt(token, "other-secret")

    def test_expired_rejected(self):
        token = issue_jwt({"sub": "ou_a"}, SECRET, ttl_seconds=-1)
        with pytest.raises(SsoError, match="过期"):
            verify_jwt(token, SECRET)

    def test_malformed_rejected(self):
        for bad in ("", "a.b", "a.b.c.d"):
            with pytest.raises(SsoError):
                verify_jwt(bad, SECRET)

    def test_issuing_without_secret_refused(self):
        # 没配密钥就签发令牌等于没有鉴权，必须拒绝而不是用空串。
        with pytest.raises(SsoError):
            issue_jwt({"sub": "ou_a"}, "", ttl_seconds=60)

    def test_unicode_claims_survive(self):
        token = issue_jwt({"sub": "ou_a", "name": "李四·测试"}, SECRET, ttl_seconds=60)
        assert verify_jwt(token, SECRET)["name"] == "李四·测试"


class TestJsapiParams:
    def test_signature_matches_feishu_spec(self, sso: SsoService):
        params = sso.jsapi_auth_params("https://hil.example/dash")
        expected = hashlib.sha1(
            f"jsapi_ticket=ticket_abc&noncestr={params['nonceStr']}"
            f"&timestamp={params['timestamp']}&url=https://hil.example/dash".encode()
        ).hexdigest()
        assert params["signature"] == expected
        assert params["appId"] == "cli_test"

    def test_different_urls_produce_different_signatures(self, sso: SsoService):
        a = sso.jsapi_auth_params("https://hil.example/a")
        b = sso.jsapi_auth_params("https://hil.example/b")
        assert a["signature"] != b["signature"]

    def test_ticket_failure_propagates(self):
        service = SsoService(FakeClient(fail="ticket"), jwt_secret=SECRET)
        with pytest.raises(FeishuError):
            service.jsapi_auth_params("https://hil.example/")


class TestOauthUrl:
    def test_contains_app_id_and_encoded_redirect(self, sso: SsoService):
        url = sso.oauth_url(state="s1")
        assert "app_id=cli_test" in url
        assert "redirect_uri=https%3A%2F%2Fhil.example%2Fcb" in url
        assert "state=s1" in url

    def test_missing_redirect_refused(self):
        service = SsoService(FakeClient(), jwt_secret=SECRET)
        with pytest.raises(SsoError):
            service.oauth_url()


class TestLogin:
    def test_successful_login_returns_token_and_user(self, sso: SsoService):
        result = sso.login("code-1")
        assert result["user"]["open_id"] == "ou_zhang"
        assert result["user"]["name"] == "张工"
        assert verify_jwt(result["token"], SECRET)["sub"] == "ou_zhang"

    def test_redirect_uri_forwarded_for_browser_oauth(self, sso: SsoService):
        sso.login("code-1", "https://hil.example/cb")
        token_call = next(c for c in sso.client.calls if c[0] == "token")
        assert token_call[1]["redirect_uri"] == "https://hil.example/cb"

    def test_client_jsapi_code_needs_no_redirect(self, sso: SsoService):
        sso.login("code-1")
        token_call = next(c for c in sso.client.calls if c[0] == "token")
        assert token_call[1]["redirect_uri"] is None

    def test_empty_code_refused_without_network_call(self, sso: SsoService):
        with pytest.raises(SsoError):
            sso.login("")
        assert sso.client.calls == []

    def test_feishu_failure_does_not_leak_internals(self):
        service = SsoService(FakeClient(fail="token"), jwt_secret=SECRET)
        with pytest.raises(SsoError) as excinfo:
            service.login("code-1")
        assert "换令牌失败" not in str(excinfo.value)

    def test_missing_access_token_refused(self):
        service = SsoService(FakeClient(fail="no_token"), jwt_secret=SECRET)
        with pytest.raises(SsoError):
            service.login("code-1")

    def test_missing_open_id_refused(self):
        service = SsoService(FakeClient(user={"name": "无 id"}), jwt_secret=SECRET)
        with pytest.raises(SsoError, match="open_id"):
            service.login("code-1")

    def test_enterprise_email_fallback(self):
        client = FakeClient(user={"open_id": "ou_a", "name": "张", "enterprise_email": "a@corp"})
        service = SsoService(client, jwt_secret=SECRET)
        assert service.login("code-1")["user"]["email"] == "a@corp"

    def test_verify_accepts_issued_token(self, sso: SsoService):
        token = sso.login("code-1")["token"]
        assert sso.verify(token)["sub"] == "ou_zhang"
