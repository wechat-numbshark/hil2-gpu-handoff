"""SQLite 状态存储。

保存交接文件第 10 节 P1 要求的最小状态集：event_id 去重、cluster、card_token、
当前状态、提醒次数和到期时间；另外保存大盘所需的最新快照与审计流水。

并发模型：单进程多线程（FastAPI 线程池 + 调度线程），用一把可重入锁串行化写入，
WAL 模式保证读不阻塞。1 核 1G 服务器上足够。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .booking import Booking, BookingError, BookingRequest, BookingStatus, describe_conflict, find_conflicts, validate
from .registry import ClusterConfig, CollectMode, OrgDepartment, OrgUser, RegistryError
from .models import ClusterFacts, MonitorEvent, MonitorState, Reservation, parse_datetime

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 事件幂等去重：飞书重试、双通道（webhook + 直连）重复投递都靠它。
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    cluster      TEXT NOT NULL,
    state        TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'unknown',
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_cluster_time ON events (cluster, observed_at DESC);

-- 每个集群的最新快照，供大盘直接读取。
CREATE TABLE IF NOT EXISTS cluster_state (
    cluster       TEXT PRIMARY KEY,
    state         TEXT NOT NULL,
    observed_at   TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'unknown',
    reasons_json  TEXT NOT NULL DEFAULT '[]',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    facts_json    TEXT NOT NULL DEFAULT '{}',
    suspected_idle_minutes INTEGER NOT NULL DEFAULT 0
);

-- 预约。file/http 数据源同步进来，卡片延期只覆盖 reserved_until。
CREATE TABLE IF NOT EXISTS reservations (
    cluster         TEXT PRIMARY KEY,
    current_open_id TEXT,
    current_name    TEXT NOT NULL DEFAULT '',
    next_open_id    TEXT,
    next_name       TEXT NOT NULL DEFAULT '',
    handoff_at      TEXT,
    reserved_until  TEXT,
    updated_at      TEXT NOT NULL
);

-- 卡片状态与超时计时。resolved 后立即失效，旧卡片不能重复操作。
CREATE TABLE IF NOT EXISTS cards (
    card_token      TEXT PRIMARY KEY,
    cluster         TEXT NOT NULL,
    current_open_id TEXT NOT NULL,
    status          TEXT NOT NULL,           -- pending / resolved / superseded
    reason          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    message_id      TEXT,
    reminded_at     TEXT,
    escalated_at    TEXT,
    resolved_at     TEXT,
    resolution      TEXT
);
CREATE INDEX IF NOT EXISTS idx_cards_pending ON cards (cluster, status);

-- 对外动作审计流水。
CREATE TABLE IF NOT EXISTS notifications (
    id             TEXT PRIMARY KEY,
    cluster        TEXT NOT NULL,
    kind           TEXT NOT NULL,
    target_open_id TEXT,
    chat_id        TEXT,
    message_id     TEXT,
    note           TEXT NOT NULL DEFAULT '',
    delivered      INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_time ON notifications (created_at DESC);

-- 未恢复事件：采集异常、投递失败等，恢复前一直在大盘上挂着。
CREATE TABLE IF NOT EXISTS incidents (
    id          TEXT PRIMARY KEY,
    cluster     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    opened_at   TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_open ON incidents (cluster, resolved_at);

-- 时段预约。粒度半小时，一台集群同一时刻只允许一条 occupies 状态的记录。
CREATE TABLE IF NOT EXISTS bookings (
    id          TEXT PRIMARY KEY,
    cluster     TEXT NOT NULL,
    open_id     TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    start_at    TEXT NOT NULL,
    end_at      TEXT NOT NULL,
    extended_to TEXT,
    status      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    cancelled_at TEXT,
    cancelled_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_bookings_cluster_time ON bookings (cluster, start_at);
CREATE INDEX IF NOT EXISTS idx_bookings_owner ON bookings (open_id, start_at);

-- 集群注册表。纳管范围和采集参数改在后台，不再依赖环境变量和重启。
CREATE TABLE IF NOT EXISTS clusters (
    cluster              TEXT PRIMARY KEY,
    name                 TEXT NOT NULL DEFAULT '',
    enabled              INTEGER NOT NULL DEFAULT 1,
    managed              INTEGER NOT NULL DEFAULT 1,
    mode                 TEXT NOT NULL DEFAULT 'agent_push',
    data_dir             TEXT NOT NULL DEFAULT '/data1',
    idle_minutes         INTEGER NOT NULL DEFAULT 15,
    sample_seconds       INTEGER NOT NULL DEFAULT 300,
    confirm_samples      INTEGER NOT NULL DEFAULT 3,
    command_timeout      INTEGER NOT NULL DEFAULT 30,
    sim_pattern          TEXT NOT NULL DEFAULT '',
    proxy_pattern        TEXT NOT NULL DEFAULT '',
    tester_pattern       TEXT NOT NULL DEFAULT '',
    extra_test_processes TEXT NOT NULL DEFAULT '',
    ssh_host             TEXT NOT NULL DEFAULT '',
    ssh_port             INTEGER NOT NULL DEFAULT 22,
    ssh_user             TEXT NOT NULL DEFAULT '',
    ssh_key_path         TEXT NOT NULL DEFAULT '',
    ssh_jump_host        TEXT NOT NULL DEFAULT '',
    ssh_jump_port        INTEGER NOT NULL DEFAULT 22,
    ssh_jump_user        TEXT NOT NULL DEFAULT '',
    ssh_jump_key_path    TEXT NOT NULL DEFAULT '',
    ssh_python           TEXT NOT NULL DEFAULT 'python3',
    ssh_use_sudo         INTEGER NOT NULL DEFAULT 0,
    webhook_url          TEXT NOT NULL DEFAULT '',
    note                 TEXT NOT NULL DEFAULT '',
    updated_at           TEXT NOT NULL,
    last_seen_at         TEXT,
    last_error           TEXT NOT NULL DEFAULT ''
);

-- 飞书通讯录快照。用于后台选管理员、预约页显示真实姓名。
CREATE TABLE IF NOT EXISTS org_users (
    open_id          TEXT PRIMARY KEY,
    name             TEXT NOT NULL DEFAULT '',
    en_name          TEXT NOT NULL DEFAULT '',
    email            TEXT NOT NULL DEFAULT '',
    mobile           TEXT NOT NULL DEFAULT '',
    avatar_url       TEXT NOT NULL DEFAULT '',
    department_ids   TEXT NOT NULL DEFAULT '[]',
    department_names TEXT NOT NULL DEFAULT '[]',
    is_admin         INTEGER NOT NULL DEFAULT 0,
    is_active        INTEGER NOT NULL DEFAULT 1,
    synced_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_org_users_name ON org_users (name);

CREATE TABLE IF NOT EXISTS org_departments (
    department_id TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    parent_id     TEXT NOT NULL DEFAULT '',
    member_count  INTEGER NOT NULL DEFAULT 0,
    synced_at     TEXT NOT NULL
);

-- 可在后台修改的运行时配置。优先级高于环境变量。
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);

-- 后台操作审计。谁改了什么，必须留痕。
CREATE TABLE IF NOT EXISTS admin_audit (
    id         TEXT PRIMARY KEY,
    actor      TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_time ON admin_audit (created_at DESC);

-- 卡片回调幂等：飞书会重试同一个 event_id，重试必须返回首次结果而不是「已处理过」。
CREATE TABLE IF NOT EXISTS callbacks (
    event_id    TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}'
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _parse_opt(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return parse_datetime(value)
    except Exception:
        return None


class Store:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            # v1 数据库原地补列；SQLite 的 ADD COLUMN 会为旧记录填默认值。
            cluster_columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(clusters)")
            }
            for name, declaration in (
                ("ssh_jump_host", "TEXT NOT NULL DEFAULT ''"),
                ("ssh_jump_port", "INTEGER NOT NULL DEFAULT 22"),
                ("ssh_jump_user", "TEXT NOT NULL DEFAULT ''"),
                ("ssh_jump_key_path", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in cluster_columns:
                    self._conn.execute(f"ALTER TABLE clusters ADD COLUMN {name} {declaration}")
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', '1')"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # 事件去重与快照
    # ------------------------------------------------------------------
    def record_event(self, event: MonitorEvent) -> bool:
        """写入事件。返回 True 表示首次收到，False 表示重复（应当直接丢弃）。"""
        payload = json.dumps(event.as_dict(), ensure_ascii=False)
        received_at = _iso(_now())
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO events "
                "(event_id, cluster, state, observed_at, received_at, source, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.cluster,
                    event.state.value,
                    _iso(event.observed_at),
                    received_at,
                    event.source,
                    payload,
                ),
            )
            if cursor.rowcount == 0:
                return False

            # 只有更新的观测才能覆盖快照，避免乱序投递把旧状态写回去。
            row = conn.execute(
                "SELECT observed_at FROM cluster_state WHERE cluster = ?", (event.cluster,)
            ).fetchone()
            newer = row is None or _parse_opt(row["observed_at"]) is None or (
                event.observed_at >= parse_datetime(row["observed_at"])
            )
            if newer:
                conn.execute(
                    "INSERT INTO cluster_state (cluster, state, observed_at, received_at, "
                    "source, reasons_json, warnings_json, facts_json, suspected_idle_minutes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(cluster) DO UPDATE SET state=excluded.state, "
                    "observed_at=excluded.observed_at, received_at=excluded.received_at, "
                    "source=excluded.source, reasons_json=excluded.reasons_json, "
                    "warnings_json=excluded.warnings_json, facts_json=excluded.facts_json, "
                    "suspected_idle_minutes=excluded.suspected_idle_minutes",
                    (
                        event.cluster,
                        event.state.value,
                        _iso(event.observed_at),
                        received_at,
                        event.source,
                        json.dumps(list(event.reasons), ensure_ascii=False),
                        json.dumps(list(event.warnings), ensure_ascii=False),
                        json.dumps(event.facts.as_dict(), ensure_ascii=False),
                        event.suspected_idle_minutes,
                    ),
                )
        return True

    def seen_event(self, event_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return row is not None

    def get_snapshot(self, cluster: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cluster_state WHERE cluster = ?", (cluster,)
            ).fetchone()
        return self._snapshot_row(row) if row else None

    def all_snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM cluster_state").fetchall()
        return [self._snapshot_row(row) for row in rows]

    @staticmethod
    def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "cluster": row["cluster"],
            "state": MonitorState.parse(row["state"]),
            "observed_at": _parse_opt(row["observed_at"]),
            "received_at": _parse_opt(row["received_at"]),
            "source": row["source"],
            "reasons": json.loads(row["reasons_json"]),
            "warnings": json.loads(row["warnings_json"]),
            "facts": ClusterFacts.from_dict(json.loads(row["facts_json"])),
            "suspected_idle_minutes": row["suspected_idle_minutes"],
        }

    def recent_events(self, cluster: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        params: tuple[Any, ...] = ()
        if cluster:
            sql += " WHERE cluster = ?"
            params = (cluster,)
        sql += " ORDER BY observed_at DESC LIMIT ?"
        params += (limit,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "cluster": row["cluster"],
                "state": row["state"],
                "observed_at": _parse_opt(row["observed_at"]),
                "received_at": _parse_opt(row["received_at"]),
                "source": row["source"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 预约
    # ------------------------------------------------------------------
    def upsert_reservation(self, reservation: Reservation, *, keep_reserved_until: bool = True) -> None:
        """写入预约。默认保留本地 reserved_until，避免上游数据源覆盖用户刚点的续期。"""
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT reserved_until, current_open_id FROM reservations WHERE cluster = ?",
                (reservation.cluster,),
            ).fetchone()
            reserved_until = reservation.reserved_until
            if keep_reserved_until and existing is not None:
                local = _parse_opt(existing["reserved_until"])
                # 占用人换人时本地保留立即作废。
                same_owner = existing["current_open_id"] == reservation.current_open_id
                if local and same_owner and (reserved_until is None or local > reserved_until):
                    reserved_until = local
            conn.execute(
                "INSERT INTO reservations (cluster, current_open_id, current_name, next_open_id, "
                "next_name, handoff_at, reserved_until, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(cluster) DO UPDATE SET current_open_id=excluded.current_open_id, "
                "current_name=excluded.current_name, next_open_id=excluded.next_open_id, "
                "next_name=excluded.next_name, handoff_at=excluded.handoff_at, "
                "reserved_until=excluded.reserved_until, updated_at=excluded.updated_at",
                (
                    reservation.cluster,
                    reservation.current_open_id,
                    reservation.current_name,
                    reservation.next_open_id,
                    reservation.next_name,
                    _iso(reservation.handoff_at),
                    _iso(reserved_until),
                    _iso(_now()),
                ),
            )

    def get_reservation(self, cluster: str) -> Reservation:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reservations WHERE cluster = ?", (cluster,)
            ).fetchone()
        if row is None:
            return Reservation(cluster=cluster)
        return Reservation(
            cluster=row["cluster"],
            current_open_id=row["current_open_id"],
            current_name=row["current_name"] or "",
            next_open_id=row["next_open_id"],
            next_name=row["next_name"] or "",
            handoff_at=_parse_opt(row["handoff_at"]),
            reserved_until=_parse_opt(row["reserved_until"]),
        )

    def all_reservations(self) -> list[Reservation]:
        with self._lock:
            rows = self._conn.execute("SELECT cluster FROM reservations").fetchall()
        return [self.get_reservation(row["cluster"]) for row in rows]

    def extend_reservation(self, cluster: str, minutes: int) -> datetime:
        """延长保留。只改到期时间，绝不触碰机器。"""
        expires_at = _now() + timedelta(minutes=minutes)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO reservations (cluster, reserved_until, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cluster) DO UPDATE SET reserved_until=excluded.reserved_until, "
                "updated_at=excluded.updated_at",
                (cluster, _iso(expires_at), _iso(_now())),
            )
        return expires_at

    def release_reservation(self, cluster: str) -> None:
        """把预约标记为已释放。只改预约数据，不操作机器。"""
        with self._tx() as conn:
            conn.execute(
                "UPDATE reservations SET current_open_id = NULL, current_name = '', "
                "reserved_until = NULL, handoff_at = NULL, updated_at = ? WHERE cluster = ?",
                (_iso(_now()), cluster),
            )

    # ------------------------------------------------------------------
    # 卡片
    # ------------------------------------------------------------------
    def create_card(self, *, cluster: str, current_open_id: str, reason: str) -> str:
        """新建确认卡片并使该集群此前的卡片全部失效（旧卡片不能重复操作）。"""
        token = f"{cluster}-{_now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        with self._tx() as conn:
            conn.execute(
                "UPDATE cards SET status = 'superseded', resolved_at = ? "
                "WHERE cluster = ? AND status = 'pending'",
                (_iso(_now()), cluster),
            )
            conn.execute(
                "INSERT INTO cards (card_token, cluster, current_open_id, status, reason, created_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (token, cluster, current_open_id, reason, _iso(_now())),
            )
        return token

    def active_card_token(self, cluster: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT card_token FROM cards WHERE cluster = ? AND status = 'pending' "
                "ORDER BY created_at DESC LIMIT 1",
                (cluster,),
            ).fetchone()
        return row["card_token"] if row else None

    def get_card(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cards WHERE card_token = ?", (token,)
            ).fetchone()
        if row is None:
            return None
        return {
            "card_token": row["card_token"],
            "cluster": row["cluster"],
            "current_open_id": row["current_open_id"],
            "status": row["status"],
            "reason": row["reason"],
            "created_at": _parse_opt(row["created_at"]),
            "message_id": row["message_id"],
            "reminded_at": _parse_opt(row["reminded_at"]),
            "escalated_at": _parse_opt(row["escalated_at"]),
            "resolved_at": _parse_opt(row["resolved_at"]),
            "resolution": row["resolution"],
        }

    def attach_card_message(self, token: str, message_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE cards SET message_id = ? WHERE card_token = ?", (message_id, token)
            )

    def resolve_card(self, token: str, resolution: str) -> bool:
        """结束卡片。只有 pending 的卡片能被结束，返回 False 表示重复点击/已过期。"""
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE cards SET status = 'resolved', resolved_at = ?, resolution = ? "
                "WHERE card_token = ? AND status = 'pending'",
                (_iso(_now()), resolution, token),
            )
            return cursor.rowcount > 0

    def pending_cards(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT card_token FROM cards WHERE status = 'pending'"
            ).fetchall()
        return [card for card in (self.get_card(row["card_token"]) for row in rows) if card]

    def mark_card_reminded(self, token: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE cards SET reminded_at = ? WHERE card_token = ? AND reminded_at IS NULL",
                (_iso(_now()), token),
            )

    def mark_card_escalated(self, token: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE cards SET escalated_at = ? WHERE card_token = ? AND escalated_at IS NULL",
                (_iso(_now()), token),
            )

    # ------------------------------------------------------------------
    # 集群注册表
    # ------------------------------------------------------------------
    @staticmethod
    def _cluster_row(row: sqlite3.Row) -> ClusterConfig:
        return ClusterConfig(
            cluster=row["cluster"],
            name=row["name"] or "",
            enabled=bool(row["enabled"]),
            managed=bool(row["managed"]),
            mode=CollectMode(row["mode"]),
            data_dir=row["data_dir"],
            idle_minutes=row["idle_minutes"],
            sample_seconds=row["sample_seconds"],
            confirm_samples=row["confirm_samples"],
            command_timeout=row["command_timeout"],
            sim_pattern=row["sim_pattern"],
            proxy_pattern=row["proxy_pattern"],
            tester_pattern=row["tester_pattern"],
            extra_test_processes=row["extra_test_processes"] or "",
            ssh_host=row["ssh_host"] or "",
            ssh_port=row["ssh_port"],
            ssh_user=row["ssh_user"] or "",
            ssh_key_path=row["ssh_key_path"] or "",
            ssh_jump_host=row["ssh_jump_host"] or "",
            ssh_jump_port=row["ssh_jump_port"],
            ssh_jump_user=row["ssh_jump_user"] or "",
            ssh_jump_key_path=row["ssh_jump_key_path"] or "",
            ssh_python=row["ssh_python"] or "python3",
            ssh_use_sudo=bool(row["ssh_use_sudo"]),
            webhook_url=row["webhook_url"] or "",
            note=row["note"] or "",
            updated_at=_parse_opt(row["updated_at"]),
            last_seen_at=_parse_opt(row["last_seen_at"]),
            last_error=row["last_error"] or "",
        )

    def save_cluster(self, config: ClusterConfig, *, actor: str = "") -> ClusterConfig:
        config.validate()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO clusters (cluster, name, enabled, managed, mode, data_dir, "
                "idle_minutes, sample_seconds, confirm_samples, command_timeout, sim_pattern, "
                "proxy_pattern, tester_pattern, extra_test_processes, ssh_host, ssh_port, "
                "ssh_user, ssh_key_path, ssh_jump_host, ssh_jump_port, ssh_jump_user, "
                "ssh_jump_key_path, ssh_python, ssh_use_sudo, webhook_url, note, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(cluster) DO UPDATE SET name=excluded.name, enabled=excluded.enabled, "
                "managed=excluded.managed, mode=excluded.mode, data_dir=excluded.data_dir, "
                "idle_minutes=excluded.idle_minutes, sample_seconds=excluded.sample_seconds, "
                "confirm_samples=excluded.confirm_samples, command_timeout=excluded.command_timeout, "
                "sim_pattern=excluded.sim_pattern, proxy_pattern=excluded.proxy_pattern, "
                "tester_pattern=excluded.tester_pattern, "
                "extra_test_processes=excluded.extra_test_processes, ssh_host=excluded.ssh_host, "
                "ssh_port=excluded.ssh_port, ssh_user=excluded.ssh_user, "
                "ssh_key_path=excluded.ssh_key_path, ssh_jump_host=excluded.ssh_jump_host, "
                "ssh_jump_port=excluded.ssh_jump_port, ssh_jump_user=excluded.ssh_jump_user, "
                "ssh_jump_key_path=excluded.ssh_jump_key_path, ssh_python=excluded.ssh_python, "
                "ssh_use_sudo=excluded.ssh_use_sudo, webhook_url=excluded.webhook_url, "
                "note=excluded.note, updated_at=excluded.updated_at",
                (
                    config.cluster, config.name, int(config.enabled), int(config.managed),
                    config.mode.value, config.data_dir, config.idle_minutes,
                    config.sample_seconds, config.confirm_samples, config.command_timeout,
                    config.sim_pattern, config.proxy_pattern, config.tester_pattern,
                    config.extra_test_processes, config.ssh_host, config.ssh_port,
                    config.ssh_user, config.ssh_key_path, config.ssh_jump_host,
                    config.ssh_jump_port, config.ssh_jump_user, config.ssh_jump_key_path,
                    config.ssh_python,
                    int(config.ssh_use_sudo), config.webhook_url, config.note, _iso(_now()),
                ),
            )
        self.log_admin(actor=actor, action="save_cluster", target=config.cluster,
                       detail=f"mode={config.mode.value} enabled={config.enabled}")
        return self.get_cluster(config.cluster)

    def get_cluster(self, cluster: str) -> ClusterConfig | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM clusters WHERE cluster = ?", (cluster,)
            ).fetchone()
        return self._cluster_row(row) if row else None

    def list_clusters(self, *, only_enabled: bool = False) -> list[ClusterConfig]:
        sql = "SELECT * FROM clusters"
        if only_enabled:
            sql += " WHERE enabled = 1"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        configs = [self._cluster_row(row) for row in rows]
        # 数字编号按数值排，其余按字符串排在后面
        return sorted(
            configs,
            key=lambda c: (0, f"{int(c.cluster):09d}") if c.cluster.isdigit() else (1, c.cluster),
        )

    def delete_cluster(self, cluster: str, *, actor: str = "") -> bool:
        """从纳管范围移除。历史事件和预约保留，便于事后追溯。"""
        with self._tx() as conn:
            deleted = conn.execute("DELETE FROM clusters WHERE cluster = ?", (cluster,)).rowcount
        if deleted:
            self.log_admin(actor=actor, action="delete_cluster", target=cluster,
                           detail="仅移出纳管，历史数据保留")
        return bool(deleted)

    def mark_cluster_seen(self, cluster: str, *, error: str = "") -> None:
        """记录最近一次采集结果。SSH 拉取失败时把原因写在这里供后台展示。"""
        with self._tx() as conn:
            conn.execute(
                "UPDATE clusters SET last_seen_at = ?, last_error = ? WHERE cluster = ?",
                (_iso(_now()), error, cluster),
            )

    # ------------------------------------------------------------------
    # 运行时配置
    # ------------------------------------------------------------------
    def set_setting(self, key: str, value: Any, *, actor: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at, updated_by) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (key, json.dumps(value, ensure_ascii=False), _iso(_now()), actor),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def all_settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM app_settings").fetchall()
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                continue
        return out

    # ------------------------------------------------------------------
    # 飞书通讯录
    # ------------------------------------------------------------------
    def upsert_org_users(self, users: list[OrgUser]) -> int:
        """整批写入。保留本地设置的管理员标记，不被同步覆盖。"""
        now = _iso(_now())
        with self._tx() as conn:
            for user in users:
                existing = conn.execute(
                    "SELECT is_admin FROM org_users WHERE open_id = ?", (user.open_id,)
                ).fetchone()
                is_admin = bool(existing["is_admin"]) if existing else user.is_admin
                conn.execute(
                    "INSERT INTO org_users (open_id, name, en_name, email, mobile, avatar_url, "
                    "department_ids, department_names, is_admin, is_active, synced_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(open_id) DO UPDATE SET name=excluded.name, "
                    "en_name=excluded.en_name, email=excluded.email, mobile=excluded.mobile, "
                    "avatar_url=excluded.avatar_url, department_ids=excluded.department_ids, "
                    "department_names=excluded.department_names, is_active=excluded.is_active, "
                    "synced_at=excluded.synced_at",
                    (
                        user.open_id, user.name, user.en_name, user.email, user.mobile,
                        user.avatar_url,
                        json.dumps(list(user.department_ids), ensure_ascii=False),
                        json.dumps(list(user.department_names), ensure_ascii=False),
                        int(is_admin), int(user.is_active), now,
                    ),
                )
        return len(users)

    def upsert_org_departments(self, departments: list[OrgDepartment]) -> int:
        now = _iso(_now())
        with self._tx() as conn:
            for item in departments:
                conn.execute(
                    "INSERT INTO org_departments (department_id, name, parent_id, member_count, "
                    "synced_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(department_id) DO UPDATE SET name=excluded.name, "
                    "parent_id=excluded.parent_id, member_count=excluded.member_count, "
                    "synced_at=excluded.synced_at",
                    (item.department_id, item.name, item.parent_id, item.member_count, now),
                )
        return len(departments)

    @staticmethod
    def _org_user_row(row: sqlite3.Row) -> OrgUser:
        return OrgUser(
            open_id=row["open_id"],
            name=row["name"] or "",
            en_name=row["en_name"] or "",
            email=row["email"] or "",
            mobile=row["mobile"] or "",
            avatar_url=row["avatar_url"] or "",
            department_ids=tuple(json.loads(row["department_ids"] or "[]")),
            department_names=tuple(json.loads(row["department_names"] or "[]")),
            is_admin=bool(row["is_admin"]),
            is_active=bool(row["is_active"]),
            synced_at=_parse_opt(row["synced_at"]),
        )

    def get_org_user(self, open_id: str) -> OrgUser | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM org_users WHERE open_id = ?", (open_id,)
            ).fetchone()
        return self._org_user_row(row) if row else None

    def search_org_users(self, keyword: str = "", *, limit: int = 50,
                         admins_only: bool = False) -> list[OrgUser]:
        sql = "SELECT * FROM org_users WHERE is_active = 1"
        params: list[Any] = []
        if admins_only:
            sql += " AND is_admin = 1"
        if keyword:
            sql += " AND (name LIKE ? OR en_name LIKE ? OR email LIKE ?)"
            like = f"%{keyword}%"
            params.extend([like, like, like])
        sql += " ORDER BY name LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._org_user_row(row) for row in rows]

    def set_admin(self, open_id: str, is_admin: bool, *, actor: str = "") -> bool:
        with self._tx() as conn:
            changed = conn.execute(
                "UPDATE org_users SET is_admin = ? WHERE open_id = ?",
                (int(is_admin), open_id),
            ).rowcount
        if changed:
            self.log_admin(actor=actor, action="set_admin", target=open_id,
                           detail=f"is_admin={is_admin}")
        return bool(changed)

    def admin_open_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT open_id FROM org_users WHERE is_admin = 1 AND is_active = 1"
            ).fetchall()
        return [row["open_id"] for row in rows]

    def org_stats(self) -> dict[str, Any]:
        with self._lock:
            users = self._conn.execute(
                "SELECT COUNT(*) AS n, MAX(synced_at) AS last FROM org_users"
            ).fetchone()
            departments = self._conn.execute(
                "SELECT COUNT(*) AS n FROM org_departments"
            ).fetchone()
            admins = self._conn.execute(
                "SELECT COUNT(*) AS n FROM org_users WHERE is_admin = 1"
            ).fetchone()
        return {
            "users": users["n"],
            "departments": departments["n"],
            "admins": admins["n"],
            "last_synced_at": _parse_opt(users["last"]),
        }

    # ------------------------------------------------------------------
    # 后台审计
    # ------------------------------------------------------------------
    def log_admin(self, *, actor: str, action: str, target: str = "", detail: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO admin_audit (id, actor, action, target, detail, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (uuid.uuid4().hex, actor, action, target, detail, _iso(_now())),
            )

    def recent_admin_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM admin_audit ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "actor": row["actor"], "action": row["action"], "target": row["target"],
                "detail": row["detail"], "created_at": _parse_opt(row["created_at"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 时段预约
    # ------------------------------------------------------------------
    @staticmethod
    def _booking_row(row: sqlite3.Row) -> Booking:
        return Booking(
            id=row["id"],
            cluster=row["cluster"],
            open_id=row["open_id"],
            name=row["name"] or "",
            start_at=parse_datetime(row["start_at"]),
            end_at=parse_datetime(row["end_at"]),
            status=BookingStatus(row["status"]),
            created_at=parse_datetime(row["created_at"]),
            note=row["note"] or "",
            extended_to=_parse_opt(row["extended_to"]),
        )

    def create_booking(
        self, request: BookingRequest, *, now: datetime | None = None, **limits: Any
    ) -> Booking:
        """校验 + 冲突检测 + 落库，全程在一个事务里，避免两人同时抢同一时段。"""
        now = now or _now()
        checked = validate(request, now=now, **limits)
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM bookings WHERE cluster = ? AND status IN ('booked','active') "
                "AND end_at > ? AND start_at < ?",
                (
                    checked.cluster,
                    _iso(checked.start_at),
                    _iso(checked.end_at),
                ),
            ).fetchall()
            existing = [self._booking_row(row) for row in rows]
            conflicts = find_conflicts(checked, existing, now=now)
            if conflicts:
                raise BookingError(
                    f"{checked.cluster} 集群这个时段已被占用：{describe_conflict(conflicts)}"
                )

            booking = Booking(
                id=uuid.uuid4().hex[:16],
                cluster=checked.cluster,
                open_id=checked.open_id,
                name=checked.name,
                start_at=checked.start_at,
                end_at=checked.end_at,
                status=BookingStatus.BOOKED,
                created_at=now,
                note=checked.note,
            )
            conn.execute(
                "INSERT INTO bookings (id, cluster, open_id, name, start_at, end_at, "
                "status, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    booking.id, booking.cluster, booking.open_id, booking.name,
                    _iso(booking.start_at), _iso(booking.end_at),
                    booking.status.value, booking.note, _iso(booking.created_at),
                ),
            )
        return booking

    def get_booking(self, booking_id: str) -> Booking | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()
        return self._booking_row(row) if row else None

    def list_bookings(
        self,
        *,
        cluster: str | None = None,
        open_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        include_inactive: bool = False,
    ) -> list[Booking]:
        sql = "SELECT * FROM bookings WHERE 1=1"
        params: list[Any] = []
        if cluster:
            sql += " AND cluster = ?"
            params.append(cluster)
        if open_id:
            sql += " AND open_id = ?"
            params.append(open_id)
        if not include_inactive:
            sql += " AND status IN ('booked','active')"
        if since:
            sql += " AND end_at > ?"
            params.append(_iso(since))
        if until:
            sql += " AND start_at < ?"
            params.append(_iso(until))
        sql += " ORDER BY start_at"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._booking_row(row) for row in rows]

    def cancel_booking(
        self, booking_id: str, *, operator_open_id: str, is_admin: bool = False
    ) -> Booking:
        """取消预约。只有本人或管理员能取消；不影响机器，只改预约数据。"""
        booking = self.get_booking(booking_id)
        if booking is None:
            raise BookingError("找不到这条预约")
        if booking.open_id != operator_open_id and not is_admin:
            raise BookingError("只能取消自己的预约")
        if booking.status in (BookingStatus.CANCELLED, BookingStatus.RELEASED):
            raise BookingError("这条预约已经取消过了")
        with self._tx() as conn:
            conn.execute(
                "UPDATE bookings SET status = ?, cancelled_at = ?, cancelled_by = ? WHERE id = ?",
                (BookingStatus.CANCELLED.value, _iso(_now()), operator_open_id, booking_id),
            )
        return self.get_booking(booking_id)

    def release_active_booking(self, cluster: str, *, now: datetime | None = None) -> Booking | None:
        """占用人点「已使用完」：把当前这条预约提前结束。只改预约，不动机器。"""
        now = now or _now()
        active = self.active_booking(cluster, now=now)
        if active is None:
            return None
        with self._tx() as conn:
            conn.execute(
                "UPDATE bookings SET status = ?, end_at = ?, extended_to = NULL WHERE id = ?",
                (BookingStatus.RELEASED.value, _iso(now), active.id),
            )
        return self.get_booking(active.id)

    def extend_active_booking(
        self, cluster: str, minutes: int, *, now: datetime | None = None
    ) -> tuple[Booking | None, Booking | None]:
        """延长当前预约。返回 (延长后的预约, 挡路的下一条预约)。

        下一位已经约了紧邻时段时不强行挤占：只延长到对方开始为止，
        并把挡路的那条返回给上层，好在卡片上如实告诉用户。
        """
        now = now or _now()
        active = self.active_booking(cluster, now=now)
        if active is None:
            return None, None
        target = active.effective_end + timedelta(minutes=minutes)
        upcoming = self.booking_after(
            cluster, active.effective_end, exclude_id=active.id
        )
        blocker = None
        if upcoming is not None and upcoming.start_at < target:
            target = upcoming.start_at
            blocker = upcoming
        if target <= active.effective_end:
            return active, blocker  # 一分钟都挤不出来
        with self._tx() as conn:
            conn.execute(
                "UPDATE bookings SET extended_to = ? WHERE id = ?", (_iso(target), active.id)
            )
        return self.get_booking(active.id), blocker

    def active_booking(self, cluster: str, *, now: datetime | None = None) -> Booking | None:
        now = now or _now()
        for booking in self.list_bookings(cluster=cluster, since=now - timedelta(days=1)):
            if booking.live_status(now) is BookingStatus.ACTIVE:
                return booking
        return None

    def booking_after(
        self, cluster: str, moment: datetime, *, exclude_id: str | None = None
    ) -> Booking | None:
        """某时刻之后的第一条有效预约。

        与 next_booking 的区别：这里只问「时间上谁排在后面」，
        用于续期时判断会不会挤占别人；不关心此刻谁在用。
        """
        for booking in self.list_bookings(cluster=cluster, since=moment):
            if exclude_id and booking.id == exclude_id:
                continue
            if booking.start_at >= moment:
                return booking
        return None

    def next_booking(self, cluster: str, *, now: datetime | None = None) -> Booking | None:
        """下一位等待者。

        必须排除此刻正在进行的那条：交接时刻整点上，接班人的预约刚好开始，
        若不排除，同一个人会同时被算成「当前占用人」和「下一位」，
        引擎就会通知他「集群可用了」——而他本来就是当前占用人。
        """
        now = now or _now()
        for booking in self.list_bookings(cluster=cluster, since=now):
            if booking.covers(now):
                continue
            if booking.start_at >= now and booking.live_status(now).occupies:
                return booking
        return None

    def expire_bookings(self, *, now: datetime | None = None) -> int:
        """把已经结束的预约落库成 done，便于统计与清理。"""
        now = now or _now()
        with self._tx() as conn:
            return conn.execute(
                "UPDATE bookings SET status = 'done' WHERE status IN ('booked','active') "
                "AND COALESCE(extended_to, end_at) <= ?",
                (_iso(now),),
            ).rowcount

    def derive_reservation(self, cluster: str, *, now: datetime | None = None) -> Reservation:
        """把时段预约翻译成判定引擎认识的 Reservation。

        这是预约与监控之间唯一的接缝：引擎只认 current / next / handoff_at，
        因此换成正式预约 API 时只要能产出同样的结构，引擎完全不用动。
        """
        now = now or _now()
        active = self.active_booking(cluster, now=now)
        upcoming = self.next_booking(cluster, now=now)
        return Reservation(
            cluster=cluster,
            current_open_id=active.open_id if active else None,
            current_name=active.name if active else "",
            next_open_id=upcoming.open_id if upcoming else None,
            next_name=upcoming.name if upcoming else "",
            # 交接时间 = 当前预约的结束时间，到点即触发交接确认
            handoff_at=active.effective_end if active else None,
            # 卡片续期写在 extended_to 上，这里不再重复表达保留
            reserved_until=None,
        )

    # ------------------------------------------------------------------
    # 回调幂等
    # ------------------------------------------------------------------
    def claim_callback(self, event_id: str) -> bool:
        """首次见到该回调返回 True；重试返回 False（调用方应回放首次结果）。"""
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO callbacks (event_id, created_at) VALUES (?, ?)",
                (event_id, _iso(_now())),
            )
            return cursor.rowcount > 0

    def save_callback_result(self, event_id: str, result: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE callbacks SET result_json = ? WHERE event_id = ?",
                (json.dumps(result, ensure_ascii=False, default=str), event_id),
            )

    def get_callback_result(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM callbacks WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["result_json"])
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------------
    # 审计与未恢复事件
    # ------------------------------------------------------------------
    def log_notification(
        self,
        *,
        cluster: str,
        kind: str,
        target_open_id: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        note: str = "",
        delivered: bool = False,
        error: str | None = None,
    ) -> str:
        row_id = uuid.uuid4().hex
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO notifications (id, cluster, kind, target_open_id, chat_id, "
                "message_id, note, delivered, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    cluster,
                    kind,
                    target_open_id,
                    chat_id,
                    message_id,
                    note,
                    1 if delivered else 0,
                    error,
                    _iso(_now()),
                ),
            )
        return row_id

    def recent_notifications(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "cluster": row["cluster"],
                "kind": row["kind"],
                "target_open_id": row["target_open_id"],
                "note": row["note"],
                "delivered": bool(row["delivered"]),
                "error": row["error"],
                "created_at": _parse_opt(row["created_at"]),
            }
            for row in rows
        ]

    def open_incident(self, *, cluster: str, kind: str, detail: str = "") -> str:
        """同集群同类型的未恢复事件只保留一条，避免大盘被刷屏。"""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT id FROM incidents WHERE cluster = ? AND kind = ? AND resolved_at IS NULL",
                (cluster, kind),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE incidents SET detail = ? WHERE id = ?", (detail, row["id"])
                )
                return row["id"]
            row_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO incidents (id, cluster, kind, detail, opened_at) VALUES (?,?,?,?,?)",
                (row_id, cluster, kind, detail, _iso(_now())),
            )
            return row_id

    def resolve_incidents(self, *, cluster: str, kind: str | None = None) -> int:
        sql = "UPDATE incidents SET resolved_at = ? WHERE cluster = ? AND resolved_at IS NULL"
        params: tuple[Any, ...] = (_iso(_now()), cluster)
        if kind:
            sql += " AND kind = ?"
            params += (kind,)
        with self._tx() as conn:
            return conn.execute(sql, params).rowcount

    def open_incidents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM incidents WHERE resolved_at IS NULL ORDER BY opened_at DESC"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "cluster": row["cluster"],
                "kind": row["kind"],
                "detail": row["detail"],
                "opened_at": _parse_opt(row["opened_at"]),
            }
            for row in rows
        ]
