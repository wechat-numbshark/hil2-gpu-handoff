"""预约服务：存储行为、飞书指令闭环、与判定引擎的接缝。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from hil2.booking import BookingError, BookingRequest, BookingStatus, align_down
from hil2.booking_service import BookingService
from hil2.flow_engine import ActionKind, QueueContext, decide
from hil2.models import MonitorState

from .conftest import make_event


@pytest.fixture
def base():
    return align_down(datetime.now(timezone.utc))


@pytest.fixture
def now(base):
    return base + timedelta(minutes=10)


@pytest.fixture
def service(settings, store, now):
    return BookingService(settings=settings, store=store, now=lambda: now)


def req(base, cluster, who, h0, h1, name=""):
    return BookingRequest(cluster=cluster, open_id=who, name=name,
                          start_at=base + timedelta(hours=h0),
                          end_at=base + timedelta(hours=h1))


class TestStoreBookings:
    def test_create_and_read_back(self, store, base, now):
        booking = store.create_booking(req(base, "52", "ou_a", 0, 2, "张工"), now=now)
        assert store.get_booking(booking.id).name == "张工"
        assert booking.live_status(now) is BookingStatus.ACTIVE

    def test_conflict_rejected(self, store, base, now):
        store.create_booking(req(base, "52", "ou_a", 0, 2), now=now)
        with pytest.raises(BookingError, match="已被占用"):
            store.create_booking(req(base, "52", "ou_b", 1, 3), now=now)

    def test_adjacent_slot_allowed(self, store, base, now):
        store.create_booking(req(base, "52", "ou_a", 0, 2), now=now)
        assert store.create_booking(req(base, "52", "ou_b", 2, 4), now=now)

    def test_other_cluster_unaffected(self, store, base, now):
        store.create_booking(req(base, "52", "ou_a", 0, 2), now=now)
        assert store.create_booking(req(base, "58", "ou_b", 0, 2), now=now)

    def test_cancel_frees_the_slot(self, store, base, now):
        booking = store.create_booking(req(base, "52", "ou_a", 2, 4), now=now)
        store.cancel_booking(booking.id, operator_open_id="ou_a")
        assert store.create_booking(req(base, "52", "ou_b", 2, 4), now=now)

    def test_cannot_cancel_someone_elses(self, store, base, now):
        booking = store.create_booking(req(base, "52", "ou_a", 2, 4), now=now)
        with pytest.raises(BookingError, match="自己"):
            store.cancel_booking(booking.id, operator_open_id="ou_b")

    def test_admin_can_cancel_anyones(self, store, base, now):
        booking = store.create_booking(req(base, "52", "ou_a", 2, 4), now=now)
        cancelled = store.cancel_booking(booking.id, operator_open_id="ou_admin", is_admin=True)
        assert cancelled.status is BookingStatus.CANCELLED

    def test_double_cancel_rejected(self, store, base, now):
        booking = store.create_booking(req(base, "52", "ou_a", 2, 4), now=now)
        store.cancel_booking(booking.id, operator_open_id="ou_a")
        with pytest.raises(BookingError, match="取消过"):
            store.cancel_booking(booking.id, operator_open_id="ou_a")

    def test_active_and_next(self, store, base, now):
        store.create_booking(req(base, "52", "ou_a", 0, 2, "张工"), now=now)
        store.create_booking(req(base, "52", "ou_b", 2, 4, "李工"), now=now)
        assert store.active_booking("52", now=now).name == "张工"
        assert store.next_booking("52", now=now).name == "李工"

    def test_expire_marks_done(self, store, base, now):
        store.create_booking(req(base, "52", "ou_a", 0, 1), now=now)
        assert store.expire_bookings(now=now + timedelta(hours=2)) == 1


class TestBusinessHours:
    def test_accepts_opening_and_midnight_boundary(self, store):
        zone = ZoneInfo("Asia/Shanghai")
        start = datetime(2026, 9, 5, 9, 0, tzinfo=zone)
        end = datetime(2026, 9, 6, 0, 0, tzinfo=zone)
        booking = store.create_booking(
            BookingRequest("52", "ou_a", "张工", start, end),
            now=datetime(2026, 9, 5, 8, 0, tzinfo=zone),
            tz="Asia/Shanghai", day_start_hour=9, day_end_hour=24,
            max_hours=15,
        )
        assert booking.start_at == start.astimezone(timezone.utc)

    @pytest.mark.parametrize("start_hour,end_hour,message", [
        (8, 10, "最早"),
        (23, 25, "跨天"),
    ])
    def test_rejects_outside_daily_window(self, store, start_hour, end_hour, message):
        zone = ZoneInfo("Asia/Shanghai")
        day = datetime(2026, 9, 5, 0, 0, tzinfo=zone)
        with pytest.raises(BookingError, match=message):
            store.create_booking(
                BookingRequest(
                    "52", "ou_a", "张工",
                    day + timedelta(hours=start_hour),
                    day + timedelta(hours=end_hour),
                ),
                now=day + timedelta(hours=7),
                tz="Asia/Shanghai", day_start_hour=9, day_end_hour=24,
            )


class TestExtension:
    def test_extends_when_nobody_waiting(self, store, base, now):
        store.create_booking(req(base, "52", "ou_a", 0, 2), now=now)
        booking, blocker = store.extend_active_booking("52", 60, now=now)
        assert blocker is None
        assert booking.effective_end == base + timedelta(hours=3)

    def test_never_squeezes_out_the_next_person(self, store, base, now):
        store.create_booking(req(base, "52", "ou_a", 0, 2, "张工"), now=now)
        store.create_booking(req(base, "52", "ou_b", 2, 4, "李工"), now=now)
        booking, blocker = store.extend_active_booking("52", 120, now=now)
        # 下一位 2 小时后开始，最多只能延到那时，一分钟都不能多
        assert blocker.name == "李工"
        assert booking.effective_end == base + timedelta(hours=2)

    def test_extension_shows_in_derived_handoff(self, store, base, now):
        store.create_booking(req(base, "52", "ou_a", 0, 2), now=now)
        store.extend_active_booking("52", 60, now=now)
        assert store.derive_reservation("52", now=now).handoff_at == base + timedelta(hours=3)


class TestDerivedReservation:
    def test_maps_active_to_current(self, store, base, now):
        store.create_booking(req(base, "52", "ou_a", 0, 2, "张工"), now=now)
        store.create_booking(req(base, "52", "ou_b", 2, 4, "李工"), now=now)
        reservation = store.derive_reservation("52", now=now)
        assert reservation.current_open_id == "ou_a"
        assert reservation.next_open_id == "ou_b"
        assert reservation.handoff_at == base + timedelta(hours=2)

    def test_empty_when_nothing_booked(self, store, now):
        reservation = store.derive_reservation("52", now=now)
        assert reservation.current_open_id is None and reservation.next_open_id is None

    def test_handoff_due_when_booking_ended(self, store, base):
        later = base + timedelta(hours=3)
        store.create_booking(req(base, "52", "ou_a", 0, 2), now=base + timedelta(minutes=5))
        reservation = store.derive_reservation("52", now=later)
        # 时段已过，没有 current，也就无从「到点」——由下一位或空闲信号接管
        assert reservation.current_open_id is None

    def test_release_clears_current_keeps_next(self, store, base, now):
        store.create_booking(req(base, "52", "ou_a", 0, 2), now=now)
        store.create_booking(req(base, "52", "ou_b", 2, 4, "李工"), now=now)
        store.release_active_booking("52", now=now)
        reservation = store.derive_reservation("52", now=now)
        assert reservation.current_open_id is None
        assert reservation.next_name == "李工"


class TestEngineSeam:
    """预约喂给判定引擎后，五态语义必须保持不变。"""

    def test_booking_end_triggers_handoff_prompt(self, store, base):
        store.create_booking(req(base, "52", "ou_a", 0, 2, "张工"),
                             now=base + timedelta(minutes=5))
        store.create_booking(req(base, "52", "ou_b", 2, 4, "李工"),
                             now=base + timedelta(minutes=5))
        # 时段刚到点，但机器上还有真实活动
        at_handoff = base + timedelta(hours=2)
        reservation = store.derive_reservation("52", now=at_handoff - timedelta(minutes=1))
        queue = QueueContext.from_reservation(reservation)
        actions = decide(make_event("52", "busy"), queue)
        assert actions[0].kind is ActionKind.AUDIT_ONLY  # 还没到点，不打扰

        # 交接时刻整点：接班人成为当前占用人，且不能同时又是「下一位」，
        # 否则引擎会通知他「集群可用了」——而他本来就是当前占用人。
        reservation = store.derive_reservation("52", now=at_handoff)
        assert reservation.current_open_id == "ou_b"
        assert reservation.next_open_id is None

    def test_unknown_still_never_notifies_next(self, store, base, now):
        store.create_booking(req(base, "52", "ou_a", 0, 2), now=now)
        store.create_booking(req(base, "52", "ou_b", 2, 4), now=now)
        queue = QueueContext.from_reservation(store.derive_reservation("52", now=now))
        actions = decide(make_event("52", "unknown", reasons=("docker 不可读",)), queue)
        assert actions[0].kind is ActionKind.ALERT_ADMIN

    def test_idle_notifies_the_booked_next_person(self, store, base, now):
        store.create_booking(req(base, "52", "ou_a", 0, 2), now=now)
        store.create_booking(req(base, "52", "ou_b", 2, 4, "李工"), now=now)
        queue = QueueContext.from_reservation(store.derive_reservation("52", now=now))
        actions = decide(make_event("52", "idle"), queue)
        assert actions[0].kind is ActionKind.NOTIFY_NEXT_AVAILABLE
        assert actions[0].target_open_id == "ou_b"


class TestFeishuCommands:
    def _card_text(self, card):
        import json
        return json.dumps(card, ensure_ascii=False)

    def test_book_via_message(self, service, store):
        card = service.handle_message(text="@_user_1 预约 52 明天 14:00-18:00",
                                      open_id="ou_zhang", name="张工")
        assert "预约成功" in card["header"]["title"]["content"]
        assert len(store.list_bookings(cluster="52")) == 1

    def test_conflict_reply_names_the_holder(self, service, store, base, now):
        store.create_booking(req(base, "52", "ou_a", 0, 4, "王工"), now=now)
        offset = (base + timedelta(hours=1)).astimezone().strftime("%H:%M")
        end = (base + timedelta(hours=2)).astimezone().strftime("%H:%M")
        card = service.handle_message(text=f"预约 52 今天 {offset}-{end}", open_id="ou_b", name="李工")
        assert "没成功" in card["header"]["title"]["content"]
        assert "王工" in self._card_text(card)

    def test_unmanaged_cluster_rejected(self, service):
        card = service.handle_message(text="预约 99 明天 14:00-16:00", open_id="ou_a", name="张")
        assert "没成功" in card["header"]["title"]["content"]

    def test_unparseable_time_gets_guidance_not_a_guess(self, service, store):
        card = service.handle_message(text="预约 52 明天", open_id="ou_a", name="张")
        assert "没成功" in card["header"]["title"]["content"]
        assert "14:00-18:00" in self._card_text(card)
        assert store.list_bookings() == []  # 绝不猜一个时间替用户下单

    def test_my_bookings(self, service, store):
        service.handle_message(text="预约 52 明天 14:00-18:00", open_id="ou_a", name="张工")
        card = service.handle_message(text="我的预约", open_id="ou_a", name="张工")
        assert "我的预约" in card["header"]["title"]["content"]
        assert "52" in self._card_text(card)

    def test_cancel_by_id(self, service, store):
        service.handle_message(text="预约 52 明天 14:00-18:00", open_id="ou_a", name="张工")
        booking = store.list_bookings(cluster="52")[0]
        card = service.handle_message(text=f"取消 {booking.id}", open_id="ou_a", name="张工")
        assert "已取消" in card["header"]["title"]["content"]
        assert store.get_booking(booking.id).status is BookingStatus.CANCELLED

    def test_cancel_by_cluster_and_time(self, service, store):
        service.handle_message(text="预约 52 明天 14:00-18:00", open_id="ou_a", name="张工")
        card = service.handle_message(text="取消 52 明天14:00", open_id="ou_a", name="张工")
        assert "已取消" in card["header"]["title"]["content"]

    def test_cannot_cancel_others(self, service, store):
        service.handle_message(text="预约 52 明天 14:00-18:00", open_id="ou_a", name="张工")
        booking = store.list_bookings(cluster="52")[0]
        card = service.handle_message(text=f"取消 {booking.id}", open_id="ou_b", name="李工")
        assert "没成功" in card["header"]["title"]["content"]
        assert store.get_booking(booking.id).status is BookingStatus.BOOKED

    def test_status_query(self, service, store):
        store.record_event(make_event("52", "busy", reasons=("检测到 pytest",)))
        service.handle_message(text="预约 52 明天 14:00-18:00", open_id="ou_a", name="张工")
        card = service.handle_message(text="查看 52", open_id="ou_b", name="李工")
        assert "52 集群状态" in card["header"]["title"]["content"]
        assert "使用中" in self._card_text(card)

    def test_help(self, service):
        card = service.handle_message(text="帮助", open_id="ou_a")
        assert "预约助手" in card["header"]["title"]["content"]
        assert "不会停容器" in self._card_text(card)

    def test_chitchat_is_ignored(self, service):
        assert service.handle_message(text="今天天气不错", open_id="ou_a") is None

    def test_state_changing_cards_disavow_touching_machines(self, service, store):
        """凡是改变了预约状态的卡片，都必须在群里写明没动机器。"""
        cards = [
            service.handle_message(text="预约 52 明天 14:00-18:00", open_id="ou_a", name="张"),
            service.handle_message(text="帮助", open_id="ou_a"),
        ]
        booking = store.list_bookings(cluster="52")[0]
        cards.append(service.handle_message(text=f"取消 {booking.id}", open_id="ou_a", name="张"))
        for card in cards:
            assert card is not None
            body = self._card_text(card)
            assert any(
                phrase in body
                for phrase in ("停容器", "未对机器做任何操作", "独占", "自动释放")
            ), f"卡片没有声明只读边界：{card['header']['title']['content']}"


class TestReminders:
    def test_reminds_once_before_start(self, settings, store, base):
        now = base + timedelta(minutes=25)  # 距离 base+30min 的预约还有 5 分钟
        service = BookingService(settings=settings, store=store, now=lambda: now)
        store.create_booking(
            BookingRequest(cluster="52", open_id="ou_a", name="张工",
                           start_at=base + timedelta(minutes=30),
                           end_at=base + timedelta(hours=2)),
            now=base,
        )
        first = service.upcoming_reminders()
        assert len(first) == 1
        assert "即将开始" in first[0]["card"]["header"]["title"]["content"]
        # 第二次扫描不应重复提醒
        assert service.upcoming_reminders() == []

    def test_silent_when_far_away(self, settings, store, base, now):
        service = BookingService(settings=settings, store=store, now=lambda: now)
        store.create_booking(
            BookingRequest(cluster="52", open_id="ou_a", name="张工",
                           start_at=base + timedelta(hours=5),
                           end_at=base + timedelta(hours=6)),
            now=now,
        )
        assert service.upcoming_reminders() == []
