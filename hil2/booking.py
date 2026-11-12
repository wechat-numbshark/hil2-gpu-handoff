"""时段预约领域模型。

粒度固定为半小时，所有时间对齐到 :00 / :30。这样做的理由：
- 台架调试的实际交接颗粒度就是半小时级，更细只会制造碎片。
- 对齐后冲突检测和网页时间轴都变成整数槽位运算，不会有边界误差。

预约与闲忙判定的关系：
    bookings（谁在什么时段占用）
        └─→ 派生 Reservation(current / next / handoff_at)
                └─→ 喂给 flow_engine.decide()（引擎本身完全不动）

这意味着换成正式预约 API 时，只要能产出同样的 Reservation，判定引擎无需任何改动。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable
from zoneinfo import ZoneInfo

SLOT_MINUTES = 30
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES  # 48


class BookingStatus(str, Enum):
    BOOKED = "booked"        # 未开始
    ACTIVE = "active"        # 正在进行（由当前时间派生，不落库）
    DONE = "done"            # 已结束
    CANCELLED = "cancelled"  # 已取消
    RELEASED = "released"    # 占用人点「已使用完」提前释放

    @property
    def label(self) -> str:
        return {
            BookingStatus.BOOKED: "待开始",
            BookingStatus.ACTIVE: "使用中",
            BookingStatus.DONE: "已结束",
            BookingStatus.CANCELLED: "已取消",
            BookingStatus.RELEASED: "已提前释放",
        }[self]

    @property
    def occupies(self) -> bool:
        """该状态是否仍占着时段（用于冲突检测）。"""
        return self in (BookingStatus.BOOKED, BookingStatus.ACTIVE)


class BookingError(ValueError):
    """预约请求不合法。错误信息直接给用户看，必须说人话。"""


# ----------------------------------------------------------------------
# 槽位运算
# ----------------------------------------------------------------------
def align_down(moment: datetime) -> datetime:
    """向下对齐到半小时边界。"""
    minute = (moment.minute // SLOT_MINUTES) * SLOT_MINUTES
    return moment.replace(minute=minute, second=0, microsecond=0)


def align_up(moment: datetime) -> datetime:
    """向上对齐到半小时边界。"""
    floor = align_down(moment)
    return floor if floor == moment else floor + timedelta(minutes=SLOT_MINUTES)


def slot_count(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60 // SLOT_MINUTES)


def iter_slots(start: datetime, end: datetime) -> Iterable[datetime]:
    cursor = start
    while cursor < end:
        yield cursor
        cursor += timedelta(minutes=SLOT_MINUTES)


@dataclass(frozen=True)
class Booking:
    id: str
    cluster: str
    open_id: str
    name: str
    start_at: datetime
    end_at: datetime
    status: BookingStatus
    created_at: datetime
    note: str = ""
    # 卡片「继续使用 30/60 分钟」延长到的时间，仅在未与下一位冲突时写入。
    extended_to: datetime | None = None

    @property
    def effective_end(self) -> datetime:
        if self.extended_to and self.extended_to > self.end_at:
            return self.extended_to
        return self.end_at

    def live_status(self, now: datetime) -> BookingStatus:
        """把落库状态和当前时间合成成展示用状态。"""
        if self.status in (BookingStatus.CANCELLED, BookingStatus.RELEASED):
            return self.status
        if now >= self.effective_end:
            return BookingStatus.DONE
        if now >= self.start_at:
            return BookingStatus.ACTIVE
        return BookingStatus.BOOKED

    def covers(self, moment: datetime) -> bool:
        return self.start_at <= moment < self.effective_end

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start_at < end and start < self.effective_end

    @property
    def slots(self) -> int:
        return slot_count(self.start_at, self.end_at)

    def as_dict(self, *, now: datetime | None = None, tz: str = "Asia/Shanghai") -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        zone = ZoneInfo(tz)
        live = self.live_status(now)
        return {
            "id": self.id,
            "cluster": self.cluster,
            "open_id": self.open_id,
            "name": self.name,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "effective_end": self.effective_end.isoformat(),
            "extended_to": self.extended_to.isoformat() if self.extended_to else None,
            "status": live.value,
            "status_label": live.label,
            "stored_status": self.status.value,
            "slots": self.slots,
            "note": self.note,
            "local_start": self.start_at.astimezone(zone).strftime("%m-%d %H:%M"),
            "local_end": self.effective_end.astimezone(zone).strftime("%H:%M"),
            "created_at": self.created_at.isoformat(),
        }


# ----------------------------------------------------------------------
# 请求校验
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class BookingRequest:
    cluster: str
    open_id: str
    name: str
    start_at: datetime
    end_at: datetime
    note: str = ""


def validate(
    request: BookingRequest,
    *,
    now: datetime,
    max_hours: int = 12,
    horizon_days: int = 14,
    min_slots: int = 1,
    tz: str = "Asia/Shanghai",
    day_start_hour: int = 0,
    day_end_hour: int = 24,
) -> BookingRequest:
    """校验并归一化预约请求。返回对齐后的请求，或抛出说人话的 BookingError。"""
    if not request.cluster:
        raise BookingError("没说要预约哪台集群")
    if not request.open_id:
        raise BookingError("拿不到你的飞书身份，无法预约")

    start = align_down(request.start_at.astimezone(timezone.utc))
    end = align_up(request.end_at.astimezone(timezone.utc))

    if end <= start:
        raise BookingError("结束时间必须晚于开始时间")

    if not 0 <= day_start_hour < day_end_hour <= 24:
        raise BookingError("预约开放时间配置不正确，请联系管理员")

    slots = slot_count(start, end)
    if slots < min_slots:
        raise BookingError(f"最短预约 {min_slots * SLOT_MINUTES} 分钟")
    if slots * SLOT_MINUTES > max_hours * 60:
        raise BookingError(f"单次预约最长 {max_hours} 小时，太长的话请分几段预约")

    # 按业务时区限制每天的开放时间。结束点允许精确落在次日 00:00，
    # 它代表当天 24:00；除此之外的跨日预约一律拆成两条。
    # 0–24 是测试/兼容用的“全天开放”，这种配置只受最长时长约束。
    if (day_start_hour, day_end_hour) != (0, 24):
        zone = ZoneInfo(tz)
        local_start = start.astimezone(zone)
        local_end = end.astimezone(zone)
        next_midnight = (
            local_start.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        )
        if local_end.date() != local_start.date():
            if local_end != next_midnight:
                raise BookingError(
                    f"预约不能跨天，请选择当天 {day_start_hour}:00–{day_end_hour}:00 的时段"
                )
            end_minutes = 24 * 60
        else:
            end_minutes = local_end.hour * 60 + local_end.minute
        start_minutes = local_start.hour * 60 + local_start.minute
        if start_minutes < day_start_hour * 60:
            raise BookingError(f"预约最早从 {day_start_hour}:00 开始")
        if end_minutes > day_end_hour * 60:
            raise BookingError(f"预约最晚到 {day_end_hour}:00 结束")

    # 允许预约「正在进行的这半小时」，但不允许往回补录历史。
    if end <= align_down(now):
        raise BookingError("不能预约已经过去的时间")
    if start > now + timedelta(days=horizon_days):
        raise BookingError(f"最多只能预约 {horizon_days} 天以内的时段")

    return BookingRequest(
        cluster=request.cluster,
        open_id=request.open_id,
        name=request.name,
        start_at=start,
        end_at=end,
        note=request.note,
    )


def find_conflicts(
    request: BookingRequest, existing: Iterable[Booking], *, now: datetime
) -> list[Booking]:
    """返回与请求时段冲突的预约。一台集群同一时间只能一个人。"""
    return [
        booking
        for booking in existing
        if booking.cluster == request.cluster
        and booking.live_status(now).occupies
        and booking.overlaps(request.start_at, request.end_at)
    ]


def describe_conflict(conflicts: list[Booking], tz: str = "Asia/Shanghai") -> str:
    zone = ZoneInfo(tz)
    parts = [
        f"{item.name or item.open_id[:8]}（"
        f"{item.start_at.astimezone(zone).strftime('%m-%d %H:%M')}"
        f"~{item.effective_end.astimezone(zone).strftime('%H:%M')}）"
        for item in conflicts[:3]
    ]
    tail = " 等" if len(conflicts) > 3 else ""
    return "、".join(parts) + tail


# ----------------------------------------------------------------------
# 中文时间解析
# ----------------------------------------------------------------------
_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_DAY_WORDS = {
    "今天": 0, "今日": 0, "本日": 0,
    "明天": 1, "明日": 1,
    "后天": 2, "後天": 2,
    "大后天": 3,
}


def parse_time_range(
    text: str, *, now: datetime, tz: str = "Asia/Shanghai"
) -> tuple[datetime, datetime]:
    """从一句中文里解析出时段。

    支持的写法：
        今天 14:00-16:00      明天 14:00~18:00      后天 9:00 到 12:00
        9/3 14:00-18:00       09-03 14:00-18:00     周三 14:00-16:00
        14:00-16:00           （不写日期默认今天，若已过则顺延到明天）
        明天下午 2 点到 6 点

    解析不出来就抛 BookingError，绝不猜一个时间替用户下单。
    """
    zone = ZoneInfo(tz)
    local_now = now.astimezone(zone)
    cleaned = text.replace("：", ":").replace("－", "-").replace("—", "-").replace("～", "~")

    # ---- 日期 ----
    day_offset = None
    base_date = None

    for word, offset in _DAY_WORDS.items():
        if word in cleaned:
            day_offset = offset
            cleaned = cleaned.replace(word, " ")
            break

    if day_offset is None:
        # 9/3 或 09-03（注意别把 14:00-16:00 里的 - 当成日期分隔）
        date_match = re.search(r"(?<!\d)(\d{1,2})\s*[/月]\s*(\d{1,2})\s*日?(?!\s*:)", cleaned)
        if date_match:
            month, day = int(date_match.group(1)), int(date_match.group(2))
            year = local_now.year
            try:
                base_date = local_now.replace(month=month, day=day).date()
            except ValueError as exc:
                raise BookingError(f"日期不对：{month} 月 {day} 日") from exc
            # 月份已过说明是明年
            if base_date < local_now.date() - timedelta(days=1):
                base_date = base_date.replace(year=year + 1)
            cleaned = cleaned[: date_match.start()] + " " + cleaned[date_match.end():]
        else:
            week_match = re.search(r"(?:周|星期|礼拜)\s*([一二三四五六日天])", cleaned)
            if week_match:
                target = _WEEKDAYS[week_match.group(1)]
                delta = (target - local_now.weekday()) % 7
                day_offset = delta or 7  # 「周三」指的是下一个周三，今天就是周三则指下周
                cleaned = cleaned[: week_match.start()] + " " + cleaned[week_match.end():]

    # ---- 时间 ----
    hours = _extract_hours(cleaned)
    if len(hours) < 2:
        raise BookingError("看不懂时间段，请写成「明天 14:00-18:00」这样")

    (start_h, start_m), (end_h, end_m) = hours[0], hours[1]

    if base_date is None:
        base_date = (local_now + timedelta(days=day_offset or 0)).date()

    start_local = datetime.combine(base_date, datetime.min.time(), tzinfo=zone).replace(
        hour=start_h, minute=start_m
    )
    end_local = datetime.combine(base_date, datetime.min.time(), tzinfo=zone).replace(
        hour=end_h % 24, minute=end_m
    )
    if end_h >= 24 or end_local <= start_local:
        end_local += timedelta(days=1)  # 跨夜，例如 22:00-02:00

    # 没写日期且时段已经过去 → 顺延到明天，避免用户一早写 9:00-12:00 却被拒
    if day_offset is None and base_date == local_now.date() and end_local <= local_now:
        start_local += timedelta(days=1)
        end_local += timedelta(days=1)

    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _extract_hours(text: str) -> list[tuple[int, int]]:
    """抽出文本里的时间点，返回 [(时, 分), ...]。

    「下午 2 点到 6 点」里的下午要同时作用于 2 点和 6 点，
    因此按时段词把文本切段，每段用各自的偏移量归一化。
    """
    markers = [
        ("凌晨", 0), ("早上", 0), ("上午", 0), ("中午", 12),
        ("下午", 12), ("傍晚", 12), ("晚上", 12), ("夜里", 12),
    ]
    positions = [
        (match.start(), len(word), offset)
        for word, offset in markers
        for match in re.finditer(re.escape(word), text)
    ]
    positions.sort()

    # 切成 (文本片段, 该片段适用的偏移量)
    segments: list[tuple[str, int]] = []
    cursor = 0
    for start, length, offset in positions:
        if start > cursor:
            segments.append((text[cursor:start], 0))
        cursor = start + length
        end = len(text)
        for next_start, _, _ in positions:
            if next_start >= cursor:
                end = next_start
                break
        segments.append((text[cursor:end], offset))
        cursor = end
    if cursor < len(text):
        segments.append((text[cursor:], 0))
    if not segments:
        segments = [(text, 0)]

    found: list[tuple[int, int]] = []
    for chunk, offset in segments:
        for hour, minute in _hours_in(chunk):
            # 「下午 2 点」→ 14 点；「下午 14 点」保持 14 点，不重复加。
            if offset and hour < 12:
                hour += offset
            found.append((hour, minute))
    return found


def _hours_in(chunk: str) -> list[tuple[int, int]]:
    normalized = re.sub(r"(\d{1,2})\s*[点時时]\s*半", lambda m: f"{m.group(1)}:30", chunk)
    normalized = re.sub(r"(\d{1,2})\s*[点時时]\s*(\d{1,2})\s*分", r"\1:\2", normalized)
    normalized = re.sub(r"(\d{1,2})\s*[点時时]", r"\1:00", normalized)

    out: list[tuple[int, int]] = []
    for match in re.finditer(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", normalized):
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 24 or minute > 59:
            raise BookingError(f"时间不对：{match.group(0)}")
        out.append((hour, minute))
    return out
