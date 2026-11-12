#!/usr/bin/env python3
"""播种演示数据：不连飞书、不连集群，直接把一批监控事件写进本地库。

用于本地看大盘、做 UI 走查和端到端演练。生产环境不要跑。

    python scripts/seed_demo.py --db var/demo.db
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hil2.models import MonitorEvent  # noqa: E402
from hil2.store import Store  # noqa: E402

NOW = datetime.now(timezone.utc)


def event(cluster: str, state: str, minutes_ago: int, **kwargs) -> MonitorEvent:
    observed = NOW - timedelta(minutes=minutes_ago)
    payload = {
        "schema": "hil.monitor.v1",
        "event_id": f"{cluster}-{int(observed.timestamp())}-{state}",
        "cluster": cluster,
        "state": state,
        "observed_at": observed.isoformat(),
        "reasons": kwargs.pop("reasons", []),
        "warnings": kwargs.pop("warnings", []),
        "suspected_idle_minutes": kwargs.pop("suspected_idle_minutes", 0),
        "facts": kwargs.pop("facts", {}),
    }
    return MonitorEvent.from_dict(payload, source=kwargs.pop("source", "direct"))


def containers(sim=1, proxy=1, tester=1, running=True) -> dict:
    def make(prefix: str, count: int) -> list[dict]:
        return [
            {"name": f"hil-{prefix}-{i + 1}", "state": "running" if running else "exited"}
            for i in range(count)
        ]

    return {"sim": make("sim", sim), "proxy": make("proxy", proxy), "tester": make("tester", tester)}


def gpus(*utils: int) -> list[dict]:
    return [
        {"index": i, "utilization": u, "memory_used_mb": 3200 + u * 180}
        for i, u in enumerate(utils)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="播种大盘演示数据")
    parser.add_argument("--db", default="var/demo.db")
    parser.add_argument("--reservations", default="var/demo-reservations.json")
    args = parser.parse_args()

    db_path = Path(args.db)
    if db_path.exists():
        db_path.unlink()
    store = Store(db_path)

    # 52：使用中，pytest 在跑
    store.record_event(event(
        "52", "busy", 2,
        reasons=["检测到测试进程（pytest 等）", "/data1 最近 15 分钟有文件写入"],
        facts={
            "hostname": "hil-cluster-52", "agent_version": "1.0.0", "data_dir": "/data1",
            "pytest_active": True, "recent_data": True,
            "containers": containers(sim=2, proxy=1, tester=2),
            "gpus": gpus(94, 88, 91, 76, 82, 90, 85, 79),
        },
    ))

    # 58：疑似空闲，GPU 还很高但没人在用 —— 这正是本系统要抓的场景
    store.record_event(event(
        "58", "suspected_idle", 3,
        reasons=["无测试进程，且 /data1 最近 15 分钟无写入"],
        suspected_idle_minutes=20,
        facts={
            "hostname": "hil-cluster-58", "agent_version": "1.0.0", "data_dir": "/data1",
            "pytest_active": False, "recent_data": False,
            "containers": containers(sim=2, proxy=1, tester=1),
            "gpus": gpus(97, 96, 95, 97, 94, 96, 93, 95),
        },
    ))

    # 60：明确空闲，核心容器全停
    store.record_event(event(
        "60", "idle", 1,
        reasons=["sim / proxy / tester 核心容器均已停止"],
        facts={
            "hostname": "hil-cluster-60", "agent_version": "1.0.0", "data_dir": "/data1",
            "pytest_active": False, "recent_data": False,
            "containers": containers(sim=2, proxy=1, tester=1, running=False),
            "gpus": gpus(0, 0, 0, 0, 0, 0, 0, 0),
        },
    ))

    # 61：采集异常 —— 绝不能被判成空闲
    store.record_event(event(
        "61", "unknown", 4,
        reasons=["无法读取 Docker 容器：permission denied while trying to connect to the Docker daemon socket"],
        facts={"hostname": "hil-cluster-61", "agent_version": "1.0.0", "data_dir": "/data1",
               "gpu_note": "采集失败（不影响闲忙结论）"},
        source="feishu_monitor_chat",
    ))

    # 62：数据过期 —— agent 挂了，最后一次采集是 3 小时前
    store.record_event(event(
        "62", "busy", 185,
        reasons=["检测到测试进程（pytest 等）"],
        warnings=["未匹配容器角色：proxy"],
        facts={
            "hostname": "hil-cluster-62", "agent_version": "1.0.0", "data_dir": "/data1",
            "pytest_active": True, "recent_data": False,
            "containers": {**containers(sim=1, tester=1), "proxy": []},
            "gpus": gpus(41, 38, 0, 0, 12, 0, 0, 0),
        },
    ))

    # 56：双 ECU 调试专用，未纳管，但已经上报过
    store.record_event(event(
        "56", "reserved", 6,
        reasons=["张工 已声明保留到 今天 18:30"],
        facts={
            "hostname": "hil-bench-56", "agent_version": "1.0.0", "data_dir": "/data1",
            "pytest_active": False, "recent_data": False,
            "containers": containers(sim=1, proxy=1, tester=1),
            "gpus": gpus(12, 8, 0, 0),
        },
    ))

    # 预约队列
    reservations = [
        {"cluster": "52", "current_open_id": "ou_zhang", "current_name": "张工",
         "next_open_id": "ou_li", "next_name": "李工",
         "handoff_at": (NOW + timedelta(minutes=42)).isoformat()},
        {"cluster": "58", "current_open_id": "ou_wang", "current_name": "王工",
         "next_open_id": "ou_chen", "next_name": "陈工",
         "handoff_at": (NOW - timedelta(minutes=8)).isoformat()},
        {"cluster": "60", "current_open_id": None, "current_name": "",
         "next_open_id": "ou_zhao", "next_name": "赵工"},
        {"cluster": "56", "current_open_id": "ou_sun", "current_name": "孙工",
         "reserved_until": (NOW + timedelta(minutes=48)).isoformat()},
    ]
    for item in reservations:
        from hil2.models import Reservation
        store.upsert_reservation(Reservation.from_dict(item))

    reservation_path = Path(args.reservations)
    reservation_path.parent.mkdir(parents=True, exist_ok=True)
    reservation_path.write_text(
        json.dumps(reservations, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 58 已经发出确认卡片，14 分钟没人回复
    token = store.create_card(cluster="58", current_open_id="ou_wang",
                             reason="容器仍在，但无 pytest、无近期数据写入；请人工确认")
    store.attach_card_message(token, "om_demo_message")
    store._conn.execute(  # noqa: SLF001  仅演示脚本使用，把卡片时间调早
        "UPDATE cards SET created_at = ? WHERE card_token = ?",
        ((NOW - timedelta(minutes=14)).isoformat(), token),
    )
    store.mark_card_reminded(token)

    # ---------- 时段预约演示 ----------
    from hil2.booking import BookingRequest, align_down
    base = align_down(NOW)

    def book(cluster, who, name, h0, h1, note=""):
        try:
            return store.create_booking(
                BookingRequest(cluster=cluster, open_id=who, name=name,
                               start_at=base + timedelta(hours=h0),
                               end_at=base + timedelta(hours=h1), note=note),
                now=NOW,
            )
        except Exception as exc:
            print(f"  跳过预约 {cluster} {name}：{exc}")
            return None

    book("52", "ou_zhang", "张工", -1, 2, "整车下线测试")
    book("52", "ou_li", "李工", 2, 5)
    book("52", "ou_zhao", "赵工", 6, 9)
    book("58", "ou_wang", "王工", -2, 1)
    book("58", "ou_chen", "陈工", 3, 6, "回归验证")
    book("60", "ou_sun", "孙工", 1, 4)
    book("60", "ou_zhang", "张工", 20, 24)

    store.open_incident(cluster="61", kind="collection_failed",
                        detail="无法读取 Docker 容器：permission denied")
    store.open_incident(cluster="62", kind="delivery_failed",
                        detail="ask_current_user 投递失败：FeishuError")

    for entry in [
        ("58", "ask_current_user", "ou_wang", "容器仍在，但无 pytest、无近期数据写入；请人工确认"),
        ("58", "remind_current_user", "ou_wang", "已 10 分钟未回复，提醒一次；系统不会自动释放集群"),
        ("60", "notify_next_available", "ou_zhao", "已由强空闲信号确认可用；不打扰上一位用户"),
        ("61", "alert_admin", None, "采集异常，禁止判空闲：无法读取 Docker 容器"),
        ("52", "audit_only", None, "存在真实活动，无需打扰"),
    ]:
        store.log_notification(cluster=entry[0], kind=entry[1], target_open_id=entry[2],
                               note=entry[3], delivered=True)

    store.close()
    print(f"演示数据已写入 {db_path}")
    print(f"预约队列已写入 {reservation_path}")
    print("\n启动大盘：")
    print(f"  DB_PATH={db_path} RESERVATION_FILE={reservation_path} "
          f".venv/bin/python -m hil2 --port 8848")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
