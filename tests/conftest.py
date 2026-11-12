"""共用测试夹具。

所有测试都在临时目录里跑，绝不碰真实数据库、真实飞书、真实集群。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hil2.config import Settings  # noqa: E402
from hil2.coordinator import Coordinator  # noqa: E402
from hil2.models import MonitorEvent, Reservation  # noqa: E402
from hil2.reservations import ReservationSource, ReservationSourceError  # noqa: E402
from hil2.store import Store  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def utc(**delta: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**delta)


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------
class FakeFeishuClient:
    """记录所有外发消息的假客户端。默认全部成功。"""

    def __init__(self, *, configured: bool = True, fail: bool = False) -> None:
        self.configured = configured
        self.fail = fail
        self.sent_cards: list[dict[str, Any]] = []
        self.sent_texts: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self._seq = 0

    def send_card(self, *, chat_id: str, card: dict[str, Any]) -> str:
        if self.fail:
            from hil2.feishu.client import FeishuError

            raise FeishuError("测试注入的发送失败")
        self._seq += 1
        message_id = f"om_test_{self._seq}"
        self.sent_cards.append({"chat_id": chat_id, "card": card, "message_id": message_id})
        return message_id

    def send_text(self, *, chat_id: str, text: str) -> str:
        self._seq += 1
        self.sent_texts.append({"chat_id": chat_id, "text": text})
        return f"om_test_{self._seq}"

    def update_card(self, *, message_id: str, card: dict[str, Any]) -> None:
        self.updated.append({"message_id": message_id, "card": card})

    def close(self) -> None:
        self.closed = True

    # 便于断言的辅助
    def card_titles(self) -> list[str]:
        return [item["card"]["header"]["title"]["content"] for item in self.sent_cards]

    def cards_to(self, chat_id: str) -> list[dict[str, Any]]:
        return [item for item in self.sent_cards if item["chat_id"] == chat_id]


class StaticReservationSource(ReservationSource):
    def __init__(self, reservations: list[Reservation] | None = None) -> None:
        self.reservations = reservations or []
        self.should_fail = False
        self.fetch_count = 0

    @property
    def describe(self) -> str:
        return "static"

    def fetch(self) -> list[Reservation]:
        self.fetch_count += 1
        if self.should_fail:
            raise ReservationSourceError("测试注入的数据源故障")
        return list(self.reservations)


# ----------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------
@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """测试用配置。

    ``_env_file=None`` 是关键：不加它时 pydantic-settings 会读开发者本地的
    ``.env``，测试结果就取决于谁的机器上有什么文件——在打包给客户的干净副本里
    会突然大批失败。每一项都在这里写死，测试与环境彻底无关。
    """
    return Settings(
        _env_file=None,
        reservation_source="file",
        db_path=tmp_path / "test.db",
        reservation_file=tmp_path / "reservations.json",
        managed_clusters=["52", "58", "60"],
        feishu_monitor_chat_id="oc_monitor",
        feishu_business_chat_id="oc_business",
        feishu_bot_open_id="ou_bot",
        feishu_admin_open_ids=["ou_admin"],
        feishu_app_id="cli_test",
        feishu_app_secret="secret_test",
        feishu_verification_token="vtoken",
        feishu_encrypt_key="ekey",
        feishu_event_mode="http_callback",
        feishu_monitor_poll_seconds=0,
        jwt_secret="jwt_test_secret",
        ingest_token="ingest_test_token",
        idle_minutes=15,
        remind_after_minutes=10,
        escalate_after_minutes=20,
        stale_after_minutes=20,
        # 通用测试可以在任意时刻运行；每日 9–24 点规则用固定时钟单独覆盖。
        booking_day_start_hour=0,
        booking_day_end_hour=24,
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    instance = Store(tmp_path / "store.db")
    yield instance
    instance.close()


@pytest.fixture
def client() -> FakeFeishuClient:
    return FakeFeishuClient()


@pytest.fixture
def source() -> StaticReservationSource:
    return StaticReservationSource()


@pytest.fixture
def coordinator(settings, store, client, source) -> Coordinator:
    return Coordinator(
        settings=settings, store=store, client=client, reservation_source=source
    )


@pytest.fixture
def agent_module():
    """把独立的采集脚本当模块加载，测试其五态判定。"""
    path = REPO_ROOT / "agent" / "hil_agent.py"
    spec = importlib.util.spec_from_file_location("hil_agent_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------
# 构造器
# ----------------------------------------------------------------------
def make_event(
    cluster: str = "52",
    state: str = "busy",
    *,
    event_id: str | None = None,
    observed_at: datetime | None = None,
    reasons: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
    suspected_idle_minutes: int = 0,
    facts: dict[str, Any] | None = None,
    source: str = "direct",
) -> MonitorEvent:
    observed = observed_at or datetime.now(timezone.utc)
    return MonitorEvent.from_dict(
        {
            "schema": "hil.monitor.v1",
            "event_id": event_id or f"{cluster}-{observed.timestamp()}-{state}",
            "cluster": cluster,
            "state": state,
            "observed_at": observed.isoformat(),
            "reasons": list(reasons),
            "warnings": list(warnings),
            "suspected_idle_minutes": suspected_idle_minutes,
            "facts": facts or {},
        },
        source=source,
    )


def make_reservation(
    cluster: str = "52",
    *,
    current: str | None = "ou_current",
    nxt: str | None = "ou_next",
    handoff_minutes: float | None = None,
    reserved_minutes: float | None = None,
) -> Reservation:
    return Reservation(
        cluster=cluster,
        current_open_id=current,
        current_name="张工" if current else "",
        next_open_id=nxt,
        next_name="李工" if nxt else "",
        handoff_at=utc(minutes=handoff_minutes) if handoff_minutes is not None else None,
        reserved_until=utc(minutes=reserved_minutes) if reserved_minutes is not None else None,
    )
