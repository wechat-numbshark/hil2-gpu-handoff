"""飞书开放平台客户端。

移植自参考项目 ``server/src/integrations/feishu/webAuth.js`` 的 token 缓存策略：
tenant_access_token 提前 5 分钟过期，进程内缓存，失败不缓存。

本客户端只做「发消息 / 发卡片 / 更新卡片」，不含任何操作集群的能力。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 提前过期余量，避免边界上用到刚失效的 token。
TOKEN_EXPIRY_SKEW_SECONDS = 300


class FeishuError(RuntimeError):
    """飞书接口调用失败。调用方需按 fail-closed 处理。"""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _CachedToken:
    value: str = ""
    expires_at: float = 0.0

    @property
    def valid(self) -> bool:
        return bool(self.value) and time.time() < self.expires_at


class FeishuClient:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        base_url: str = "https://open.feishu.cn/open-apis",
        timeout: float = 10.0,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._lock = threading.Lock()
        self._tenant_token = _CachedToken()
        self._app_token = _CachedToken()
        self._jsapi_ticket = _CachedToken()
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # 凭据
    # ------------------------------------------------------------------
    def _fetch_internal_token(self, path: str, field: str, cache: _CachedToken) -> str:
        if not self.configured:
            raise FeishuError("飞书应用凭据未配置")
        with self._lock:
            if cache.valid:
                return cache.value
            try:
                response = self._client.post(
                    path, json={"app_id": self.app_id, "app_secret": self.app_secret}
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as exc:
                raise FeishuError(f"获取 {field} 失败：{type(exc).__name__}") from exc
            if payload.get("code") != 0:
                # 不打印 msg 之外的内容，避免把凭据写进日志。
                raise FeishuError(f"获取 {field} 失败：{payload.get('msg')}", code=payload.get("code"))
            cache.value = payload[field]
            cache.expires_at = time.time() + max(60, int(payload.get("expire", 7200))) - TOKEN_EXPIRY_SKEW_SECONDS
            return cache.value

    def tenant_access_token(self) -> str:
        return self._fetch_internal_token(
            "/auth/v3/tenant_access_token/internal", "tenant_access_token", self._tenant_token
        )

    def app_access_token(self) -> str:
        return self._fetch_internal_token(
            "/auth/v3/app_access_token/internal", "app_access_token", self._app_token
        )

    def jsapi_ticket(self) -> str:
        with self._lock:
            if self._jsapi_ticket.valid:
                return self._jsapi_ticket.value
        token = self.tenant_access_token()
        payload = self._request("POST", "/jssdk/ticket/get", token=token, json_body={})
        data = payload.get("data") or {}
        with self._lock:
            self._jsapi_ticket.value = data.get("ticket", "")
            self._jsapi_ticket.expires_at = (
                time.time() + max(60, int(data.get("expire_in", 7200))) - TOKEN_EXPIRY_SKEW_SECONDS
            )
            return self._jsapi_ticket.value

    def invalidate_tokens(self) -> None:
        """凭据被判为失效时清空缓存，下次调用重新获取。"""
        with self._lock:
            self._tenant_token = _CachedToken()
            self._app_token = _CachedToken()
            self._jsapi_ticket = _CachedToken()

    # ------------------------------------------------------------------
    # 通用请求
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = token or self.tenant_access_token()
        try:
            response = self._client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
            payload = response.json()
        except httpx.HTTPError as exc:
            raise FeishuError(f"{path} 请求失败：{type(exc).__name__}") from exc
        except ValueError as exc:
            raise FeishuError(f"{path} 返回非 JSON") from exc

        code = payload.get("code")
        if code not in (0, None):
            # 99991663/99991664：token 失效，清缓存让下次重试。
            if code in (99991663, 99991664, 99991661):
                self.invalidate_tokens()
            raise FeishuError(f"{path} 返回失败：{payload.get('msg')}", code=code)
        return payload

    # ------------------------------------------------------------------
    # 消息
    # ------------------------------------------------------------------
    def send_text(self, *, chat_id: str, text: str) -> str:
        """发纯文本。@人请在 text 中使用 <at user_id="ou_xxx"></at>。"""
        payload = self._request(
            "POST",
            "/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            json_body={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
        return (payload.get("data") or {}).get("message_id", "")

    def list_chat_messages(self, *, chat_id: str, page_size: int = 50) -> list[dict[str, Any]]:
        """读取群内最近消息。

        自定义 Webhook 机器人发出的消息不会触发应用机器人的消息事件，
        因此内网部署用此接口轮询监控群，补齐 Webhook -> 系统的最后一跳。
        """
        payload = self._request(
            "GET",
            "/im/v1/messages",
            params={
                "container_id_type": "chat",
                "container_id": chat_id,
                "sort_type": "ByCreateTimeDesc",
                "page_size": max(1, min(int(page_size), 50)),
            },
        )
        items = (payload.get("data") or {}).get("items") or []
        return [item for item in items if isinstance(item, dict)]

    def send_card(self, *, chat_id: str, card: dict[str, Any]) -> str:
        payload = self._request(
            "POST",
            "/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            json_body={
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )
        return (payload.get("data") or {}).get("message_id", "")

    def update_card(self, *, message_id: str, card: dict[str, Any]) -> None:
        """把已发出的卡片替换成结果态，防止旧卡片被重复点击。"""
        self._request(
            "PATCH",
            f"/im/v1/messages/{message_id}",
            json_body={"content": json.dumps(card, ensure_ascii=False)},
        )

    # ------------------------------------------------------------------
    # 用户
    # ------------------------------------------------------------------
    def get_user_access_token(self, code: str, redirect_uri: str | None = None) -> dict[str, Any]:
        """OAuth v2 换取 user_access_token。

        浏览器 OAuth 必须带与授权请求完全一致的 redirect_uri；
        飞书客户端 JSAPI 临时码不需要，故该参数可选。
        """
        body: dict[str, Any] = {
            "grant_type": "authorization_code",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "code": code,
        }
        if redirect_uri:
            body["redirect_uri"] = redirect_uri
        try:
            response = self._client.post("/authen/v2/oauth/token", json=body)
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FeishuError(f"换取用户令牌失败：{type(exc).__name__}") from exc
        if payload.get("code") not in (0, None):
            raise FeishuError(f"换取用户令牌失败：{payload.get('msg')}", code=payload.get("code"))
        return payload

    def get_user_info(self, user_access_token: str) -> dict[str, Any]:
        payload = self._request("GET", "/authen/v1/user_info", token=user_access_token)
        return payload.get("data") or {}
