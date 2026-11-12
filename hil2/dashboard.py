"""大盘视图模型。

把 store 里的快照、预约、卡片、未恢复事件组装成前端一次拉取就够用的结构。
所有时间都以 ISO8601 输出，相对时间由前端按本地时区渲染。

新鲜度规则：采集时间超过 ``stale_after_minutes`` 标记为过期。
过期只影响展示与告警，绝不因此把集群标成可用——沿用最后一次已知状态并明确标注。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .models import ClusterFacts, MonitorState
from .store import Store

# 大盘分组顺序：越需要人处理的排越前。
GROUP_ORDER = ["unknown", "suspected_idle", "busy", "reserved", "idle", "no_data"]

GROUP_LABEL = {
    "unknown": "采集异常",
    "suspected_idle": "待确认",
    "busy": "使用中",
    "reserved": "已保留",
    "idle": "空闲",
    "no_data": "无数据",
}


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _group_of(state: MonitorState | None, *, reservation_active: bool) -> str:
    if state is None:
        return "no_data"
    if state is MonitorState.UNKNOWN:
        return "unknown"
    if state is MonitorState.SUSPECTED_IDLE:
        return "suspected_idle"
    if state is MonitorState.BUSY:
        return "busy"
    if state is MonitorState.RESERVED or reservation_active:
        return "reserved"
    return "idle"


def _container_view(facts: ClusterFacts) -> list[dict[str, Any]]:
    """按 sim / proxy / tester 固定顺序输出，缺失的角色显式标注为未匹配。"""
    out: list[dict[str, Any]] = []
    for role in ("sim", "proxy", "tester"):
        items = facts.containers.get(role)
        if not items:
            out.append({"role": role, "matched": False, "running": 0, "total": 0, "items": []})
            continue
        running = sum(1 for item in items if item.running)
        out.append(
            {
                "role": role,
                "matched": True,
                "running": running,
                "total": len(items),
                "items": [item.as_dict() for item in items],
            }
        )
    return out


def build(store: Store, settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    snapshots = {item["cluster"]: item for item in store.all_snapshots()}
    incidents = store.open_incidents()
    incidents_by_cluster: dict[str, list[dict[str, Any]]] = {}
    for incident in incidents:
        incidents_by_cluster.setdefault(incident["cluster"], []).append(incident)

    # 纳管集群 + 已经上报过但不在纳管清单里的（标为未纳管，仍然展示）。
    clusters = list(settings.managed_clusters)
    for cluster in sorted(snapshots):
        if cluster not in clusters:
            clusters.append(cluster)

    items: list[dict[str, Any]] = []
    counters = {key: 0 for key in GROUP_ORDER}

    for cluster in clusters:
        snapshot = snapshots.get(cluster)
        # booking 模式下预约来自 bookings 表；直接读旧 reservations 表会和预约看板不一致
        if settings.reservation_source == "booking":
            reservation = store.derive_reservation(cluster, now=now)
        else:
            reservation = store.get_reservation(cluster)
        card_token = store.active_card_token(cluster)
        card = store.get_card(card_token) if card_token else None
        reservation_active = bool(
            reservation.reserved_until and reservation.reserved_until > now
        )

        state: MonitorState | None = snapshot["state"] if snapshot else None
        observed_at: datetime | None = snapshot["observed_at"] if snapshot else None
        age_seconds = int((now - observed_at).total_seconds()) if observed_at else None
        stale = age_seconds is not None and age_seconds > settings.stale_after_minutes * 60

        group = _group_of(state, reservation_active=reservation_active)
        counters[group] += 1

        facts: ClusterFacts = snapshot["facts"] if snapshot else ClusterFacts()
        items.append(
            {
                "cluster": cluster,
                "managed": cluster in settings.managed_clusters,
                "group": group,
                "group_label": GROUP_LABEL[group],
                "state": state.value if state else None,
                "state_label": state.label if state else "无数据",
                "reasons": snapshot["reasons"] if snapshot else [],
                "warnings": snapshot["warnings"] if snapshot else [],
                "suspected_idle_minutes": snapshot["suspected_idle_minutes"] if snapshot else 0,
                "observed_at": _iso(observed_at),
                "received_at": _iso(snapshot["received_at"]) if snapshot else None,
                "age_seconds": age_seconds,
                "stale": stale,
                "source": snapshot["source"] if snapshot else None,
                "hostname": facts.hostname,
                "primary_ip": facts.primary_ip,
                "ipv4_addresses": list(facts.ipv4_addresses),
                "network_note": facts.network_note,
                "agent_version": facts.agent_version,
                "data_dir": facts.data_dir,
                "pytest_active": facts.pytest_active,
                "recent_data": facts.recent_data,
                "containers": _container_view(facts),
                "gpus": [gpu.as_dict() for gpu in facts.gpus],
                "gpu_note": facts.gpu_note,
                "login_session_count": facts.login_session_count,
                "login_users": list(facts.login_users),
                "login_note": facts.login_note,
                "reservation": {
                    **reservation.as_dict(),
                    "active": reservation_active,
                    "handoff_due": reservation.handoff_due,
                },
                "pending_card": (
                    {
                        "card_token": card["card_token"],
                        "created_at": _iso(card["created_at"]),
                        "reminded": card["reminded_at"] is not None,
                        "escalated": card["escalated_at"] is not None,
                    }
                    if card
                    else None
                ),
                "incidents": [
                    {**incident, "opened_at": _iso(incident["opened_at"])}
                    for incident in incidents_by_cluster.get(cluster, [])
                ],
            }
        )

    # 排序：先按分组紧急度，再按集群编号。
    items.sort(key=lambda item: (GROUP_ORDER.index(item["group"]), _cluster_sort_key(item["cluster"])))

    last_observed = max(
        (snapshot["observed_at"] for snapshot in snapshots.values() if snapshot["observed_at"]),
        default=None,
    )

    return {
        "generated_at": _iso(now),
        "last_observed_at": _iso(last_observed),
        "refresh_seconds": settings.dashboard_refresh_seconds,
        "stale_after_minutes": settings.stale_after_minutes,
        "counters": {
            **counters,
            "total": len(items),
            "open_incidents": len(incidents),
            "pending_cards": sum(1 for item in items if item["pending_card"]),
        },
        "group_labels": GROUP_LABEL,
        "clusters": items,
        "incidents": [
            {**incident, "opened_at": _iso(incident["opened_at"])} for incident in incidents
        ],
        "notifications": [
            {**item, "created_at": _iso(item["created_at"])}
            for item in store.recent_notifications(limit=20)
        ],
        "safety": {
            "read_only": True,
            "never_auto_release": True,
            "note": "只读监控：不会停容器、杀进程、删数据，也不会因未回复而自动释放集群",
        },
    }


def _cluster_sort_key(cluster: str) -> tuple[int, str]:
    """数字集群按数值排，其余按字符串排在后面。"""
    return (0, f"{int(cluster):09d}") if cluster.isdigit() else (1, cluster)
