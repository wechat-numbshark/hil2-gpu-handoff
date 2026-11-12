"""FastAPI 应用。

入站通道
--------
* ``POST /api/ingest``            集群脚本直连（主通道，Bearer ingest_token）
* Channel SDK 长连接           默认飞书事件入站（无需公网回调地址）
* ``POST /api/feishu/callback``   可选 HTTP 事件订阅 + 卡片回调

交接文件第 7 节的关键兼容方案：两条通道投递同一份 JSON，靠 ``event_id`` 幂等去重，
任意一条可用系统就能正常工作。
"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import admin as admin_routes
from . import dashboard as dashboard_view
from .booking import BookingError, align_down, align_up
from .booking_service import BookingService
from .config import Settings, get_settings
from .coordinator import Coordinator
from .feishu import callback as feishu_callback
from .feishu.client import FeishuClient
from .feishu.channel import FeishuLongConnection
from .feishu.monitor_relay import FeishuMonitorRelay
from .feishu.sso import SsoError, SsoService
from .models import EventParseError, MonitorEvent, parse_datetime
from .puller import SshPuller
from .registry import ClusterConfig, CollectMode
from .reservations import ReservationSourceError, build_source
from .scheduler import PeriodicWorker
from .store import Store

logger = logging.getLogger(__name__)

# 让 _booking_identity 能取到 AppState 而不必层层传参
request_state: ContextVar["AppState | None"] = ContextVar("request_state", default=None)

WEB_DIR = Path(__file__).parent / "web"
TIMEOUT_SCAN_SECONDS = 30.0
SSH_PULL_TICK_SECONDS = 20.0
RESERVATION_SYNC_SECONDS = 60.0
BOOKING_REMINDER_SECONDS = 60.0


class AppState:
    """进程内单例集合，挂在 app.state 上，测试可整体替换。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.db_path)
        self.client = FeishuClient(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret,
            base_url=settings.feishu_api_base_url,
        )
        self.sso = SsoService(
            self.client,
            jwt_secret=settings.jwt_secret,
            jwt_ttl_hours=settings.jwt_ttl_hours,
            nonce_str=settings.feishu_nonce_str,
            redirect_uri=settings.sso_redirect_uri,
        )
        # 集群注册表是纳管范围的唯一真相；首次启动时从 .env 引导一次
        self._bootstrap_clusters()
        self.booking = BookingService(settings=settings, store=self.store)
        self.puller = SshPuller(store=self.store, ingest=lambda e: self.coordinator.ingest(e))
        self.coordinator = Coordinator(
            settings=settings,
            store=self.store,
            client=self.client,
            reservation_source=build_source_safe(settings),
        )
        self.monitor_relay = FeishuMonitorRelay(
            client=self.client,
            chat_id=settings.feishu_monitor_chat_id,
            ingest=lambda event: self.coordinator.ingest(event),
        )
        self.feishu_channel: FeishuLongConnection | None = None
        self.timeout_worker = PeriodicWorker(
            name="card-timeout-scan",
            interval_seconds=TIMEOUT_SCAN_SECONDS,
            job=self.coordinator.scan_timeouts,
        )
        self.reservation_worker = PeriodicWorker(
            name="reservation-sync",
            interval_seconds=RESERVATION_SYNC_SECONDS,
            job=lambda: self.coordinator.sync_reservations(force=True),
        )
        self.pull_worker = PeriodicWorker(
            name="ssh-pull",
            interval_seconds=SSH_PULL_TICK_SECONDS,
            job=self.puller.run_once,
        )
        self.monitor_relay_worker = PeriodicWorker(
            name="feishu-monitor-relay",
            interval_seconds=max(5, settings.feishu_monitor_poll_seconds),
            job=self.monitor_relay.run_once,
        )
        self.booking_worker = PeriodicWorker(
            name="booking-reminder",
            interval_seconds=BOOKING_REMINDER_SECONDS,
            job=self._send_booking_reminders,
        )

    def _send_booking_reminders(self) -> None:
        """预约即将开始时提醒下一位。只发消息，不动机器。"""
        for item in self.booking.upcoming_reminders():
            booking = item["booking"]
            chat_id = self.settings.feishu_business_chat_id
            if not (self.client.configured and chat_id):
                logger.info("预约提醒（未配飞书，仅记审计）：%s %s", booking.cluster, booking.id)
                message_id = ""
            else:
                try:
                    message_id = self.client.send_card(chat_id=chat_id, card=item["card"])
                except Exception:
                    logger.exception("预约提醒发送失败：%s", booking.id)
                    message_id = ""
            self.store.log_notification(
                cluster=booking.cluster, kind="booking_reminder",
                target_open_id=booking.open_id, chat_id=chat_id, message_id=message_id,
                note="预约即将开始", delivered=bool(message_id),
            )

    def admin_open_ids(self) -> set[str]:
        return set(self.settings.feishu_admin_open_ids) | set(self.store.admin_open_ids())

    def _bootstrap_clusters(self) -> None:
        """首次启动把 .env 里的 MANAGED_CLUSTERS 写进注册表，之后以注册表为准。"""
        if self.store.list_clusters():
            return
        for cluster in self.settings.managed_clusters:
            try:
                self.store.save_cluster(
                    ClusterConfig(cluster=cluster, data_dir="/data1"), actor="bootstrap"
                )
            except Exception:
                logger.exception("引导集群 %s 失败", cluster)
        if self.settings.managed_clusters:
            logger.info("已从环境变量引导 %s 台集群到注册表，后续请在管理后台维护",
                        len(self.settings.managed_clusters))

    def runtime_settings(self) -> dict[str, Any]:
        """后台可改的配置。库里有值就用库里的，否则回落到 .env。"""
        stored = self.store.all_settings()
        defaults = {
            "feishu_monitor_chat_id": self.settings.feishu_monitor_chat_id,
            "feishu_business_chat_id": self.settings.feishu_business_chat_id,
            "feishu_bot_open_id": self.settings.feishu_bot_open_id,
            "coordinator_public_url": "",
            "dashboard_refresh_seconds": self.settings.dashboard_refresh_seconds,
            "stale_after_minutes": self.settings.stale_after_minutes,
            "remind_after_minutes": self.settings.remind_after_minutes,
            "escalate_after_minutes": self.settings.escalate_after_minutes,
            "booking_max_hours": self.settings.booking_max_hours,
            "booking_horizon_days": self.settings.booking_horizon_days,
            "booking_reminder_minutes": self.settings.booking_reminder_minutes,
            "booking_day_start_hour": self.settings.booking_day_start_hour,
            "booking_day_end_hour": self.settings.booking_day_end_hour,
        }
        return {key: stored.get(key, default) for key, default in defaults.items()}

    def effective_settings(self) -> Settings:
        """把后台改过的值叠加到 Settings 上，供判定和展示使用。"""
        runtime = self.runtime_settings()
        managed = [c.cluster for c in self.store.list_clusters(only_enabled=True) if c.managed]
        overrides: dict[str, Any] = {
            key: value for key, value in runtime.items()
            if value not in (None, "") and key in type(self.settings).model_fields
        }
        if managed:
            overrides["managed_clusters"] = managed
        return self.settings.model_copy(update=overrides) if overrides else self.settings

    def shutdown(self) -> None:
        # 任何一步失败都不能让后面的资源泄漏，逐个兜底。
        for step in (
            self.timeout_worker.stop,
            self.reservation_worker.stop,
            self.booking_worker.stop,
            self.pull_worker.stop,
            self.monitor_relay_worker.stop,
            self.client.close,
            self.store.close,
        ):
            try:
                step()
            except Exception:
                logger.warning("关闭步骤失败：%s", getattr(step, "__qualname__", step), exc_info=True)


def build_source_safe(settings: Settings):
    """预约数据源构造失败时不阻止服务启动，改为在运行期持续报未恢复事件。"""
    try:
        return build_source(settings)
    except ReservationSourceError as exc:
        logger.error("预约数据源配置错误：%s", exc)

        class _Broken:
            describe = "misconfigured"

            def fetch(self):
                raise ReservationSourceError(str(exc))

        return _Broken()


def get_state(request: Request) -> AppState:
    return request.app.state.hil2


# ----------------------------------------------------------------------
# 鉴权
# ----------------------------------------------------------------------
def require_ingest_token(
    state: AppState = Depends(get_state),
    authorization: str = Header(default=""),
) -> None:
    expected = state.settings.ingest_token
    if not expected:
        # 未配置共享密钥时只接受本地来源，避免误开公网写入口。
        return
    provided = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="ingest token 无效")


def current_user(
    state: AppState = Depends(get_state),
    authorization: str = Header(default=""),
) -> dict[str, Any] | None:
    """写操作用：没登录直接 401。"""
    if not state.settings.sso_enabled:
        return None
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        return state.sso.verify(token)
    except SsoError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def optional_user(
    state: AppState = Depends(get_state),
    authorization: str = Header(default=""),
) -> dict[str, Any] | None:
    """只读用：登录了就认人，没登录也放行。

    大盘要挂在墙上给所有人看，要求登录才能看状态会毁掉这个用途。
    确需管起来的场景把 REQUIRE_LOGIN_TO_VIEW 打开即可。
    """
    if not state.settings.sso_enabled:
        return None
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token:
        if state.settings.require_login_to_view:
            raise HTTPException(status_code=401, detail="需要登录后查看")
        return None
    try:
        return state.sso.verify(token)
    except SsoError:
        if state.settings.require_login_to_view:
            raise HTTPException(status_code=401, detail="登录已过期，请重新登录") from None
        return None


# ----------------------------------------------------------------------
# 应用
# ----------------------------------------------------------------------
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = AppState(settings)
        app.state.hil2 = state
        logger.info("HIL2.0 协调服务启动，配置：%s", state.settings.redacted())
        state.coordinator.sync_reservations(force=True)
        state.timeout_worker.start()
        state.reservation_worker.start()
        state.booking_worker.start()
        state.pull_worker.start()
        if settings.feishu_monitor_poll_seconds > 0:
            state.monitor_relay_worker.start()
        if settings.feishu_event_mode == "long_connection":
            async def dispatch_ws(envelope):
                return await run_in_threadpool(_dispatch_feishu_envelope, state, envelope)

            state.feishu_channel = FeishuLongConnection(
                app_id=settings.feishu_app_id,
                app_secret=settings.feishu_app_secret,
                api_base_url=settings.feishu_api_base_url,
                monitor_chat_id=settings.feishu_monitor_chat_id,
                business_chat_id=settings.feishu_business_chat_id,
                admin_open_ids=settings.feishu_admin_open_ids,
                security_mode=settings.feishu_channel_security_mode,
                connect_timeout_seconds=settings.feishu_channel_connect_timeout_seconds,
                on_envelope=dispatch_ws,
            )
            await state.feishu_channel.start()
        try:
            yield
        finally:
            if state.feishu_channel is not None:
                await state.feishu_channel.stop()
            state.shutdown()

    app = FastAPI(
        title="HIL2.0 集群闲忙监控与飞书交接",
        version="1.0.0",
        description="只读监控；采集异常绝不判空闲；绝不自动释放集群。",
        lifespan=lifespan,
    )

    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    # ---------------- 大盘 ----------------
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        state: AppState = request.app.state.hil2
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "refresh_seconds": state.settings.dashboard_refresh_seconds,
                "sso_enabled": state.settings.sso_enabled,
                "managed_clusters": state.settings.managed_clusters,
                "booking_day_start_hour": state.settings.booking_day_start_hour,
                "booking_day_end_hour": state.settings.booking_day_end_hour,
            },
        )

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page(request: Request):
        state: AppState = request.app.state.hil2
        return templates.TemplateResponse(
            request, "admin.html", {"sso_enabled": state.settings.sso_enabled}
        )

    @app.get("/api/health")
    async def health(request: Request):
        state: AppState = request.app.state.hil2
        channel_status = (
            state.feishu_channel.status
            if state.feishu_channel is not None
            else {
                "mode": state.settings.feishu_event_mode,
                "configured": state.client.configured,
                "ready": state.settings.feishu_event_mode == "http_callback",
                "connection": None,
                "last_error": "",
            }
        )
        return {
            "status": "ok",
            "feishu_configured": state.client.configured,
            "feishu_events": channel_status,
            "feishu_monitor_relay": state.monitor_relay.status,
            "reservation_source": state.coordinator.reservation_source.describe,
            "managed_clusters": state.effective_settings().managed_clusters,
            "collect_modes": {
                mode.value: sum(
                    1 for c in state.store.list_clusters(only_enabled=True) if c.mode is mode
                )
                for mode in CollectMode
            },
            "pending_cards": len(state.store.pending_cards()),
            "open_incidents": len(state.store.open_incidents()),
            "safety": {"read_only": True, "never_auto_release": True},
        }

    @app.get("/api/dashboard")
    async def dashboard(request: Request, _user=Depends(optional_user)):
        state: AppState = request.app.state.hil2
        return await run_in_threadpool(
            dashboard_view.build, state.store, state.effective_settings()
        )

    @app.get("/api/clusters/{cluster}/events")
    async def cluster_events(cluster: str, request: Request, _user=Depends(optional_user)):
        state: AppState = request.app.state.hil2
        events = state.store.recent_events(cluster=cluster, limit=50)
        return {
            "cluster": cluster,
            "events": [
                {
                    **event,
                    "observed_at": event["observed_at"].isoformat() if event["observed_at"] else None,
                    "received_at": event["received_at"].isoformat() if event["received_at"] else None,
                }
                for event in events
            ],
        }

    # ---------------- 集群脚本直连 ----------------
    @app.post("/api/ingest")
    async def ingest(request: Request, _: None = Depends(require_ingest_token)):
        state: AppState = request.app.state.hil2
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
        try:
            event = MonitorEvent.from_dict(payload, source="direct")
        except EventParseError as exc:
            # 解析失败绝不静默：明确 400，让集群脚本重试或报警。
            raise HTTPException(status_code=400, detail=f"事件无法解析：{exc}") from exc
        return await run_in_threadpool(state.coordinator.ingest, event)

    # ---------------- 飞书回调 ----------------
    @app.post("/api/feishu/callback")
    async def feishu_callback_endpoint(
        request: Request,
        x_lark_request_timestamp: str = Header(default=""),
        x_lark_request_nonce: str = Header(default=""),
        x_lark_signature: str = Header(default=""),
    ):
        state: AppState = request.app.state.hil2
        settings = state.settings
        raw = await request.body()

        # 配了 encrypt_key 就必须验签通过；没配则只接受未加密的本地调试来源。
        if settings.feishu_encrypt_key and x_lark_signature:
            if not feishu_callback.verify_signature(
                timestamp=x_lark_request_timestamp,
                nonce=x_lark_request_nonce,
                encrypt_key=settings.feishu_encrypt_key,
                body=raw,
                signature=x_lark_signature,
            ):
                raise HTTPException(status_code=401, detail="签名校验失败")

        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc

        try:
            plain = feishu_callback.unwrap(
                payload,
                encrypt_key=settings.feishu_encrypt_key,
                verification_token=settings.feishu_verification_token,
            )
            envelope = feishu_callback.parse(plain)
        except feishu_callback.CallbackError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        if envelope.kind == "url_verification":
            return {"challenge": envelope.challenge}

        return await run_in_threadpool(_dispatch_feishu_envelope, state, envelope)

    # ---------------- 飞书 SSO ----------------
    @app.get("/api/auth/feishu-auth-params")
    async def feishu_auth_params(url: str, request: Request):
        state: AppState = request.app.state.hil2
        if not url:
            raise HTTPException(status_code=400, detail="URL 参数不能为空")
        try:
            return {"data": await run_in_threadpool(state.sso.jsapi_auth_params, url)}
        except Exception as exc:
            logger.warning("获取 JSAPI 鉴权参数失败：%s", exc)
            raise HTTPException(status_code=500, detail="获取鉴权参数失败") from exc

    @app.get("/api/auth/feishu-oauth-url")
    async def feishu_oauth_url(request: Request, state_param: str = ""):
        state: AppState = request.app.state.hil2
        try:
            return {"data": {"url": state.sso.oauth_url(state=state_param)}}
        except SsoError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/auth/feishu-login")
    async def feishu_login(request: Request):
        state: AppState = request.app.state.hil2
        payload = await request.json()
        code = str(payload.get("code", ""))
        redirect_uri = payload.get("redirect_uri") or None
        try:
            return {"data": await run_in_threadpool(state.sso.login, code, redirect_uri)}
        except SsoError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.get("/api/auth/session")
    async def session(request: Request, authorization: str = Header(default="")):
        """当前登录态。前端据此决定显示登录按钮还是用户头像。"""
        state: AppState = request.app.state.hil2
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        base = {
            "sso_enabled": state.settings.sso_enabled,
            "in_feishu_hint": "在飞书里打开可以一键免登",
            "authenticated": False,
            "user": None,
            "is_admin": False,
            "role": "guest",
        }
        if not state.settings.sso_enabled:
            # 免登模式：内网只读部署，身份由用户自报，后台仅限本机
            return {
                **base, "authenticated": True, "user": None,
                "open_mode": True, "role": "local_user",
            }
        if not token:
            return base
        try:
            claims = state.sso.verify(token)
        except SsoError:
            return base

        open_id = str(claims.get("sub", ""))
        profile = state.store.get_org_user(open_id)
        admins = state.admin_open_ids()
        return {
            **base,
            "authenticated": True,
            "open_mode": False,
            "user": {
                "open_id": open_id,
                "name": (profile.name if profile else "") or str(claims.get("name", "")),
                "avatar_url": (profile.avatar_url if profile else "") or str(claims.get("avatar", "")),
                "departments": list(profile.department_names) if profile else [],
            },
            "is_admin": open_id in admins,
            "role": "admin" if open_id in admins else "user",
        }

    @app.get("/auth/callback", response_class=HTMLResponse)
    async def auth_callback(request: Request):
        """浏览器 OAuth 回跳落地页。换令牌在前端做，避免 token 出现在服务端日志里。"""
        return templates.TemplateResponse(request, "callback.html", {})

    @app.get("/api/auth/me")
    async def me(user=Depends(current_user)):
        return {"data": user or {"sub": "anonymous", "name": "免登模式"}}

    # ---------------- 预约 ----------------
    def _booking_identity(user: dict[str, Any] | None, payload: dict[str, Any]) -> tuple[str, str]:
        """确定操作人。开了 SSO 就以登录身份为准，绝不信前端传来的 open_id。"""
        if user:
            open_id = str(user.get("sub", ""))
            state: AppState = request_state.get()
            # 优先用通讯录里的真实姓名，而不是令牌里可能过时的那份
            profile = state.store.get_org_user(open_id) if state else None
            name = (profile.name if profile else "") or str(user.get("name", ""))
            return open_id, name
        # 免登模式（内网只读部署）下允许前端自报身份，仅适用于可信内网
        return str(payload.get("open_id", "")), str(payload.get("name", ""))

    @app.get("/api/bookings")
    async def list_bookings(
        request: Request,
        cluster: str = "",
        days: int = 3,
        mine: bool = False,
        user=Depends(optional_user),
    ):
        state: AppState = request.app.state.hil2
        now = datetime.now(timezone.utc)
        days = max(1, min(days, state.settings.booking_horizon_days))
        # 时间轴从当天 00:00 起，避免半截开头
        zone = ZoneInfo(state.settings.timezone)
        window_start = datetime.now(zone).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)
        window_end = window_start + timedelta(days=days)

        if state.settings.sso_enabled:
            # 开启 SSO 后只相信令牌，不能靠查询参数冒充管理员。
            open_id = str(user.get("sub", "")) if user else ""
        else:
            open_id = request.query_params.get("open_id", "")
        viewer_is_admin = open_id in state.admin_open_ids()
        bookings = await run_in_threadpool(
            state.store.list_bookings,
            cluster=cluster or None,
            open_id=open_id if mine else None,
            since=window_start,
            until=window_end,
        )
        return {
            "now": now.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "slot_minutes": state.settings.booking_slot_minutes,
            "timezone": state.settings.timezone,
            "clusters": state.settings.managed_clusters,
            "max_hours": state.settings.booking_max_hours,
            "horizon_days": state.settings.booking_horizon_days,
            "day_start_hour": state.settings.booking_day_start_hour,
            "day_end_hour": state.settings.booking_day_end_hour,
            "viewer": {
                "open_id": open_id,
                "is_admin": viewer_is_admin,
                "role": "admin" if viewer_is_admin else "user",
            },
            "bookings": [
                {
                    **item.as_dict(now=now, tz=state.settings.timezone),
                    "is_owner": bool(open_id) and item.open_id == open_id,
                    "can_cancel": bool(open_id) and (
                        item.open_id == open_id or viewer_is_admin
                    ),
                }
                for item in bookings
            ],
        }

    @app.post("/api/bookings")
    async def create_booking(request: Request, user=Depends(current_user)):
        state: AppState = request.app.state.hil2
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc

        request_state.set(state)
        open_id, name = _booking_identity(user, payload)
        if not open_id:
            raise HTTPException(status_code=401, detail="需要先登录才能预约")
        try:
            start_at = parse_datetime(payload.get("start_at"))
            end_at = parse_datetime(payload.get("end_at"))
        except EventParseError as exc:
            raise HTTPException(status_code=400, detail=f"时间格式不对：{exc}") from exc

        try:
            booking = await run_in_threadpool(
                state.booking.book,
                cluster=str(payload.get("cluster", "")),
                open_id=open_id,
                name=name or str(payload.get("name", "")),
                start_at=start_at,
                end_at=end_at,
                note=str(payload.get("note", "")),
            )
        except BookingError as exc:
            # 冲突和越界都是用户能自己纠正的问题，用 409 而不是 500
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"data": booking.as_dict(tz=state.settings.timezone)}

    @app.delete("/api/bookings/{booking_id}")
    async def cancel_booking(booking_id: str, request: Request, user=Depends(current_user)):
        state: AppState = request.app.state.hil2
        payload: dict[str, Any] = {}
        try:
            payload = await request.json()
        except Exception:
            pass
        request_state.set(state)
        open_id, _ = _booking_identity(user, payload)
        if not open_id:
            raise HTTPException(status_code=401, detail="需要先登录才能取消")
        try:
            booking = await run_in_threadpool(
                state.booking.cancel,
                booking_id,
                open_id=open_id,
                is_admin=open_id in state.admin_open_ids(),
            )
        except BookingError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"data": booking.as_dict(tz=state.settings.timezone)}

    # ---------------- 管理 ----------------
    @app.get("/api/admin/reservations")
    async def list_reservations(request: Request, _user=Depends(optional_user)):
        state: AppState = request.app.state.hil2
        return {
            "source": state.coordinator.reservation_source.describe,
            "data": [item.as_dict() for item in state.store.all_reservations()],
        }

    @app.post("/api/admin/reservations/sync")
    async def sync_reservations(request: Request, _user=Depends(current_user)):
        state: AppState = request.app.state.hil2
        ok = await run_in_threadpool(state.coordinator.sync_reservations, force=True)
        return {"synced": ok, "source": state.coordinator.reservation_source.describe}

    app.include_router(admin_routes.build_router(get_state, current_user))

    @app.exception_handler(EventParseError)
    async def _event_parse_handler(_: Request, exc: EventParseError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return app


# ----------------------------------------------------------------------
# 回调处理（同步，运行在线程池里）
# ----------------------------------------------------------------------
def _dispatch_feishu_envelope(state: AppState, envelope) -> dict[str, Any]:
    """HTTP 回调与长连接共用的唯一业务分发入口。"""
    if (
        envelope.sender_open_id
        and envelope.sender_open_id == state.settings.feishu_bot_open_id
    ):
        return {"ignored": "loop_guard"}
    if envelope.kind == "message":
        return _handle_monitor_message(state, envelope)
    if envelope.kind == "card_action":
        return _handle_card_action(state, envelope)
    return {"ignored": envelope.event_type or "unsupported"}


def _handle_monitor_message(state: AppState, envelope) -> dict[str, Any]:
    settings = state.settings

    # 业务大群：@机器人 的自助预约指令
    if settings.feishu_business_chat_id and envelope.chat_id == settings.feishu_business_chat_id:
        return _handle_booking_command(state, envelope)

    # 监控群：结构化监控事件。其它群的消息一律忽略。
    if settings.feishu_monitor_chat_id and envelope.chat_id != settings.feishu_monitor_chat_id:
        return {"ignored": "not_monitor_chat"}

    payload = feishu_callback.extract_monitor_payload(envelope.text)
    if payload is None:
        return {"ignored": "no_event_json"}

    try:
        event = MonitorEvent.from_dict(payload, source="feishu_monitor_chat")
    except EventParseError as exc:
        logger.warning("监控群事件解析失败：%s", exc)
        state.store.open_incident(
            cluster=str(payload.get("cluster", "-")) or "-",
            kind="malformed_event",
            detail=str(exc),
        )
        return {"ignored": "unparsable", "detail": str(exc)}

    return state.coordinator.ingest(event)


def _handle_card_action(state: AppState, envelope) -> dict[str, Any]:
    # 飞书会重试卡片回调。重试必须回放首次结果，而不是报「已处理过」。
    event_id = envelope.event_id
    if event_id and not state.store.claim_callback(event_id):
        cached = state.store.get_callback_result(event_id)
        if cached:
            return {**cached, "replayed": True}
        return {"toast": {"type": "info", "content": "该操作正在处理中"}, "replayed": True}

    result = state.coordinator.handle_card_click(
        action_value=envelope.card_action_value,
        operator_open_id=envelope.operator_open_id,
    )
    # 注意顺序：result 里也有一个字符串 toast，结构化 toast 必须放在后面，
    # 否则会被覆盖成裸字符串，飞书就弹不出提示。
    response = {
        **result,
        "toast": {
            "type": "success" if result["accepted"] else "error",
            "content": result["toast"],
        },
    }
    if event_id:
        state.store.save_callback_result(event_id, response)
        state.store.log_notification(
            cluster=str(envelope.card_action_value.get("cluster", "-")) or "-",
            kind="card_callback",
            target_open_id=envelope.operator_open_id,
            note=f"event_id={event_id}",
            delivered=True,
        )
    return response


def _handle_booking_command(state: AppState, envelope) -> dict[str, Any]:
    """业务大群里 @机器人 的预约指令。认不出来就安静，不在群里刷屏。"""
    # 同一条消息飞书可能重投，按 event_id 幂等
    if envelope.event_id and not state.store.claim_callback(f"msg:{envelope.event_id}"):
        return {"ignored": "duplicate_message"}

    card = state.booking.handle_message(
        text=envelope.text,
        open_id=envelope.sender_open_id,
        name="",
    )
    if card is None:
        return {"ignored": "not_a_command"}

    chat_id = state.settings.feishu_business_chat_id
    message_id = ""
    if state.client.configured and chat_id:
        try:
            message_id = state.client.send_card(chat_id=chat_id, card=card)
        except Exception:
            logger.exception("预约指令回复失败")
    else:
        logger.info("预约指令已处理（未配飞书，仅本地）：%s", card["header"]["title"]["content"])

    return {
        "handled": "booking_command",
        "title": card["header"]["title"]["content"],
        "message_id": message_id,
    }
