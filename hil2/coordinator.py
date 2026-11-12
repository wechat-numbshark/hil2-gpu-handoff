"""协调服务：把监控事件和卡片点击变成真实的飞书动作。

职责边界
--------
* 只发消息、只改预约数据。任何路径都不会 SSH 到集群、不会停容器、不会杀进程。
* 关键依赖失败一律 fail-closed：宁可不通知下一位，也不错误宣告可用。
* 所有对外动作写入 notifications 审计表；采集异常写入 incidents 未恢复事件。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .config import Settings
from .feishu import cards
from .feishu.client import FeishuClient, FeishuError
from .flow_engine import (
    Action,
    ActionKind,
    QueueContext,
    decide,
    decide_timeout,
    handle_card_action,
)
from .booking import BookingStatus
from .models import MonitorEvent, MonitorState, Reservation
from .reservations import ReservationSource, ReservationSourceError
from .store import Store

logger = logging.getLogger(__name__)

# 预约数据源的最小刷新间隔，避免每条事件都打一次上游接口。
RESERVATION_CACHE_SECONDS = 30

INCIDENT_COLLECTION = "collection_failed"
INCIDENT_DELIVERY = "delivery_failed"
INCIDENT_RESERVATION = "reservation_source_failed"
INCIDENT_UNATTENDED = "unattended_confirmation"
INCIDENT_UNRESERVED_USAGE = "unreserved_usage"


class Coordinator:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        client: FeishuClient | None,
        reservation_source: ReservationSource,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.reservation_source = reservation_source
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._reservations_synced_at: datetime | None = None
        # booking 模式下预约由用户自助产生，不需要从外部数据源拉取。
        self.booking_mode = settings.reservation_source == "booking"

    # ------------------------------------------------------------------
    # 预约同步
    # ------------------------------------------------------------------
    def sync_reservations(self, *, force: bool = False) -> bool:
        """从数据源刷新预约。失败时保持上一次已知数据并记未恢复事件。

        booking 模式下预约就在本地库里，这里只做过期归档。
        """
        if self.booking_mode:
            self.store.expire_bookings(now=self._now())
            return True

        now = self._now()
        if not force and self._reservations_synced_at is not None:
            if now - self._reservations_synced_at < timedelta(seconds=RESERVATION_CACHE_SECONDS):
                return True
        try:
            reservations = self.reservation_source.fetch()
        except ReservationSourceError as exc:
            logger.warning("预约数据源不可用，沿用上次数据：%s", exc)
            self.store.open_incident(
                cluster="-", kind=INCIDENT_RESERVATION, detail=str(exc)
            )
            return False
        for reservation in reservations:
            self.store.upsert_reservation(reservation)
        self._reservations_synced_at = now
        self.store.resolve_incidents(cluster="-", kind=INCIDENT_RESERVATION)
        return True

    def queue_context(self, cluster: str) -> tuple[QueueContext, Reservation]:
        if self.booking_mode:
            reservation = self.store.derive_reservation(cluster, now=self._now())
        else:
            reservation = self.store.get_reservation(cluster)
        token = self.store.active_card_token(cluster)
        return QueueContext.from_reservation(reservation, current_card_token=token), reservation

    def admin_open_ids(self) -> list[str]:
        """后台设置的管理员与环境变量里的首批管理员拥有同等权限。"""
        return sorted(
            set(self.settings.feishu_admin_open_ids) | set(self.store.admin_open_ids())
        )

    def _track_unreserved_usage(
        self, event: MonitorEvent, queue: QueueContext
    ) -> Action | None:
        """真实活动与预约表不一致时建事件，并只在首次出现时告警一次。

        SSH/登录会话只用来帮助管理员定位，不能单独证明机器正在被业务占用。
        """
        active = event.state is MonitorState.BUSY and not queue.current_open_id
        if not active:
            self.store.resolve_incidents(
                cluster=event.cluster, kind=INCIDENT_UNRESERVED_USAGE
            )
            return None

        already_open = any(
            item["cluster"] == event.cluster
            and item["kind"] == INCIDENT_UNRESERVED_USAGE
            for item in self.store.open_incidents()
        )
        evidence = "；".join(event.reasons) or "检测到真实活动"
        if event.facts.login_users:
            evidence += "；当前登录用户：" + " / ".join(event.facts.login_users)
        elif event.facts.login_session_count:
            evidence += f"；当前有 {event.facts.login_session_count} 个登录会话"
        detail = f"当前没有有效预约，但机器正在使用：{evidence}"
        self.store.open_incident(
            cluster=event.cluster,
            kind=INCIDENT_UNRESERVED_USAGE,
            detail=detail,
        )
        if already_open:
            return None
        return Action(ActionKind.ALERT_ADMIN, event.cluster, note=detail)

    # ------------------------------------------------------------------
    # 监控事件入口
    # ------------------------------------------------------------------
    def ingest(self, event: MonitorEvent) -> dict[str, Any]:
        """处理一条监控事件。重复 event_id 直接丢弃（幂等）。"""
        with self._lock:
            if not self.store.record_event(event):
                logger.info("重复事件已忽略 event_id=%s", event.event_id)
                return {"accepted": False, "duplicate": True, "actions": []}

            # 采集异常挂未恢复事件；恢复正常时自动销案。
            if event.state is MonitorState.UNKNOWN:
                self.store.open_incident(
                    cluster=event.cluster,
                    kind=INCIDENT_COLLECTION,
                    detail="；".join(event.reasons) or "关键采集失败",
                )
            else:
                self.store.resolve_incidents(cluster=event.cluster, kind=INCIDENT_COLLECTION)

            self.sync_reservations()
            queue, reservation = self.queue_context(event.cluster)
            mismatch_alert = self._track_unreserved_usage(event, queue)
            actions = (
                [mismatch_alert]
                if mismatch_alert is not None
                else decide(event, queue, confirm_minutes=self.settings.idle_minutes)
            )
            results = [self._dispatch(action, reservation=reservation, event=event) for action in actions]

        return {
            "accepted": True,
            "duplicate": False,
            "state": event.state.value,
            "actions": [
                {**action.as_dict(), "result": result}
                for action, result in zip(actions, results)
            ],
        }

    # ------------------------------------------------------------------
    # 卡片点击入口
    # ------------------------------------------------------------------
    def handle_card_click(
        self, *, action_value: dict[str, Any], operator_open_id: str, operator_name: str = ""
    ) -> dict[str, Any]:
        cluster = str(action_value.get("cluster", ""))
        with self._lock:
            queue, reservation = self.queue_context(cluster)
            actions = handle_card_action(
                action_value=action_value,
                operator_open_id=operator_open_id,
                queue=queue,
                now=self._now(),
            )
            action = actions[0]

            if action.kind is ActionKind.REJECT_STALE_OR_UNAUTHORIZED:
                # 拒绝的原因分两类，给用户的话要说准：重复点击不是他的错。
                known_card = self.store.get_card(str(action_value.get("card_token", "")))
                already_handled = (
                    known_card is not None
                    and known_card["cluster"] == cluster
                    and known_card["current_open_id"] == operator_open_id
                    and known_card["status"] != "pending"
                )
                toast = (
                    "这张卡片已经处理过了"
                    if already_handled
                    else "这张卡片已失效，或者你不是当前占用人"
                )
                self.store.log_notification(
                    cluster=cluster or "-",
                    kind=action.kind.value,
                    target_open_id=operator_open_id,
                    note=("重复点击已处理的卡片" if already_handled else action.note),
                    delivered=True,
                )
                return {
                    "accepted": False,
                    "toast": toast,
                    "actions": [action.as_dict()],
                }

            token = str(action_value.get("card_token", ""))
            # 先让卡片失效再执行动作：飞书重试造成的重复点击到这里就被拦住。
            if not self.store.resolve_card(token, action.kind.value):
                return {
                    "accepted": False,
                    "toast": "这张卡片已经处理过了",
                    "actions": [
                        Action(
                            ActionKind.REJECT_STALE_OR_UNAUTHORIZED,
                            cluster,
                            target_open_id=operator_open_id,
                            note="卡片已处理，重复点击被拒绝",
                        ).as_dict()
                    ],
                }

            card = self.store.get_card(token)
            result = self._dispatch(action, reservation=reservation, event=None)

            if action.kind is ActionKind.EXTEND_RESERVATION:
                toast = f"已延长 {action.minutes} 分钟，不会打扰你"
                outcome = f"当前占用人选择**继续使用 {action.minutes} 分钟**"
            else:
                toast = "已标记为使用完毕，正在通知下一位"
                outcome = "当前占用人确认**已使用完**，预约已释放（未对机器做任何操作）"

            if card and card.get("message_id"):
                self._safe_update_card(
                    message_id=card["message_id"],
                    card=cards.build_resolved_card(
                        cluster=cluster,
                        operator_name=operator_name,
                        outcome=outcome,
                        detail=action.note,
                    ),
                )
            self.store.resolve_incidents(cluster=cluster, kind=INCIDENT_UNATTENDED)

        return {"accepted": True, "toast": toast, "actions": [{**action.as_dict(), "result": result}]}

    # ------------------------------------------------------------------
    # 超时扫描（10 分钟提醒 / 20 分钟升级，永不自动释放）
    # ------------------------------------------------------------------
    def scan_timeouts(self) -> list[dict[str, Any]]:
        performed: list[dict[str, Any]] = []
        with self._lock:
            for card in self.store.pending_cards():
                created_at = card.get("created_at")
                if created_at is None:
                    continue
                waited = int((self._now() - created_at).total_seconds() // 60)
                reservation = self.store.get_reservation(card["cluster"])
                actions = decide_timeout(
                    cluster=card["cluster"],
                    current_open_id=card["current_open_id"],
                    admin_open_ids=self.admin_open_ids(),
                    waited_minutes=waited,
                    already_reminded=card["reminded_at"] is not None,
                    already_escalated=card["escalated_at"] is not None,
                    remind_after=self.settings.remind_after_minutes,
                    escalate_after=self.settings.escalate_after_minutes,
                )
                for action in actions:
                    result = self._dispatch(action, reservation=reservation, event=None)
                    if action.kind is ActionKind.REMIND_CURRENT_USER:
                        self.store.mark_card_reminded(card["card_token"])
                    elif action.kind is ActionKind.ALERT_ADMIN:
                        self.store.mark_card_escalated(card["card_token"])
                        self.store.open_incident(
                            cluster=card["cluster"],
                            kind=INCIDENT_UNATTENDED,
                            detail=f"占用确认卡片已 {waited} 分钟无人回复",
                        )
                    performed.append({**action.as_dict(), "result": result, "waited_minutes": waited})
        return performed

    # ------------------------------------------------------------------
    # 动作执行
    # ------------------------------------------------------------------
    def _dispatch(
        self, action: Action, *, reservation: Reservation, event: MonitorEvent | None
    ) -> dict[str, Any]:
        handler = {
            ActionKind.AUDIT_ONLY: self._do_audit_only,
            ActionKind.ASK_CURRENT_USER: self._do_ask_current_user,
            ActionKind.NOTIFY_NEXT_AVAILABLE: self._do_notify_next,
            ActionKind.ALERT_ADMIN: self._do_alert_admin,
            ActionKind.REMIND_CURRENT_USER: self._do_remind,
            ActionKind.EXTEND_RESERVATION: self._do_extend,
            ActionKind.RELEASE_AND_NOTIFY_NEXT: self._do_release,
            ActionKind.REJECT_STALE_OR_UNAUTHORIZED: self._do_audit_only,
        }[action.kind]
        try:
            return handler(action, reservation, event)
        except Exception as exc:  # 任何投递失败都不得影响判定结果
            logger.exception("动作执行失败 kind=%s cluster=%s", action.kind.value, action.cluster)
            self.store.log_notification(
                cluster=action.cluster,
                kind=action.kind.value,
                target_open_id=action.target_open_id,
                note=action.note,
                delivered=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            self.store.open_incident(
                cluster=action.cluster,
                kind=INCIDENT_DELIVERY,
                detail=f"{action.kind.value} 投递失败：{type(exc).__name__}",
            )
            return {"delivered": False, "error": type(exc).__name__}

    def _do_audit_only(self, action: Action, *_: Any) -> dict[str, Any]:
        # 只审计，业务大群保持安静。
        self.store.log_notification(
            cluster=action.cluster,
            kind=action.kind.value,
            target_open_id=action.target_open_id,
            note=action.note,
            delivered=True,
        )
        return {"delivered": True, "silent": True}

    def _do_ask_current_user(
        self, action: Action, reservation: Reservation, event: MonitorEvent | None
    ) -> dict[str, Any]:
        if not action.target_open_id:
            return self._do_alert_admin(
                Action(ActionKind.ALERT_ADMIN, action.cluster, note="缺少当前占用人 open_id"),
                reservation,
                event,
            )
        token = self.store.create_card(
            cluster=action.cluster,
            current_open_id=action.target_open_id,
            reason=action.note,
        )
        card = cards.build_confirmation_card(
            cluster=action.cluster,
            current_open_id=action.target_open_id,
            current_name=reservation.current_name,
            card_token=token,
            reason=action.note,
            next_name=reservation.next_name,
            observed_at=event.observed_at if event else None,
        )
        message_id = self._send_card(self.settings.feishu_business_chat_id, card)
        if message_id:
            self.store.attach_card_message(token, message_id)
        self.store.log_notification(
            cluster=action.cluster,
            kind=action.kind.value,
            target_open_id=action.target_open_id,
            chat_id=self.settings.feishu_business_chat_id,
            message_id=message_id,
            note=action.note,
            delivered=bool(message_id),
        )
        return {"delivered": bool(message_id), "card_token": token, "message_id": message_id}

    def _do_notify_next(
        self, action: Action, reservation: Reservation, _: MonitorEvent | None
    ) -> dict[str, Any]:
        card = cards.build_next_available_card(
            cluster=action.cluster,
            next_open_id=action.target_open_id or "",
            next_name=reservation.next_name,
            reason=action.note,
            previous_name=reservation.current_name,
        )
        message_id = self._send_card(self.settings.feishu_business_chat_id, card)
        self.store.log_notification(
            cluster=action.cluster,
            kind=action.kind.value,
            target_open_id=action.target_open_id,
            chat_id=self.settings.feishu_business_chat_id,
            message_id=message_id,
            note=action.note,
            delivered=bool(message_id),
        )
        return {"delivered": bool(message_id), "message_id": message_id}

    def _do_alert_admin(self, action: Action, *_: Any) -> dict[str, Any]:
        card = cards.build_admin_alert_card(
            cluster=action.cluster,
            admin_open_ids=self.admin_open_ids(),
            title="需要人工处理",
            detail=action.note,
            waited_minutes=action.minutes,
        )
        # 管理员告警优先发监控群，避免业务大群被运维噪音刷屏。
        chat_id = self.settings.feishu_monitor_chat_id or self.settings.feishu_business_chat_id
        message_id = self._send_card(chat_id, card)
        self.store.log_notification(
            cluster=action.cluster,
            kind=action.kind.value,
            target_open_id=action.target_open_id,
            chat_id=chat_id,
            message_id=message_id,
            note=action.note,
            delivered=bool(message_id),
        )
        return {"delivered": bool(message_id), "message_id": message_id}

    def _do_remind(
        self, action: Action, reservation: Reservation, _: MonitorEvent | None
    ) -> dict[str, Any]:
        card = cards.build_reminder_card(
            cluster=action.cluster,
            current_open_id=action.target_open_id or "",
            current_name=reservation.current_name,
            waited_minutes=action.minutes or 0,
        )
        message_id = self._send_card(self.settings.feishu_business_chat_id, card)
        self.store.log_notification(
            cluster=action.cluster,
            kind=action.kind.value,
            target_open_id=action.target_open_id,
            chat_id=self.settings.feishu_business_chat_id,
            message_id=message_id,
            note=action.note,
            delivered=bool(message_id),
        )
        return {"delivered": bool(message_id), "message_id": message_id}

    def _do_extend(self, action: Action, *_: Any) -> dict[str, Any]:
        minutes = action.minutes or 30
        if not self.booking_mode:
            expires_at = self.store.extend_reservation(action.cluster, minutes)
            self.store.log_notification(
                cluster=action.cluster, kind=action.kind.value,
                target_open_id=action.target_open_id,
                note=f"{action.note}（未对机器做任何操作）", delivered=True,
            )
            return {"delivered": True, "reserved_until": expires_at.isoformat()}

        booking, blocker = self.store.extend_active_booking(
            action.cluster, minutes, now=self._now()
        )
        if booking is None:
            return {"delivered": False, "error": "没有正在进行的预约可以延长"}

        granted = int((booking.effective_end - booking.end_at).total_seconds() // 60)
        note = f"预约延长至 {booking.effective_end.astimezone().strftime('%H:%M')}"
        if blocker is not None:
            # 如实告诉用户被谁挡住，不能悄悄少给时间。
            note += f"；下一位 {blocker.name or '同事'} 已预约 " \
                    f"{blocker.start_at.astimezone().strftime('%H:%M')}，无法再往后延"
        self.store.log_notification(
            cluster=action.cluster, kind=action.kind.value,
            target_open_id=action.target_open_id,
            note=f"{note}（未对机器做任何操作）", delivered=True,
        )
        return {
            "delivered": True,
            "reserved_until": booking.effective_end.isoformat(),
            "granted_minutes": granted,
            "requested_minutes": minutes,
            "blocked_by": blocker.name if blocker else None,
        }

    def _do_release(
        self, action: Action, reservation: Reservation, _: MonitorEvent | None
    ) -> dict[str, Any]:
        # 只改预约状态。交接文件第 5 节：不操作机器。
        previous_name = reservation.current_name
        if self.booking_mode:
            self.store.release_active_booking(action.cluster, now=self._now())
        else:
            self.store.release_reservation(action.cluster)
        self.store.log_notification(
            cluster=action.cluster,
            kind=action.kind.value,
            target_open_id=action.target_open_id,
            note=f"{action.note}（只标记预约释放，未操作机器）",
            delivered=True,
        )
        if not action.target_open_id:
            return {"delivered": True, "released": True, "notified_next": False}

        card = cards.build_next_available_card(
            cluster=action.cluster,
            next_open_id=action.target_open_id,
            next_name=reservation.next_name,
            reason="上一位已确认使用完毕",
            previous_name=previous_name,
        )
        message_id = self._send_card(self.settings.feishu_business_chat_id, card)
        self.store.log_notification(
            cluster=action.cluster,
            kind=ActionKind.NOTIFY_NEXT_AVAILABLE.value,
            target_open_id=action.target_open_id,
            chat_id=self.settings.feishu_business_chat_id,
            message_id=message_id,
            note="上一位已确认使用完毕",
            delivered=bool(message_id),
        )
        return {"delivered": True, "released": True, "notified_next": bool(message_id)}

    # ------------------------------------------------------------------
    # 飞书投递（未配置时降级为本地日志，便于灰度阶段只写日志运行）
    # ------------------------------------------------------------------
    def _send_card(self, chat_id: str, card: dict[str, Any]) -> str:
        if self.client is None or not self.client.configured or not chat_id:
            logger.info("飞书未配置或缺少群 ID，卡片仅写入本地审计：%s", card["header"]["title"]["content"])
            return ""
        try:
            return self.client.send_card(chat_id=chat_id, card=card)
        except FeishuError as exc:
            logger.error("飞书卡片发送失败：%s", exc)
            raise

    def _safe_update_card(self, *, message_id: str, card: dict[str, Any]) -> None:
        if self.client is None or not self.client.configured or not message_id:
            return
        try:
            self.client.update_card(message_id=message_id, card=card)
        except FeishuError as exc:
            # 更新失败不影响业务结论，卡片 token 已经在库里失效了。
            logger.warning("卡片状态更新失败（token 已失效，不影响安全）：%s", exc)
