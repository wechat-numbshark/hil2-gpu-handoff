"""时段预约：槽位对齐、校验、冲突、中文时间解析。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from hil2.booking import (
    SLOT_MINUTES,
    Booking,
    BookingError,
    BookingRequest,
    BookingStatus,
    align_down,
    align_up,
    find_conflicts,
    parse_time_range,
    slot_count,
    validate,
)

TZ = "Asia/Shanghai"
ZONE = ZoneInfo(TZ)


def at(hour, minute=0, day=1):
    return datetime(2026, 9, day, hour, minute, tzinfo=ZONE).astimezone(timezone.utc)


class TestAlignment:
    @pytest.mark.parametrize("minute,expected", [(0, 0), (1, 0), (29, 0), (30, 30), (59, 30)])
    def test_align_down(self, minute, expected):
        assert align_down(at(10, minute)).astimezone(ZONE).minute == expected

    @pytest.mark.parametrize("minute,expected", [(0, 0), (1, 30), (29, 30), (30, 30), (31, 0)])
    def test_align_up(self, minute, expected):
        assert align_up(at(10, minute)).astimezone(ZONE).minute == expected

    def test_align_up_crosses_hour(self):
        assert align_up(at(10, 31)).astimezone(ZONE).hour == 11

    def test_align_strips_seconds(self):
        moment = at(10, 17).replace(second=42, microsecond=999)
        assert align_down(moment).second == 0 and align_down(moment).microsecond == 0

    def test_slot_count(self):
        assert slot_count(at(9), at(11)) == 4
        assert slot_count(at(9), at(9, 30)) == 1


class TestValidation:
    def _req(self, start, end, cluster="52", who="ou_a"):
        return BookingRequest(cluster=cluster, open_id=who, name="张工",
                              start_at=start, end_at=end)

    def test_accepts_and_aligns(self):
        checked = validate(self._req(at(9, 7), at(11, 3)), now=at(8))
        assert checked.start_at == at(9, 0)
        assert checked.end_at == at(11, 30)

    def test_end_before_start_rejected(self):
        with pytest.raises(BookingError, match="结束时间"):
            validate(self._req(at(11), at(9)), now=at(8))

    def test_zero_length_rejected(self):
        with pytest.raises(BookingError):
            validate(self._req(at(9), at(9)), now=at(8))

    def test_past_rejected(self):
        with pytest.raises(BookingError, match="过去"):
            validate(self._req(at(6), at(7)), now=at(10))

    def test_current_slot_still_bookable(self):
        # 正在进行的这半小时应当可约，否则「我现在就要用」没法下单
        checked = validate(self._req(at(10), at(12)), now=at(10, 17))
        assert checked.start_at == at(10)

    def test_too_long_rejected(self):
        with pytest.raises(BookingError, match="最长"):
            validate(self._req(at(8), at(23)), now=at(7), max_hours=12)

    def test_beyond_horizon_rejected(self):
        far = at(9) + timedelta(days=30)
        with pytest.raises(BookingError, match="天以内"):
            validate(self._req(far, far + timedelta(hours=2)), now=at(8), horizon_days=14)

    def test_missing_cluster_rejected(self):
        with pytest.raises(BookingError, match="哪台"):
            validate(self._req(at(9), at(11), cluster=""), now=at(8))

    def test_missing_identity_rejected(self):
        with pytest.raises(BookingError, match="身份"):
            validate(self._req(at(9), at(11), who=""), now=at(8))


def make(cluster, who, start, end, status=BookingStatus.BOOKED, bid="b1"):
    return Booking(id=bid, cluster=cluster, open_id=who, name=who,
                   start_at=start, end_at=end, status=status, created_at=start)


class TestConflicts:
    def _req(self, start, end, cluster="52"):
        return BookingRequest(cluster=cluster, open_id="ou_new", name="新人",
                              start_at=start, end_at=end)

    def test_overlap_detected(self):
        existing = [make("52", "ou_a", at(9), at(12))]
        assert find_conflicts(self._req(at(11), at(13)), existing, now=at(8))

    def test_touching_edges_do_not_conflict(self):
        # 9-11 和 11-13 是相邻不是重叠，必须允许
        existing = [make("52", "ou_a", at(9), at(11))]
        assert find_conflicts(self._req(at(11), at(13)), existing, now=at(8)) == []

    def test_fully_contained_conflicts(self):
        existing = [make("52", "ou_a", at(9), at(15))]
        assert find_conflicts(self._req(at(10), at(11)), existing, now=at(8))

    def test_fully_containing_conflicts(self):
        existing = [make("52", "ou_a", at(10), at(11))]
        assert find_conflicts(self._req(at(9), at(15)), existing, now=at(8))

    def test_other_cluster_does_not_conflict(self):
        existing = [make("58", "ou_a", at(9), at(12))]
        assert find_conflicts(self._req(at(9), at(12)), existing, now=at(8)) == []

    def test_cancelled_does_not_block(self):
        existing = [make("52", "ou_a", at(9), at(12), BookingStatus.CANCELLED)]
        assert find_conflicts(self._req(at(9), at(12)), existing, now=at(8)) == []

    def test_released_does_not_block(self):
        existing = [make("52", "ou_a", at(9), at(12), BookingStatus.RELEASED)]
        assert find_conflicts(self._req(at(9), at(12)), existing, now=at(8)) == []

    def test_extension_extends_conflict_window(self):
        booking = Booking(id="b1", cluster="52", open_id="ou_a", name="张工",
                          start_at=at(9), end_at=at(11), status=BookingStatus.BOOKED,
                          created_at=at(8), extended_to=at(12))
        assert find_conflicts(self._req(at(11), at(12)), [booking], now=at(10))


class TestLiveStatus:
    def test_before_start_is_booked(self):
        b = make("52", "ou_a", at(14), at(16))
        assert b.live_status(at(10)) is BookingStatus.BOOKED

    def test_within_range_is_active(self):
        b = make("52", "ou_a", at(14), at(16))
        assert b.live_status(at(15)) is BookingStatus.ACTIVE

    def test_after_end_is_done(self):
        b = make("52", "ou_a", at(14), at(16))
        assert b.live_status(at(17)) is BookingStatus.DONE

    def test_cancelled_stays_cancelled(self):
        b = make("52", "ou_a", at(14), at(16), BookingStatus.CANCELLED)
        assert b.live_status(at(15)) is BookingStatus.CANCELLED

    def test_extension_keeps_it_active(self):
        b = Booking(id="b", cluster="52", open_id="o", name="n", start_at=at(14),
                    end_at=at(16), status=BookingStatus.BOOKED, created_at=at(10),
                    extended_to=at(17))
        assert b.live_status(at(16, 30)) is BookingStatus.ACTIVE

    def test_occupies_only_for_live_states(self):
        assert BookingStatus.BOOKED.occupies and BookingStatus.ACTIVE.occupies
        assert not BookingStatus.CANCELLED.occupies
        assert not BookingStatus.DONE.occupies
        assert not BookingStatus.RELEASED.occupies


class TestTimeParsing:
    NOW = datetime(2026, 9, 1, 10, 17, tzinfo=ZONE).astimezone(timezone.utc)  # 周二

    def parse(self, text):
        start, end = parse_time_range(text, now=self.NOW, tz=TZ)
        return start.astimezone(ZONE), end.astimezone(ZONE)

    @pytest.mark.parametrize("text,sd,sh,sm,ed,eh,em", [
        ("明天 14:00-18:00", 2, 14, 0, 2, 18, 0),
        ("今天 14:00~16:00", 1, 14, 0, 1, 16, 0),
        ("后天 9:00 到 12:00", 3, 9, 0, 3, 12, 0),
        ("9/3 14:00-18:00", 3, 14, 0, 3, 18, 0),
        ("14:00-16:00", 1, 14, 0, 1, 16, 0),
        ("明天 9点半到11点", 2, 9, 30, 2, 11, 0),
        ("后天 13:30-15:30", 3, 13, 30, 3, 15, 30),
    ])
    def test_common_forms(self, text, sd, sh, sm, ed, eh, em):
        start, end = self.parse(text)
        assert (start.day, start.hour, start.minute) == (sd, sh, sm)
        assert (end.day, end.hour, end.minute) == (ed, eh, em)

    def test_afternoon_applies_to_both_ends(self):
        # 「下午2点到6点」不能只把 2 点变成 14 点而把 6 点留在早上
        start, end = self.parse("明天下午2点到6点")
        assert (start.hour, end.hour) == (14, 18)

    def test_mixed_period_words(self):
        start, end = self.parse("上午9点到下午2点")
        assert (start.hour, end.hour) == (9, 14)

    def test_overnight_rolls_to_next_day(self):
        start, end = self.parse("今天 22:00-02:00")
        assert start.day == 1 and end.day == 2

    def test_weekday_picks_next_occurrence(self):
        start, _ = self.parse("周三 9:00-12:00")
        assert start.weekday() == 2

    def test_past_time_without_date_rolls_to_tomorrow(self):
        # 上午 10:17 发「8:00-9:00」应理解为明天，而不是拒绝
        start, _ = self.parse("8:00-9:00")
        assert start.day == 2

    @pytest.mark.parametrize("text", ["明天", "预约 52", "随便什么时候", ""])
    def test_unparseable_refuses_rather_than_guessing(self, text):
        with pytest.raises(BookingError):
            self.parse(text)

    def test_invalid_clock_rejected(self):
        with pytest.raises(BookingError, match="时间不对"):
            self.parse("明天 25:00-26:00")

    def test_invalid_date_rejected(self):
        with pytest.raises(BookingError, match="日期不对"):
            self.parse("2/31 14:00-16:00")
