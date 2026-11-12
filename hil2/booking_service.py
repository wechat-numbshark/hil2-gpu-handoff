"""预约服务：把用户意图（飞书指令 / 网页操作）变成预约数据。

与协调服务的分工：
- 本模块只管「谁在什么时段占用哪台」，产出 Reservation 供判定引擎消费。
- 它同样**不碰机器**：取消、释放、延期都只改预约表。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .booking import Booking, BookingError, BookingRequest, BookingStatus
from .config import Settings
from .feishu import cards
from .feishu.commands import (
    HELP_TEXT,
    Command,
    CommandKind,
    format_range,
    humanize_day,
    parse,
)
from .store import Store

logger = logging.getLogger(__name__)


class BookingService:
    def __init__(self, *, settings: Settings, store: Store, now=None) -> None:
        self.settings = settings
        self.store = store
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def tz(self) -> str:
        return self.settings.timezone

    @property
    def _limits(self) -> dict[str, Any]:
        return {
            "max_hours": self.settings.booking_max_hours,
            "horizon_days": self.settings.booking_horizon_days,
            "tz": self.settings.timezone,
            "day_start_hour": self.settings.booking_day_start_hour,
            "day_end_hour": self.settings.booking_day_end_hour,
        }

    def is_admin(self, open_id: str) -> bool:
        """环境变量负责首批管理员，后台角色负责日常维护；两者权限一致。"""
        return open_id in (
            set(self.settings.feishu_admin_open_ids) | set(self.store.admin_open_ids())
        )

    # ------------------------------------------------------------------
    # 核心操作（网页和飞书共用）
    # ------------------------------------------------------------------
    def book(
        self, *, cluster: str, open_id: str, name: str,
        start_at: datetime, end_at: datetime, note: str = "",
    ) -> Booking:
        if self.settings.managed_clusters and cluster not in self.settings.managed_clusters:
            raise BookingError(
                f"{cluster} 不在纳管范围内，当前只开放 "
                f"{' / '.join(self.settings.managed_clusters)}"
            )
        booking = self.store.create_booking(
            BookingRequest(
                cluster=cluster, open_id=open_id, name=name,
                start_at=start_at, end_at=end_at, note=note,
            ),
            now=self._now(),
            **self._limits,
        )
        self.store.log_notification(
            cluster=cluster, kind="booking_created", target_open_id=open_id,
            note=f"{name or open_id[:8]} 预约 {format_range(booking.start_at, booking.end_at, self.tz)}",
            delivered=True,
        )
        return booking

    def cancel(self, booking_id: str, *, open_id: str, is_admin: bool = False) -> Booking:
        booking = self.store.cancel_booking(
            booking_id, operator_open_id=open_id, is_admin=is_admin
        )
        self.store.log_notification(
            cluster=booking.cluster, kind="booking_cancelled", target_open_id=open_id,
            note=f"取消 {format_range(booking.start_at, booking.end_at, self.tz)}（只改预约，未操作机器）",
            delivered=True,
        )
        return booking

    def find_by_start(self, *, cluster: str, open_id: str, start_at: datetime) -> Booking | None:
        """按「集群 + 开始时间」找自己的预约，用于「取消 52 明天14:00」。"""
        for booking in self.store.list_bookings(cluster=cluster, open_id=open_id):
            if abs((booking.start_at - start_at).total_seconds()) < 1800:
                return booking
        return None

    def queue_note(self, booking: Booking) -> str:
        """告诉用户他排在谁后面，以及预计什么时候能真正用上。"""
        ahead = [
            item for item in self.store.list_bookings(
                cluster=booking.cluster, since=self._now()
            )
            if item.id != booking.id and item.start_at < booking.start_at
        ]
        if not ahead:
            return ""
        previous = ahead[-1]
        zone = ZoneInfo(self.tz)
        return (
            f"你前面是 {previous.name or previous.open_id[:8]}"
            f"（用到 {previous.effective_end.astimezone(zone):%H:%M}）"
        )

    # ------------------------------------------------------------------
    # 飞书指令入口
    # ------------------------------------------------------------------
    def handle_message(self, *, text: str, open_id: str, name: str = "") -> dict[str, Any] | None:
        """处理群里 @机器人 的一条消息。返回要回的卡片，None 表示不理会。"""
        command = parse(
            text,
            known_clusters=list(self.settings.managed_clusters),
            now=self._now(),
            tz=self.tz,
        )
        handler = {
            CommandKind.BOOK: self._reply_book,
            CommandKind.MY_BOOKINGS: self._reply_my_bookings,
            CommandKind.CANCEL: self._reply_cancel,
            CommandKind.STATUS: self._reply_status,
            CommandKind.HELP: lambda *_: cards.build_help_card(HELP_TEXT),
        }.get(command.kind)
        if handler is None:
            return None  # 认不出来就安静，别在群里刷屏
        try:
            return handler(command, open_id, name)
        except BookingError as exc:
            return cards.build_booking_rejected_card(open_id=open_id, name=name, reason=str(exc))
        except Exception:
            logger.exception("处理预约指令失败：%s", command.kind)
            return cards.build_booking_rejected_card(
                open_id=open_id, name=name, reason="系统出错了，请联系管理员"
            )

    def _reply_book(self, command: Command, open_id: str, name: str) -> dict[str, Any]:
        if command.error:
            return cards.build_booking_rejected_card(
                open_id=open_id, name=name, reason=command.error
            )
        booking = self.book(
            cluster=command.cluster, open_id=open_id, name=name,
            start_at=command.start_at, end_at=command.end_at,
        )
        return cards.build_booked_card(
            cluster=booking.cluster, open_id=open_id, name=name,
            time_range=format_range(booking.start_at, booking.end_at, self.tz),
            booking_id=booking.id,
            queue_note=self.queue_note(booking),
        )

    def _reply_my_bookings(self, _: Command, open_id: str, name: str) -> dict[str, Any]:
        now = self._now()
        rows = [
            {
                "cluster": booking.cluster,
                "range": format_range(booking.start_at, booking.effective_end, self.tz),
                "status": booking.live_status(now).label,
                "id": booking.id,
            }
            for booking in self.store.list_bookings(open_id=open_id, since=now)
        ]
        return cards.build_my_bookings_card(open_id=open_id, name=name, rows=rows)

    def _reply_cancel(self, command: Command, open_id: str, name: str) -> dict[str, Any]:
        booking = None
        if command.booking_id:
            booking = self.store.get_booking(command.booking_id)
        if booking is None and command.cluster and command.start_at:
            booking = self.find_by_start(
                cluster=command.cluster, open_id=open_id, start_at=command.start_at
            )
        if booking is None and command.cluster:
            # 只写了集群号：取消该集群上自己最近的一条未开始预约
            upcoming = [
                item for item in self.store.list_bookings(
                    cluster=command.cluster, open_id=open_id, since=self._now()
                )
            ]
            booking = upcoming[0] if upcoming else None
        if booking is None:
            raise BookingError("没找到要取消的预约，可以发「我的预约」查编号")

        cancelled = self.cancel(
            booking.id, open_id=open_id, is_admin=self.is_admin(open_id)
        )

        # 时段空出来后，看看谁会接上，一并告诉群里
        successor = self.store.next_booking(cancelled.cluster, now=self._now())
        taken_over = ""
        if successor is not None:
            taken_over = (
                f"{successor.name or successor.open_id[:8]}"
                f"（{format_range(successor.start_at, successor.effective_end, self.tz)}）"
            )
        return cards.build_cancelled_card(
            cluster=cancelled.cluster, open_id=open_id, name=name,
            time_range=format_range(cancelled.start_at, cancelled.end_at, self.tz),
            taken_over=taken_over,
        )

    def _reply_status(self, command: Command, open_id: str, name: str) -> dict[str, Any]:
        cluster = command.cluster
        if not cluster:
            raise BookingError("没说要看哪台集群，比如「查看 52」")
        now = self._now()
        snapshot = self.store.get_snapshot(cluster)
        active = self.store.active_booking(cluster, now=now)
        schedule = [
            {
                "range": f"{humanize_day(item.start_at, now=now, tz=self.tz)} "
                         f"{format_range(item.start_at, item.effective_end, self.tz).split(' ', 1)[-1]}",
                "name": item.name or item.open_id[:8],
                "status": item.live_status(now).label,
            }
            for item in self.store.list_bookings(cluster=cluster, since=now)[:6]
        ]
        stale = False
        if snapshot and snapshot["observed_at"]:
            stale = (now - snapshot["observed_at"]).total_seconds() > \
                    self.settings.stale_after_minutes * 60
        return cards.build_cluster_status_card(
            cluster=cluster,
            state_label=snapshot["state"].label if snapshot else "无数据",
            reasons=snapshot["reasons"] if snapshot else [],
            current=(active.name or active.open_id[:8]) if active else "",
            schedule=schedule,
            stale=stale,
        )

    # ------------------------------------------------------------------
    # 到点提醒
    # ------------------------------------------------------------------
    def upcoming_reminders(self) -> list[dict[str, Any]]:
        """找出即将开始、还没提醒过的预约。由调度线程每分钟调用。"""
        now = self._now()
        window = timedelta(minutes=self.settings.booking_reminder_minutes)
        out: list[dict[str, Any]] = []
        for cluster in self.settings.managed_clusters:
            upcoming = self.store.next_booking(cluster, now=now)
            if upcoming is None:
                continue
            if not (now < upcoming.start_at <= now + window):
                continue
            marker = f"booking-reminder:{upcoming.id}"
            if not self.store.claim_callback(marker):
                continue  # 已经提醒过了
            minutes = max(1, int((upcoming.start_at - now).total_seconds() // 60))
            out.append({
                "booking": upcoming,
                "card": cards.build_upcoming_card(
                    cluster=cluster, open_id=upcoming.open_id,
                    name=upcoming.name,
                    time_range=format_range(upcoming.start_at, upcoming.effective_end, self.tz),
                    minutes=minutes,
                ),
            })
        return out
