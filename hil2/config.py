"""集中配置。所有敏感值只从环境变量读取，禁止硬编码生产值。

交接文件第 12 条：不得把 Webhook、App Secret、Token 写进代码仓库或日志。
本模块提供 ``redacted()`` 用于安全打印。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 服务 ----------
    host: str = "127.0.0.1"
    port: int = 8848
    log_level: str = "INFO"
    timezone: str = "Asia/Shanghai"
    db_path: Path = REPO_ROOT / "var" / "hil2.db"

    # 集群脚本直连协调服务时使用的共享密钥。
    # 交接文件第 7 条关键兼容方案：bot-to-bot 不可靠时走这条直连通道。
    ingest_token: str = ""

    # ---------- 判定阈值 ----------
    # 与集群侧采集脚本保持一致；改这里不改变引擎语义，只改变触发时机。
    idle_minutes: int = 15
    remind_after_minutes: int = 10
    escalate_after_minutes: int = 20
    # 采集数据超过该时长即视为过期，大盘标注「可能过期」，且不得据此宣告可用。
    stale_after_minutes: int = 20

    # ---------- 纳管范围 ----------
    # 52/58/60 是虚构演示集群编号；部署前请按实际环境修改。
    # NoDecode：环境变量里写逗号分隔即可（MANAGED_CLUSTERS=52,58,60），
    # 不必写成 JSON 数组；解析交给下面的 _split_csv。
    managed_clusters: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["52", "58", "60"]
    )

    # ---------- 飞书应用 ----------
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_api_base_url: str = "https://open.feishu.cn/open-apis"
    # 事件订阅安全设置；两者都留空时回调只接受本地调试来源。
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    # JSAPI 免登签名用随机串。
    feishu_nonce_str: str = "hil2_default_nonce"
    # 飞书事件入站。长连接是默认生产方案，只需服务器出站访问飞书；
    # http_callback 保留给已有公网反代的部署，off 只用于纯 Web/离线演示。
    feishu_event_mode: Literal["long_connection", "http_callback", "off"] = "long_connection"
    feishu_channel_connect_timeout_seconds: int = 20
    feishu_channel_security_mode: Literal["audit", "strict"] = "strict"
    # Webhook 自定义机器人消息不会触发应用机器人事件；内网系统通过 API
    # 轮询监控群补齐入站链路。设为 0 可关闭（例如只用 /api/ingest）。
    feishu_monitor_poll_seconds: int = 20

    # ---------- 群与人 ----------
    # 独立监控群：只接 webhook 审计副本，不打扰业务大群。
    feishu_monitor_chat_id: str = ""
    # 业务大群：只出现必须的人机交互。
    feishu_business_chat_id: str = ""
    # 业务机器人自身 open_id，用于 loop guard（防止消费自己发出的消息）。
    feishu_bot_open_id: str = ""
    # 管理员 open_id 列表，采集异常与超时升级时 @ 他们。
    feishu_admin_open_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ---------- 预约数据源 ----------
    # booking: 用户在网页/飞书里自助预约（默认）
    # file:    管理员维护的临时队列 JSON
    # http:    正式预约 API
    # 三种模式最终都产出同一个 Reservation，判定引擎不受影响。
    reservation_source: Literal["booking", "file", "http"] = "booking"
    reservation_file: Path = REPO_ROOT / "var" / "reservations.json"
    reservation_api_url: str = ""
    reservation_api_token: str = ""

    # ---------- 预约规则 ----------
    booking_slot_minutes: int = 30      # 时段粒度，固定半小时
    booking_max_hours: int = 12         # 单次最长
    booking_horizon_days: int = 14      # 最远可约到几天后
    booking_reminder_minutes: int = 10  # 到点前多久提醒下一位
    # 每日开放时段按业务所在地时区计算；24 表示次日 00:00。
    booking_day_start_hour: int = 9
    booking_day_end_hour: int = 24

    # ---------- Web / SSO ----------
    jwt_secret: str = ""
    jwt_ttl_hours: int = 12
    # 开启后预约和管理后台需要飞书登录。大盘本身默认仍然免登可看——
    # 它是挂在墙上给所有人扫一眼的，要求登录才能看状态会毁掉这个用途。
    sso_enabled: bool = False
    # 确实需要连看都管起来时再打开（例如服务暴露在了不可信网络里）。
    require_login_to_view: bool = False
    # 浏览器 OAuth 回调地址，必须与飞书后台配置完全一致。
    sso_redirect_uri: str = ""
    dashboard_refresh_seconds: int = 60

    # ---------- 安全边界（只读常量，禁止通过环境变量放开）----------
    @property
    def never_auto_release(self) -> bool:
        """永不因超时或任何自动条件释放集群。交接文件第 12 条。"""
        return True

    @field_validator("managed_clusters", "feishu_admin_open_ids", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def feishu_configured(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret)

    def redacted(self) -> dict[str, object]:
        """可安全写入日志的配置快照。"""
        secret_fields = {
            "feishu_app_secret",
            "feishu_encrypt_key",
            "feishu_verification_token",
            "reservation_api_token",
            "jwt_secret",
            "ingest_token",
        }
        out: dict[str, object] = {}
        # pydantic 2.11 起实例上访问 model_fields 已废弃，走类属性。
        for name in type(self).model_fields:
            value = getattr(self, name)
            if name in secret_fields:
                out[name] = "***已配置***" if value else "***未配置***"
            elif isinstance(value, Path):
                out[name] = str(value)
            else:
                out[name] = value
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：改动环境变量后重新读取配置。"""
    get_settings.cache_clear()
