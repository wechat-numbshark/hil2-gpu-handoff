"""集群注册表与运行时配置。

原来纳管范围写在环境变量 ``MANAGED_CLUSTERS`` 里，改配置要重启服务。
现在集群、采集方式、阈值都存库，管理员在后台改完立即生效。

采集有两种模式，可以逐台选择：

* ``agent_push``  集群上常驻 agent，主动把事件推给协调服务和监控群。
                  实时性最好，但需要在每台集群上装东西。
* ``ssh_pull``    协调服务定时 ssh 过去执行同一份采集脚本。
                  集群上**什么都不用装**，代价是要给一把只读 ssh 钥匙。

两种模式产出完全相同的 ``hil.monitor.v1`` 事件，后续链路无差别。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# 默认容器识别规则。现场确认真实容器名后应在后台改成精确匹配。
DEFAULT_PATTERNS = {
    "sim": r"(^|[-_])sim(ulator)?($|[-_])",
    "proxy": r"(^|[-_])proxy($|[-_])",
    "tester": r"tester|autotest|simlib|logsim",
}


class CollectMode(str, Enum):
    AGENT_PUSH = "agent_push"
    SSH_PULL = "ssh_pull"
    DISABLED = "disabled"

    @property
    def label(self) -> str:
        return {
            CollectMode.AGENT_PUSH: "Agent 主动推送",
            CollectMode.SSH_PULL: "SSH 定时拉取",
            CollectMode.DISABLED: "暂停采集",
        }[self]


class RegistryError(ValueError):
    """集群配置不合法。错误信息直接展示给管理员，必须说清怎么改。"""


@dataclass
class ClusterConfig:
    """一台集群的完整监控配置。"""

    cluster: str
    name: str = ""
    enabled: bool = True
    managed: bool = True
    mode: CollectMode = CollectMode.AGENT_PUSH

    # ---- 采集参数（两种模式共用）----
    data_dir: str = "/data1"
    idle_minutes: int = 15
    sample_seconds: int = 300
    confirm_samples: int = 3
    command_timeout: int = 30
    sim_pattern: str = DEFAULT_PATTERNS["sim"]
    proxy_pattern: str = DEFAULT_PATTERNS["proxy"]
    tester_pattern: str = DEFAULT_PATTERNS["tester"]
    extra_test_processes: str = ""

    # ---- SSH 拉取模式专用 ----
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_key_path: str = ""
    # 客户网络可选：协调服务先登录堡垒机，再转到 GPU 服务器。
    ssh_jump_host: str = ""
    ssh_jump_port: int = 22
    ssh_jump_user: str = ""
    ssh_jump_key_path: str = ""
    ssh_python: str = "python3"
    # 需要 sudo 才能读 docker 时填 true；配合最小 sudo 规则使用
    ssh_use_sudo: bool = False

    # ---- 事件去向 ----
    # 每台可以有自己的监控群 webhook；留空则用全局配置
    webhook_url: str = ""

    note: str = ""
    updated_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_error: str = ""

    # ------------------------------------------------------------------
    def validate(self) -> "ClusterConfig":
        if not self.cluster or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", self.cluster):
            raise RegistryError("集群编号只能用字母、数字、下划线、点和短横，且不超过 32 个字符")
        if not self.data_dir.startswith("/"):
            raise RegistryError("数据目录必须是绝对路径，例如 /data1")
        for label, value, low, high in (
            ("无活动判定窗口", self.idle_minutes, 1, 240),
            ("采集间隔", self.sample_seconds, 30, 3600),
            ("确认轮数", self.confirm_samples, 1, 20),
            ("命令超时", self.command_timeout, 5, 300),
        ):
            if not low <= value <= high:
                raise RegistryError(f"{label}应在 {low}～{high} 之间，当前是 {value}")

        for label, pattern in (
            ("sim", self.sim_pattern), ("proxy", self.proxy_pattern), ("tester", self.tester_pattern)
        ):
            if not pattern.strip():
                raise RegistryError(f"{label} 容器匹配规则不能为空，否则会一直判为采集异常")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise RegistryError(f"{label} 容器匹配规则不是合法正则：{exc}") from exc

        if self.mode is CollectMode.SSH_PULL:
            if not self.ssh_host:
                raise RegistryError("SSH 拉取模式必须填写主机地址")
            if not 1 <= self.ssh_port <= 65535:
                raise RegistryError("SSH 端口不合法")
            if self.ssh_key_path and not self.ssh_key_path.startswith("/"):
                raise RegistryError("SSH 私钥路径必须是绝对路径")
            if self.ssh_jump_host:
                if not 1 <= self.ssh_jump_port <= 65535:
                    raise RegistryError("堡垒机 SSH 端口不合法")
                if self.ssh_jump_key_path and not self.ssh_jump_key_path.startswith("/"):
                    raise RegistryError("堡垒机私钥路径必须是绝对路径")
            elif self.ssh_jump_user or self.ssh_jump_key_path:
                raise RegistryError("填写堡垒机用户或私钥前，必须先填写堡垒机主机")
        return self

    # ------------------------------------------------------------------
    def agent_env(self, *, coordinator_url: str, ingest_token: str,
                  bot_open_id: str = "") -> dict[str, str]:
        """生成这台集群的 agent 环境变量。下载安装包和 SSH 拉取共用。"""
        return {
            "HIL_MACHINE_NAME": self.cluster,
            "HIL_DATA_DIR": self.data_dir,
            "HIL_IDLE_MINUTES": str(self.idle_minutes),
            "HIL_SAMPLE_SECONDS": str(self.sample_seconds),
            "HIL_CONFIRM_SAMPLES": str(self.confirm_samples),
            "HIL_COMMAND_TIMEOUT": str(self.command_timeout),
            "HIL_SIM_PATTERN": self.sim_pattern,
            "HIL_PROXY_PATTERN": self.proxy_pattern,
            "HIL_TESTER_PATTERN": self.tester_pattern,
            "HIL_EXTRA_TEST_PROCESSES": self.extra_test_processes,
            "HIL_COORDINATOR_URL": coordinator_url,
            "HIL_INGEST_TOKEN": ingest_token,
            "FEISHU_WEBHOOK": self.webhook_url,
            "HIL_CONSUMER_BOT_OPEN_ID": bot_open_id,
        }

    def as_dict(self, *, redact_secrets: bool = True) -> dict[str, Any]:
        data = {
            "cluster": self.cluster,
            "name": self.name,
            "enabled": self.enabled,
            "managed": self.managed,
            "mode": self.mode.value,
            "mode_label": self.mode.label,
            "data_dir": self.data_dir,
            "idle_minutes": self.idle_minutes,
            "sample_seconds": self.sample_seconds,
            "confirm_samples": self.confirm_samples,
            "command_timeout": self.command_timeout,
            "sim_pattern": self.sim_pattern,
            "proxy_pattern": self.proxy_pattern,
            "tester_pattern": self.tester_pattern,
            "extra_test_processes": self.extra_test_processes,
            "ssh_host": self.ssh_host,
            "ssh_port": self.ssh_port,
            "ssh_user": self.ssh_user,
            "ssh_key_path": self.ssh_key_path,
            "ssh_jump_host": self.ssh_jump_host,
            "ssh_jump_port": self.ssh_jump_port,
            "ssh_jump_user": self.ssh_jump_user,
            "ssh_jump_key_path": self.ssh_jump_key_path,
            "ssh_python": self.ssh_python,
            "ssh_use_sudo": self.ssh_use_sudo,
            "note": self.note,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "last_error": self.last_error,
        }
        # webhook 里带着密钥，后台只显示「已配置」而不回显原值
        data["webhook_configured"] = bool(self.webhook_url)
        data["webhook_url"] = "" if redact_secrets else self.webhook_url
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, base: "ClusterConfig | None" = None) -> "ClusterConfig":
        """从后台表单构造。base 存在时表示部分更新，未提供的字段保持原值。"""
        def pick(key: str, default: Any) -> Any:
            if key in raw and raw[key] is not None and raw[key] != "":
                return raw[key]
            return getattr(base, key) if base else default

        def pick_int(key: str, default: int) -> int:
            try:
                return int(pick(key, default))
            except (TypeError, ValueError) as exc:
                raise RegistryError(f"{key} 必须是整数") from exc

        def pick_bool(key: str, default: bool) -> bool:
            value = raw.get(key)
            if value is None:
                return getattr(base, key) if base else default
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}

        mode_raw = pick("mode", CollectMode.AGENT_PUSH.value)
        # 部分更新时 pick 会取回 base 上已经是枚举的值；
        # Python 3.11 起 str(枚举) 返回 "CollectMode.AGENT_PUSH"，不能直接转回去。
        if isinstance(mode_raw, CollectMode):
            mode = mode_raw
        else:
            try:
                mode = CollectMode(str(mode_raw))
            except ValueError as exc:
                raise RegistryError(f"不支持的采集方式：{mode_raw}") from exc

        cluster = str(raw.get("cluster") or (base.cluster if base else "")).strip()

        config = cls(
            cluster=cluster,
            name=str(pick("name", "")),
            enabled=pick_bool("enabled", True),
            managed=pick_bool("managed", True),
            mode=mode,
            data_dir=str(pick("data_dir", "/data1")),
            idle_minutes=pick_int("idle_minutes", 15),
            sample_seconds=pick_int("sample_seconds", 300),
            confirm_samples=pick_int("confirm_samples", 3),
            command_timeout=pick_int("command_timeout", 30),
            sim_pattern=str(pick("sim_pattern", DEFAULT_PATTERNS["sim"])),
            proxy_pattern=str(pick("proxy_pattern", DEFAULT_PATTERNS["proxy"])),
            tester_pattern=str(pick("tester_pattern", DEFAULT_PATTERNS["tester"])),
            extra_test_processes=str(raw.get("extra_test_processes",
                                             base.extra_test_processes if base else "")),
            ssh_host=str(pick("ssh_host", "")),
            ssh_port=pick_int("ssh_port", 22),
            ssh_user=str(pick("ssh_user", "")),
            ssh_key_path=str(pick("ssh_key_path", "")),
            ssh_jump_host=str(raw.get("ssh_jump_host", base.ssh_jump_host if base else "")),
            ssh_jump_port=pick_int("ssh_jump_port", 22),
            ssh_jump_user=str(raw.get("ssh_jump_user", base.ssh_jump_user if base else "")),
            ssh_jump_key_path=str(raw.get(
                "ssh_jump_key_path", base.ssh_jump_key_path if base else ""
            )),
            ssh_python=str(pick("ssh_python", "python3")),
            ssh_use_sudo=pick_bool("ssh_use_sudo", False),
            webhook_url=str(raw.get("webhook_url", base.webhook_url if base else "")),
            note=str(raw.get("note", base.note if base else "")),
        )
        return config.validate()


@dataclass
class OrgUser:
    """飞书通讯录里的一个人。用于后台选管理员、预约时显示姓名。"""

    open_id: str
    name: str = ""
    en_name: str = ""
    email: str = ""
    mobile: str = ""
    avatar_url: str = ""
    department_ids: tuple[str, ...] = ()
    department_names: tuple[str, ...] = ()
    is_admin: bool = False
    is_active: bool = True
    synced_at: datetime | None = None

    def as_dict(self, *, include_contact: bool = False) -> dict[str, Any]:
        data = {
            "open_id": self.open_id,
            "name": self.name,
            "en_name": self.en_name,
            "avatar_url": self.avatar_url,
            "department_names": list(self.department_names),
            "is_admin": self.is_admin,
            "is_active": self.is_active,
            "synced_at": self.synced_at.isoformat() if self.synced_at else None,
        }
        # 邮箱手机号属于个人信息，只在后台管理员视图里返回
        if include_contact:
            data["email"] = self.email
            data["mobile"] = self.mobile
        return data


@dataclass
class OrgDepartment:
    department_id: str
    name: str = ""
    parent_id: str = ""
    member_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "department_id": self.department_id,
            "name": self.name,
            "parent_id": self.parent_id,
            "member_count": self.member_count,
        }
