"""业务大群里 @机器人 的自助预约指令。

设计原则：
- **解析不出来就说不懂，绝不猜**。猜错时间会让人白等一下午。
- 每条回复都要说清「谁、哪台、什么时段」，让群里其他人也能看懂。
- 指令只影响预约数据，永远不碰机器。

支持的说法（都要 @机器人）：
    预约 52 明天 14:00-18:00
    约一下 58 下午2点到6点
    我的预约
    取消 52 明天14:00      /  取消 a1b2c3d4
    查看 52                /  52 现在谁在用
    帮助
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from ..booking import BookingError, parse_time_range

# 飞书 text 消息里 @ 会变成 @_user_1 这类占位符，解析前先去掉。
_MENTION = re.compile(r"@_user_\d+|@_all|<at[^>]*>.*?</at>|@\S+")


class CommandKind(str, Enum):
    BOOK = "book"
    MY_BOOKINGS = "my_bookings"
    CANCEL = "cancel"
    STATUS = "status"
    HELP = "help"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Command:
    kind: CommandKind
    cluster: str = ""
    start_at: datetime | None = None
    end_at: datetime | None = None
    booking_id: str = ""
    raw: str = ""
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


HELP_TEXT = """HIL 集群预约助手 · 在群里 @我 即可

📅 预约
   预约 52 明天 14:00-18:00
   预约 58 下午2点到6点
   预约 60 9/3 09:00-12:00
   （时段按半小时对齐，一台集群同一时间只能一个人）

📋 查看
   我的预约          看自己约了哪些
   查看 52           看这台今天的排期和当前状态

❌ 取消
   取消 52 明天14:00
   取消 a1b2c3d4     （用预约编号）

系统只做监控和预约，不会停容器、杀进程、删数据，
也不会因为你没回复卡片就自动把机器收走。"""


def clean(text: str) -> str:
    return _MENTION.sub(" ", text or "").strip()


def _find_cluster(text: str, known: list[str]) -> str:
    """先在已知集群里精确找，找不到再退回抓裸数字。

    精确匹配优先是为了避免把时间里的数字当成集群号。
    """
    for cluster in sorted(known, key=len, reverse=True):
        if re.search(rf"(?<!\d){re.escape(cluster)}(?!\d)", text):
            return cluster
    # 去掉时间片段后再找数字，降低误判
    without_time = re.sub(r"\d{1,2}\s*[:：]\s*\d{2}", " ", text)
    without_time = re.sub(r"\d{1,2}\s*[/月]\s*\d{1,2}", " ", without_time)
    without_time = re.sub(r"\d{1,2}\s*[点時时分]", " ", without_time)
    match = re.search(r"(?<!\d)(\d{2,3})(?!\d)", without_time)
    return match.group(1) if match else ""


def parse(text: str, *, known_clusters: list[str], now: datetime | None = None,
          tz: str = "Asia/Shanghai") -> Command:
    """把一条群消息解析成指令。"""
    now = now or datetime.now(timezone.utc)
    body = clean(text)
    if not body:
        return Command(CommandKind.HELP, raw=text)

    lowered = body.lower()

    if re.search(r"帮助|help|怎么用|使用说明|指令", lowered):
        return Command(CommandKind.HELP, raw=body)

    if re.search(r"我的(预约|排期)|我约了|我的$", body):
        return Command(CommandKind.MY_BOOKINGS, raw=body)

    if re.search(r"取消|退订|不用了|撤销", body):
        # 优先按预约编号取消（12~16 位十六进制）
        id_match = re.search(r"(?<![0-9a-f])([0-9a-f]{8,16})(?![0-9a-f])", body)
        cluster = _find_cluster(body, known_clusters)
        start_at = None
        try:
            start_at, _ = parse_time_range(body, now=now, tz=tz)
        except BookingError:
            pass
        return Command(
            CommandKind.CANCEL,
            cluster=cluster,
            booking_id=id_match.group(1) if id_match and not cluster.startswith(id_match.group(1)) else "",
            start_at=start_at,
            raw=body,
        )

    if re.search(r"预约|预定|预订|约一下|约个|要用|申请", body):
        cluster = _find_cluster(body, known_clusters)
        if not cluster:
            return Command(CommandKind.BOOK, raw=body,
                           error="没看出要约哪台集群，写成「预约 52 明天 14:00-18:00」这样")
        try:
            start_at, end_at = parse_time_range(body, now=now, tz=tz)
        except BookingError as exc:
            return Command(CommandKind.BOOK, cluster=cluster, raw=body, error=str(exc))
        return Command(CommandKind.BOOK, cluster=cluster, start_at=start_at,
                       end_at=end_at, raw=body)

    if re.search(r"查看|状态|情况|谁在用|排期|空|忙", body):
        return Command(CommandKind.STATUS,
                       cluster=_find_cluster(body, known_clusters), raw=body)

    # 只写了个集群号，当作查状态
    cluster = _find_cluster(body, known_clusters)
    if cluster and len(body) <= 12:
        return Command(CommandKind.STATUS, cluster=cluster, raw=body)

    return Command(CommandKind.UNKNOWN, raw=body)


def format_range(start: datetime, end: datetime, tz: str = "Asia/Shanghai") -> str:
    zone = ZoneInfo(tz)
    local_start, local_end = start.astimezone(zone), end.astimezone(zone)
    if local_start.date() == local_end.date():
        return f"{local_start:%m-%d %H:%M}~{local_end:%H:%M}"
    return f"{local_start:%m-%d %H:%M}~{local_end:%m-%d %H:%M}"


def humanize_day(moment: datetime, *, now: datetime, tz: str = "Asia/Shanghai") -> str:
    zone = ZoneInfo(tz)
    days = (moment.astimezone(zone).date() - now.astimezone(zone).date()).days
    return {0: "今天", 1: "明天", 2: "后天"}.get(days, f"{moment.astimezone(zone):%m-%d}")
