"""飞书免登（SSO）。

流程与参考项目 ``webAuth.js`` / ``authController.feishuLogin`` 一致：

    前端 → JSAPI 鉴权参数 → tt.requestAuthCode 拿 code
         → POST /api/auth/feishu-login {code}
         → 后端换 user_access_token → 取用户信息 → 签发本系统 JWT

浏览器场景走 OAuth v2，需要 redirect_uri 与授权请求完全一致。

JWT 用标准库实现 HS256，避免为一个功能引入额外依赖。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .client import FeishuClient, FeishuError

logger = logging.getLogger(__name__)

OAUTH_AUTHORIZE_URL = "https://open.feishu.cn/open-apis/authen/v1/index"


class SsoError(RuntimeError):
    pass


# ----------------------------------------------------------------------
# 最小 JWT（HS256）
# ----------------------------------------------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def issue_jwt(payload: dict[str, Any], secret: str, *, ttl_seconds: int) -> str:
    if not secret:
        raise SsoError("JWT_SECRET 未配置，拒绝签发令牌")
    now = int(time.time())
    body = {**payload, "iat": now, "exp": now + ttl_seconds}
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64url(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode())
    signing_input = f"{header}.{claims}".encode("ascii")
    signature = _b64url(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    return f"{header}.{claims}.{signature}"


def verify_jwt(token: str, secret: str) -> dict[str, Any]:
    if not secret:
        raise SsoError("JWT_SECRET 未配置")
    try:
        header_b64, claims_b64, signature_b64 = token.split(".")
    except ValueError as exc:
        raise SsoError("令牌格式错误") from exc
    signing_input = f"{header_b64}.{claims_b64}".encode("ascii")
    expected = _b64url(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature_b64):
        raise SsoError("令牌签名无效")
    try:
        claims = json.loads(_b64url_decode(claims_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise SsoError("令牌内容无法解析") from exc
    if int(claims.get("exp", 0)) < int(time.time()):
        raise SsoError("令牌已过期")
    return claims


# ----------------------------------------------------------------------
# SSO 服务
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class FeishuUser:
    open_id: str
    name: str = ""
    avatar_url: str = ""
    email: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "open_id": self.open_id,
            "name": self.name,
            "avatar_url": self.avatar_url,
            "email": self.email,
        }


class SsoService:
    def __init__(
        self,
        client: FeishuClient,
        *,
        jwt_secret: str,
        jwt_ttl_hours: int = 12,
        nonce_str: str = "hil2_default_nonce",
        redirect_uri: str = "",
    ) -> None:
        self.client = client
        self.jwt_secret = jwt_secret
        self.jwt_ttl_hours = jwt_ttl_hours
        self.nonce_str = nonce_str
        self.redirect_uri = redirect_uri

    # -- JSAPI 免登 ---------------------------------------------------
    def jsapi_auth_params(self, url: str) -> dict[str, Any]:
        """返回 h5sdk.config 所需的鉴权参数。签名串顺序固定，不能改。"""
        ticket = self.client.jsapi_ticket()
        timestamp = int(time.time() * 1000)
        verify_str = (
            f"jsapi_ticket={ticket}&noncestr={self.nonce_str}"
            f"&timestamp={timestamp}&url={url}"
        )
        signature = hashlib.sha1(verify_str.encode("utf-8")).hexdigest()
        return {
            "appId": self.client.app_id,
            "timestamp": timestamp,
            "nonceStr": self.nonce_str,
            "signature": signature,
        }

    # -- 浏览器 OAuth --------------------------------------------------
    def oauth_url(self, *, state: str = "", redirect_uri: str = "") -> str:
        target = redirect_uri or self.redirect_uri
        if not target:
            raise SsoError("未配置 SSO_REDIRECT_URI")
        return (
            f"{OAUTH_AUTHORIZE_URL}?app_id={quote(self.client.app_id)}"
            f"&redirect_uri={quote(target, safe='')}&state={quote(state)}"
        )

    # -- 登录 ----------------------------------------------------------
    def login(self, code: str, redirect_uri: str | None = None) -> dict[str, Any]:
        if not code:
            raise SsoError("授权码不能为空")
        try:
            token_data = self.client.get_user_access_token(code, redirect_uri)
            access_token = token_data.get("access_token") or (
                token_data.get("data") or {}
            ).get("access_token", "")
            if not access_token:
                raise SsoError("飞书未返回 access_token")
            info = self.client.get_user_info(access_token)
        except FeishuError as exc:
            # 不把飞书返回体原样透出，避免泄漏内部信息。
            logger.warning("飞书登录失败：%s", exc)
            raise SsoError("飞书登录失败，请重试") from exc

        user = FeishuUser(
            open_id=str(info.get("open_id", "")),
            name=str(info.get("name", "")),
            avatar_url=str(info.get("avatar_url", "")),
            email=str(info.get("email", "") or info.get("enterprise_email", "")),
        )
        if not user.open_id:
            raise SsoError("飞书未返回 open_id")

        token = issue_jwt(
            {"sub": user.open_id, "name": user.name, "avatar": user.avatar_url},
            self.jwt_secret,
            ttl_seconds=self.jwt_ttl_hours * 3600,
        )
        return {"token": token, "user": user.as_dict()}

    def verify(self, token: str) -> dict[str, Any]:
        return verify_jwt(token, self.jwt_secret)
