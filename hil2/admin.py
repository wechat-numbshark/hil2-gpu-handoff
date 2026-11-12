"""管理后台接口。

覆盖客户提出的四件事：
1. 集群监控配置（增删改、阈值、容器识别规则）
2. 采集方式切换（agent 推送 / SSH 拉取），并能测试连接
3. 下载每台集群专属的 agent 安装包
4. 飞书组织同步与管理员指派

鉴权：所有接口都要求管理员身份。管理员来自通讯录里被标记的人；
系统初始为空时，允许 ``.env`` 里的 ``FEISHU_ADMIN_OPEN_IDS`` 引导第一批。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from . import collector_ssh, packaging
from .booking import BookingError
from .orgsync import OrgSyncError, OrgSyncService
from .registry import ClusterConfig, CollectMode, RegistryError

logger = logging.getLogger(__name__)

# 可在后台改的配置项白名单。没列进来的只能改 .env，避免关键安全项被网页改掉。
EDITABLE_SETTINGS = {
    "feishu_monitor_chat_id": "监控群 ID",
    "feishu_business_chat_id": "业务大群 ID",
    "feishu_bot_open_id": "业务机器人 open_id",
    "coordinator_public_url": "协调服务对集群暴露的地址",
    "dashboard_refresh_seconds": "大盘刷新间隔（秒）",
    "stale_after_minutes": "数据过期阈值（分钟）",
    "remind_after_minutes": "卡片提醒等待（分钟）",
    "escalate_after_minutes": "卡片升级等待（分钟）",
    "booking_max_hours": "单次预约最长（小时）",
    "booking_horizon_days": "最远可约（天）",
    "booking_reminder_minutes": "预约开始前提醒（分钟）",
    "booking_day_start_hour": "每天最早预约时间（整点）",
    "booking_day_end_hour": "每天最晚结束时间（整点）",
}


def build_router(get_state, current_user) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["admin"])

    # ------------------------------------------------------------------
    def require_admin(request: Request, user=Depends(current_user)) -> str:
        """确认操作人是管理员，返回其 open_id。"""
        state = request.app.state.hil2
        settings = state.settings

        open_id = str(user.get("sub", "")) if user else ""
        if not settings.sso_enabled:
            # 免登模式：后台只在本机可用，避免内网任何人都能改配置
            client_host = request.client.host if request.client else ""
            if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
                raise HTTPException(
                    status_code=403,
                    detail="未开启飞书登录时，管理后台只允许本机访问。"
                           "要给同事用请在 .env 里设 SSO_ENABLED=true",
                )
            return open_id or "local-admin"

        if not open_id:
            raise HTTPException(status_code=401, detail="需要先用飞书登录")

        admins = set(state.store.admin_open_ids()) | set(settings.feishu_admin_open_ids)
        if not admins:
            raise HTTPException(
                status_code=403,
                detail="系统还没有任何管理员。请先在 .env 的 FEISHU_ADMIN_OPEN_IDS 里指定第一批。",
            )
        if open_id not in admins:
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return open_id

    # ------------------------------------------------------------------
    # 概览
    # ------------------------------------------------------------------
    @router.get("/overview")
    async def overview(request: Request, actor: str = Depends(require_admin)):
        state = request.app.state.hil2
        clusters = state.store.list_clusters()
        by_mode: dict[str, int] = {}
        for config in clusters:
            by_mode[config.mode.value] = by_mode.get(config.mode.value, 0) + 1
        return {
            "actor": actor,
            "clusters": {
                "total": len(clusters),
                "enabled": sum(1 for c in clusters if c.enabled),
                "by_mode": by_mode,
                "with_error": [c.cluster for c in clusters if c.last_error],
            },
            "org": {
                **state.store.org_stats(),
                "last_synced_at": (
                    state.store.org_stats()["last_synced_at"].isoformat()
                    if state.store.org_stats()["last_synced_at"] else None
                ),
            },
            "feishu_configured": state.client.configured,
            "sso_enabled": state.settings.sso_enabled,
            "settings": state.runtime_settings(),
            "editable_settings": EDITABLE_SETTINGS,
            "audit": [
                {**item, "created_at": item["created_at"].isoformat() if item["created_at"] else None}
                for item in state.store.recent_admin_audit(limit=20)
            ],
        }

    # ------------------------------------------------------------------
    # 集群
    # ------------------------------------------------------------------
    @router.get("/clusters")
    async def list_clusters(request: Request, actor: str = Depends(require_admin)):
        state = request.app.state.hil2
        out = []
        for config in state.store.list_clusters():
            snapshot = state.store.get_snapshot(config.cluster)
            out.append({
                **config.as_dict(),
                "current_state": snapshot["state"].value if snapshot else None,
                "current_state_label": snapshot["state"].label if snapshot else "无数据",
                "observed_at": (
                    snapshot["observed_at"].isoformat()
                    if snapshot and snapshot["observed_at"] else None
                ),
            })
        return {"data": out, "modes": [
            {"value": mode.value, "label": mode.label} for mode in CollectMode
        ]}

    @router.post("/clusters")
    async def create_cluster(request: Request, actor: str = Depends(require_admin)):
        state = request.app.state.hil2
        payload = await request.json()
        cluster = str(payload.get("cluster", "")).strip()
        if state.store.get_cluster(cluster):
            raise HTTPException(status_code=409, detail=f"集群 {cluster} 已经存在")
        try:
            config = ClusterConfig.from_dict(payload)
            saved = await run_in_threadpool(state.store.save_cluster, config, actor=actor)
        except RegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"data": saved.as_dict()}

    @router.put("/clusters/{cluster}")
    async def update_cluster(cluster: str, request: Request, actor: str = Depends(require_admin)):
        state = request.app.state.hil2
        existing = state.store.get_cluster(cluster)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"集群 {cluster} 不存在")
        payload = await request.json()
        payload["cluster"] = cluster
        try:
            config = ClusterConfig.from_dict(payload, base=existing)
            saved = await run_in_threadpool(state.store.save_cluster, config, actor=actor)
        except RegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"data": saved.as_dict()}

    @router.delete("/clusters/{cluster}")
    async def delete_cluster(cluster: str, request: Request, actor: str = Depends(require_admin)):
        state = request.app.state.hil2
        if not await run_in_threadpool(state.store.delete_cluster, cluster, actor=actor):
            raise HTTPException(status_code=404, detail=f"集群 {cluster} 不存在")
        return {"deleted": cluster, "note": "已移出纳管；历史事件和预约保留"}

    @router.post("/clusters/{cluster}/test")
    async def test_cluster(cluster: str, request: Request, actor: str = Depends(require_admin)):
        """测试 SSH 连接并跑一次真实采集，把结果原样回给管理员。"""
        state = request.app.state.hil2
        config = state.store.get_cluster(cluster)
        if config is None:
            raise HTTPException(status_code=404, detail=f"集群 {cluster} 不存在")
        if config.mode is not CollectMode.SSH_PULL:
            raise HTTPException(
                status_code=400,
                detail="只有 SSH 拉取模式能测试连接。Agent 推送模式请看集群上报是否到达。",
            )
        result = await run_in_threadpool(collector_ssh.check_connection, config)
        state.store.log_admin(actor=actor, action="test_ssh", target=cluster,
                              detail="成功" if result["ok"] else result["error"])
        return {"data": result}

    @router.post("/clusters/{cluster}/collect")
    async def collect_now(cluster: str, request: Request, actor: str = Depends(require_admin)):
        state = request.app.state.hil2
        try:
            result = await run_in_threadpool(state.puller.force, cluster)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"data": result}

    @router.get("/clusters/{cluster}/agent.tar.gz")
    async def download_agent(cluster: str, request: Request, actor: str = Depends(require_admin)):
        """下载这台集群专属的安装包。包里含上报密钥，因此记审计。"""
        state = request.app.state.hil2
        config = state.store.get_cluster(cluster)
        if config is None:
            raise HTTPException(status_code=404, detail=f"集群 {cluster} 不存在")

        settings = state.runtime_settings()
        coordinator_url = settings.get("coordinator_public_url") or ""
        if not coordinator_url and not config.webhook_url:
            raise HTTPException(
                status_code=400,
                detail="请先为集群填写监控群 Webhook；如需直连备用通道，"
                       "再填写协调服务地址。",
            )
        ingest_url = (
            coordinator_url.rstrip("/") + "/api/ingest" if coordinator_url else ""
        )
        content, filename = await run_in_threadpool(
            packaging.build_bundle, config,
            coordinator_url=ingest_url,
            ingest_token=state.settings.ingest_token,
            bot_open_id=settings.get("feishu_bot_open_id", ""),
        )
        state.store.log_admin(actor=actor, action="download_agent", target=cluster,
                              detail="安装包含上报密钥")
        return Response(
            content=content,
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/clusters/{cluster}/probe-hint")
    async def probe_hint(cluster: str, request: Request, actor: str = Depends(require_admin)):
        """SSH 模式下直接跑一次现场探测，帮管理员确认容器名。"""
        state = request.app.state.hil2
        config = state.store.get_cluster(cluster)
        if config is None:
            raise HTTPException(status_code=404, detail=f"集群 {cluster} 不存在")
        if config.mode is not CollectMode.SSH_PULL:
            raise HTTPException(status_code=400, detail="Agent 模式请在集群上运行 probe_site.py")
        result = await run_in_threadpool(collector_ssh.check_connection, config)
        return {"data": result}

    # ------------------------------------------------------------------
    # 系统设置
    # ------------------------------------------------------------------
    @router.put("/settings")
    async def update_settings(request: Request, actor: str = Depends(require_admin)):
        state = request.app.state.hil2
        payload = await request.json()
        unknown = set(payload) - set(EDITABLE_SETTINGS)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"这些配置不允许在后台修改：{'、'.join(sorted(unknown))}。"
                       f"涉及安全的项请改 .env 并重启。",
            )
        for key, value in payload.items():
            state.store.set_setting(key, value, actor=actor)
        state.store.log_admin(actor=actor, action="update_settings", target="",
                              detail="、".join(sorted(payload)))
        return {"data": state.runtime_settings()}

    # ------------------------------------------------------------------
    # 飞书组织
    # ------------------------------------------------------------------
    @router.post("/org/sync")
    async def sync_org(request: Request, actor: str = Depends(require_admin)):
        state = request.app.state.hil2
        service = OrgSyncService(client=state.client, store=state.store)
        try:
            result = await run_in_threadpool(service.sync, actor=actor)
        except OrgSyncError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"data": result.as_dict()}

    @router.get("/org/check")
    async def check_org(request: Request, actor: str = Depends(require_admin)):
        state = request.app.state.hil2
        service = OrgSyncService(client=state.client, store=state.store)
        return {"data": await run_in_threadpool(service.check)}

    @router.get("/org/users")
    async def list_org_users(request: Request, q: str = "", admins_only: bool = False,
                             actor: str = Depends(require_admin)):
        state = request.app.state.hil2
        users = state.store.search_org_users(q, admins_only=admins_only)
        return {"data": [user.as_dict(include_contact=True) for user in users]}

    @router.put("/org/users/{open_id}/admin")
    async def set_admin(open_id: str, request: Request, actor: str = Depends(require_admin)):
        state = request.app.state.hil2
        payload = await request.json()
        is_admin = bool(payload.get("is_admin"))
        # 不允许把最后一个管理员摘掉，否则没人能进后台了
        if not is_admin:
            remaining = [x for x in state.store.admin_open_ids() if x != open_id]
            if not remaining and not state.settings.feishu_admin_open_ids:
                raise HTTPException(status_code=400, detail="至少要保留一名管理员")
        if not await run_in_threadpool(state.store.set_admin, open_id, is_admin, actor=actor):
            raise HTTPException(status_code=404, detail="通讯录里没有这个人，请先同步组织架构")
        return {"data": {"open_id": open_id, "is_admin": is_admin}}

    return router
