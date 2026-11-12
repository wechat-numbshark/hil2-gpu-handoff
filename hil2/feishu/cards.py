"""飞书交互卡片构建。

卡片只承载「人工确认」这一件事：继续 30 / 继续 60 / 已使用完。
任何按钮都不会去操作机器——「已使用完」只把预约标记为已释放。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

_HEADER_TEMPLATE = {
    "ask": "orange",
    "remind": "yellow",
    "available": "green",
    "alert": "red",
    "done": "grey",
    "booked": "blue",
    "info": "indigo",
    "reject": "carmine",
}


def _at(open_id: str | None, name: str = "") -> str:
    if not open_id:
        return name or "相关同学"
    return f'<at id="{open_id}">{name}</at>'


def _fmt_time(value: datetime | None) -> str:
    if value is None:
        return "未知"
    return value.astimezone().strftime("%m-%d %H:%M")


def _header(title: str, kind: str) -> dict[str, Any]:
    return {
        "template": _HEADER_TEMPLATE.get(kind, "blue"),
        "title": {"tag": "plain_text", "content": title},
    }


def _note(text: str) -> dict[str, Any]:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": text}]}


def build_confirmation_card(
    *,
    cluster: str,
    current_open_id: str,
    current_name: str = "",
    card_token: str,
    reason: str,
    next_name: str = "",
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """给当前占用人的占用确认卡片。"""

    def button(label: str, operation: str, style: str = "default") -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": style,
            "value": {
                "operation": operation,
                "cluster": cluster,
                "card_token": card_token,
            },
        }

    lines = [
        f"{_at(current_open_id, current_name)}，**{cluster} 集群**当前无法确认是否仍在使用。",
        "",
        f"**判定依据**：{reason}",
    ]
    if next_name:
        lines.append(f"**等待中**：{next_name}")
    lines.append("")
    lines.append("请选择当前状态。**系统不会因未回复而自动释放集群。**")

    return {
        "config": {"wide_screen_mode": True, "enable_forward": False, "update_multi": True},
        "header": _header(f"{cluster} 集群占用确认", "ask"),
        "elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    button("继续使用 30 分钟", "continue_30"),
                    button("继续使用 60 分钟", "continue_60"),
                    button("已使用完", "released", "primary"),
                ],
            },
            _note(f"采集时间 {_fmt_time(observed_at)}　·　只读监控，不会停容器 / 杀进程 / 删数据"),
        ],
    }


def build_resolved_card(
    *,
    cluster: str,
    operator_name: str,
    outcome: str,
    detail: str = "",
) -> dict[str, Any]:
    """卡片被点击后替换成的结果态，按钮消失，避免重复操作。"""
    body = [f"**{cluster} 集群**：{outcome}", "", f"操作人：{operator_name or '当前占用人'}"]
    if detail:
        body.extend(["", detail])
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False, "update_multi": True},
        "header": _header(f"{cluster} 集群占用确认（已处理）", "done"),
        "elements": [
            {"tag": "markdown", "content": "\n".join(body)},
            _note("本卡片已失效，如需再次确认请等待下一轮监控事件"),
        ],
    }


def build_next_available_card(
    *,
    cluster: str,
    next_open_id: str,
    next_name: str = "",
    reason: str = "",
    previous_name: str = "",
) -> dict[str, Any]:
    """明确空闲 / 用户确认释放后，通知下一位可以使用。"""
    lines = [f"{_at(next_open_id, next_name)}，**{cluster} 集群现在可以使用了。**"]
    if reason:
        lines.extend(["", f"**依据**：{reason}"])
    if previous_name:
        lines.append(f"上一位使用人：{previous_name}")
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": _header(f"{cluster} 集群可以使用", "available"),
        "elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            _note("一台集群同一时间只由一人独占，使用前请确认队列顺序"),
        ],
    }


def build_reminder_card(
    *,
    cluster: str,
    current_open_id: str,
    current_name: str = "",
    waited_minutes: int,
) -> dict[str, Any]:
    """10 分钟未回复的一次性提醒。仍然不会自动释放。"""
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": _header(f"{cluster} 集群占用确认提醒", "remind"),
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"{_at(current_open_id, current_name)}，"
                    f"**{cluster} 集群**的占用确认卡片已经 {waited_minutes} 分钟没有回复。\n\n"
                    "麻烦在上方卡片上点一下：继续使用，或者已使用完。"
                ),
            },
            _note("系统不会自动释放集群，也不会关闭你的环境"),
        ],
    }


def build_admin_alert_card(
    *,
    cluster: str,
    admin_open_ids: Iterable[str],
    title: str,
    detail: str,
    waited_minutes: int | None = None,
) -> dict[str, Any]:
    """管理员告警：采集异常、超时升级、找不到占用人等。"""
    mentions = " ".join(_at(open_id) for open_id in admin_open_ids) or "管理员"
    lines = [f"{mentions}", "", f"**{cluster} 集群**：{title}", "", detail]
    if waited_minutes is not None:
        lines.append(f"\n已等待 {waited_minutes} 分钟，仍未收到当前占用人回复。")
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": _header(f"{cluster} 集群需要人工处理", "alert"),
        "elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            _note("采集异常绝不判定为空闲；系统不会自动释放集群"),
        ],
    }


# ----------------------------------------------------------------------
# 预约相关卡片
# ----------------------------------------------------------------------
def build_booked_card(
    *,
    cluster: str,
    open_id: str,
    name: str,
    time_range: str,
    booking_id: str,
    queue_note: str = "",
) -> dict[str, Any]:
    """预约成功回执。写清谁、哪台、什么时段，让群里其他人也一眼看懂。"""
    lines = [
        f"{_at(open_id, name)} 已预约 **{cluster} 集群**",
        "",
        f"**时段**：{time_range}",
    ]
    if queue_note:
        lines.append(f"**排队**：{queue_note}")
    lines.extend(["", f"取消请发：`取消 {booking_id}`"])
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": _header(f"{cluster} 集群预约成功", "booked"),
        "elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            _note("时段按半小时对齐 · 一台集群同一时间只由一人独占"),
        ],
    }


def build_booking_rejected_card(*, open_id: str, name: str, reason: str) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": _header("预约没成功", "reject"),
        "elements": [
            {"tag": "markdown", "content": f"{_at(open_id, name)}，{reason}"},
            _note("发「帮助」可以看用法"),
        ],
    }


def build_my_bookings_card(
    *, open_id: str, name: str, rows: list[dict[str, str]]
) -> dict[str, Any]:
    if not rows:
        body = f"{_at(open_id, name)}，你目前没有预约。\n\n发「预约 52 明天 14:00-18:00」就能约。"
    else:
        listed = "\n".join(
            f"· **{row['cluster']}**　{row['range']}　{row['status']}　`{row['id']}`"
            for row in rows
        )
        body = f"{_at(open_id, name)} 的预约：\n\n{listed}"
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": _header("我的预约", "info"),
        "elements": [
            {"tag": "markdown", "content": body},
            _note("取消请发：取消 + 预约编号"),
        ],
    }


def build_cluster_status_card(
    *,
    cluster: str,
    state_label: str,
    reasons: list[str],
    current: str,
    schedule: list[dict[str, str]],
    stale: bool = False,
) -> dict[str, Any]:
    lines = [f"**{cluster} 集群**：{state_label}"]
    if stale:
        lines.append("⚠️ 采集数据可能已过期，以下状态未必是此刻的真实情况")
    if reasons:
        lines.append(f"依据：{'；'.join(reasons)}")
    lines.extend(["", f"**当前占用**：{current or '无人'}"])
    if schedule:
        listed = "\n".join(
            f"· {row['range']}　{row['name']}　{row['status']}" for row in schedule
        )
        lines.extend(["", "**接下来的排期**", listed])
    else:
        lines.extend(["", "**接下来没有预约**，现在可以约"])
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": _header(f"{cluster} 集群状态", "info"),
        "elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            _note("只读监控 · 不会停容器 / 杀进程 / 删数据"),
        ],
    }


def build_help_card(help_text: str) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": _header("集群预约助手", "info"),
        "elements": [{"tag": "markdown", "content": help_text.replace("\n", "\n")}],
    }


def build_upcoming_card(
    *, cluster: str, open_id: str, name: str, time_range: str, minutes: int
) -> dict[str, Any]:
    """预约即将开始时提醒下一位。"""
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": _header(f"{cluster} 集群预约即将开始", "booked"),
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"{_at(open_id, name)}，你预约的 **{cluster} 集群** "
                    f"还有 {minutes} 分钟开始（{time_range}）。"
                ),
            },
            _note("上一位若仍在使用，系统会先请对方确认，不会直接把机器收走"),
        ],
    }


def build_cancelled_card(
    *, cluster: str, open_id: str, name: str, time_range: str, taken_over: str = ""
) -> dict[str, Any]:
    """取消预约回执。明确说明只改了预约，机器没动。"""
    lines = [
        f"{_at(open_id, name)} 已取消 **{cluster} 集群** 的预约",
        "",
        f"**释放时段**：{time_range}",
    ]
    lines.append(f"**下一位**：{taken_over}" if taken_over else "该时段现在空出来了，其他人可以约")
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": _header(f"{cluster} 集群预约已取消", "done"),
        "elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            _note("只改预约数据，未对机器做任何操作"),
        ],
    }
