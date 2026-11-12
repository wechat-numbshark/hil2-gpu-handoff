"""飞书通讯录同步。

按参考项目 ``feishuOrgSyncService`` 的做法：从根部门广度优先遍历，
再逐个部门拉成员。用到的接口：

* ``/contact/v3/departments``            部门列表（分页）
* ``/contact/v3/users/find_by_department`` 部门成员（分页）
* ``/tenant/v2/tenant/query``            企业信息（可选，缺权限时降级）

同步只**新增和更新**，不删人：飞书里离职的人在本地标记为不活跃，
历史预约仍然能显示出是谁约的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .feishu.client import FeishuClient, FeishuError
from .registry import OrgDepartment, OrgUser
from .store import Store

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
# 防御性上限：部门树异常时不至于把服务拖死
MAX_DEPARTMENTS = 2000
MAX_USERS = 20000


class OrgSyncError(RuntimeError):
    """同步失败。错误信息展示给管理员，需要能指导下一步。"""


@dataclass
class SyncResult:
    departments: int = 0
    users: int = 0
    admins_kept: int = 0
    tenant_name: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "departments": self.departments,
            "users": self.users,
            "admins_kept": self.admins_kept,
            "tenant_name": self.tenant_name,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": (
                int((self.finished_at - self.started_at).total_seconds() * 1000)
                if self.started_at and self.finished_at else None
            ),
            "warnings": list(self.warnings),
        }


class OrgSyncService:
    def __init__(self, *, client: FeishuClient, store: Store) -> None:
        self.client = client
        self.store = store

    # ------------------------------------------------------------------
    def _paged(self, path: str, params: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
        """分页拉取，直到 has_more 为假或达到上限。"""
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            query = {**params, "page_size": PAGE_SIZE}
            if page_token:
                query["page_token"] = page_token
            payload = self.client._request("GET", path, params=query)  # noqa: SLF001
            data = payload.get("data") or {}
            items.extend(data.get("items") or [])
            if len(items) >= limit:
                logger.warning("%s 返回条数超过上限 %s，已截断", path, limit)
                return items[:limit]
            if not data.get("has_more"):
                return items
            page_token = data.get("page_token")
            if not page_token:
                return items

    def fetch_departments(self) -> list[OrgDepartment]:
        """从根部门开始广度优先遍历整棵树。"""
        found: dict[str, OrgDepartment] = {}
        queue: list[str] = ["0"]
        visited: set[str] = set()

        while queue and len(found) < MAX_DEPARTMENTS:
            parent = queue.pop(0)
            if parent in visited:
                continue
            visited.add(parent)
            try:
                children = self._paged(
                    "/contact/v3/departments",
                    {
                        "department_id_type": "open_department_id",
                        "user_id_type": "open_id",
                        "parent_department_id": parent,
                    },
                    limit=MAX_DEPARTMENTS,
                )
            except FeishuError as exc:
                raise OrgSyncError(f"获取部门失败：{exc}") from exc

            for item in children:
                dept_id = str(item.get("open_department_id") or item.get("department_id") or "")
                if not dept_id or dept_id in found:
                    continue
                found[dept_id] = OrgDepartment(
                    department_id=dept_id,
                    name=str(item.get("name", "")),
                    parent_id=str(item.get("parent_department_id", "")),
                    member_count=int(item.get("member_count") or 0),
                )
                queue.append(dept_id)
        return list(found.values())

    def fetch_members(self, departments: Iterable[OrgDepartment]) -> list[OrgUser]:
        """逐个部门拉成员。同一个人可能属于多个部门，按 open_id 合并。"""
        by_open_id: dict[str, OrgUser] = {}
        dept_names = {dept.department_id: dept.name for dept in departments}
        # 根部门要单独查一次，直属于公司的人不在任何子部门里
        targets = ["0"] + [dept.department_id for dept in departments]

        for dept_id in targets:
            if len(by_open_id) >= MAX_USERS:
                break
            params: dict[str, Any] = {"department_id": dept_id, "user_id_type": "open_id"}
            if dept_id != "0":
                params["department_id_type"] = "open_department_id"
            try:
                members = self._paged(
                    "/contact/v3/users/find_by_department", params, limit=MAX_USERS
                )
            except FeishuError as exc:
                # 单个部门读不到不该让整次同步失败，记下来继续
                logger.warning("部门 %s 成员获取失败：%s", dept_id, exc)
                continue

            for item in members:
                open_id = str(item.get("open_id", ""))
                if not open_id:
                    continue
                existing = by_open_id.get(open_id)
                dept_ids = tuple(str(d) for d in (item.get("department_ids") or []))
                names = tuple(
                    dept_names[d] for d in dept_ids if d in dept_names
                ) or ((dept_names.get(dept_id, ""),) if dept_id != "0" else ())
                if existing:
                    merged = tuple(dict.fromkeys(existing.department_names + names))
                    by_open_id[open_id] = OrgUser(
                        **{**existing.__dict__, "department_names": merged}
                    )
                    continue
                by_open_id[open_id] = OrgUser(
                    open_id=open_id,
                    name=str(item.get("name", "")),
                    en_name=str(item.get("en_name", "")),
                    email=str(item.get("email") or item.get("enterprise_email") or ""),
                    mobile=str(item.get("mobile", "")),
                    avatar_url=str((item.get("avatar") or {}).get("avatar_240", "")),
                    department_ids=dept_ids,
                    department_names=tuple(name for name in names if name),
                    is_active=str(item.get("status", {}).get("is_activated", True)) != "False",
                )
        return list(by_open_id.values())

    def fetch_tenant_name(self) -> tuple[str, str]:
        """企业名称。缺 tenant:tenant:readonly 权限时降级，不阻断同步。"""
        try:
            payload = self.client._request("GET", "/tenant/v2/tenant/query")  # noqa: SLF001
            tenant = (payload.get("data") or {}).get("tenant") or {}
            return str(tenant.get("name", "")), ""
        except FeishuError as exc:
            return "", f"未取到企业名称（缺 tenant:tenant:readonly 权限）：{exc}"

    # ------------------------------------------------------------------
    def sync(self, *, actor: str = "") -> SyncResult:
        if not self.client.configured:
            raise OrgSyncError("飞书应用凭据未配置，无法同步通讯录")

        started = datetime.now(timezone.utc)
        warnings: list[str] = []

        departments = self.fetch_departments()
        if not departments:
            warnings.append("没有读到任何部门，请确认应用有 contact:department.base:readonly 权限")

        users = self.fetch_members(departments)
        if not users:
            raise OrgSyncError(
                "没有读到任何成员。请确认应用已开通 contact:user.base:readonly 权限，"
                "并在「数据权限」里勾选了可见的通讯录范围。"
            )

        self.store.upsert_org_departments(departments)
        self.store.upsert_org_users(users)

        tenant_name, tenant_warning = self.fetch_tenant_name()
        if tenant_warning:
            warnings.append(tenant_warning)

        result = SyncResult(
            departments=len(departments),
            users=len(users),
            admins_kept=len(self.store.admin_open_ids()),
            tenant_name=tenant_name,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            warnings=tuple(warnings),
        )
        self.store.set_setting("org_last_sync", result.as_dict(), actor=actor)
        self.store.log_admin(
            actor=actor, action="org_sync", target=tenant_name or "-",
            detail=f"部门 {result.departments} 个，成员 {result.users} 人",
        )
        logger.info("通讯录同步完成：%s 部门 / %s 人", result.departments, result.users)
        return result

    def check(self) -> dict[str, Any]:
        """后台「测试连接」：只探一层部门，快速验证凭据和权限。"""
        if not self.client.configured:
            return {"ok": False, "error": "飞书应用凭据未配置"}
        try:
            items = self._paged(
                "/contact/v3/departments",
                {"department_id_type": "open_department_id", "parent_department_id": "0"},
                limit=PAGE_SIZE,
            )
        except FeishuError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "root_departments": len(items),
            "sample": [str(item.get("name", "")) for item in items[:5]],
        }
