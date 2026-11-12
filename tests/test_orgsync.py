"""飞书通讯录同步：分页、合并、降级、不删人。"""

from __future__ import annotations

import pytest

from hil2.feishu.client import FeishuError
from hil2.orgsync import OrgSyncError, OrgSyncService
from hil2.registry import OrgUser


class FakeFeishu:
    """按路径返回预置分页数据的假客户端。"""

    def __init__(self, *, configured=True, departments=None, members=None,
                 tenant_name="星辰汽车", fail_on=None):
        self.configured = configured
        self.departments = departments or {}
        self.members = members or {}
        self.tenant_name = tenant_name
        self.fail_on = fail_on or set()
        self.calls = []

    def _request(self, method, path, *, params=None, **kwargs):
        self.calls.append((path, dict(params or {})))
        if path in self.fail_on:
            raise FeishuError(f"{path} 没有权限")

        if path.endswith("/contact/v3/departments"):
            parent = (params or {}).get("parent_department_id", "0")
            pages = self.departments.get(parent, [[]])
            index = 0 if not (params or {}).get("page_token") else int(params["page_token"])
            items = pages[index] if index < len(pages) else []
            has_more = index + 1 < len(pages)
            return {"code": 0, "data": {"items": items, "has_more": has_more,
                                        "page_token": str(index + 1)}}

        if path.endswith("/users/find_by_department"):
            dept = (params or {}).get("department_id", "0")
            return {"code": 0, "data": {"items": self.members.get(dept, []), "has_more": False}}

        if path.endswith("/tenant/v2/tenant/query"):
            return {"code": 0, "data": {"tenant": {"name": self.tenant_name}}}
        return {"code": 0, "data": {}}


def dept(did, name, parent="0"):
    return {"open_department_id": did, "name": name, "parent_department_id": parent}


def member(open_id, name, dept_ids=(), **extra):
    return {"open_id": open_id, "name": name, "department_ids": list(dept_ids),
            "avatar": {"avatar_240": f"https://avatar/{open_id}"}, **extra}


@pytest.fixture
def service(store):
    def make(client):
        return OrgSyncService(client=client, store=store)
    return make


class TestFetch:
    def test_walks_department_tree(self, service):
        client = FakeFeishu(departments={
            "0": [[dept("d1", "研发中心")]],
            "d1": [[dept("d2", "电控部", "d1")]],
            "d2": [[]],
        })
        assert {d.name for d in service(client).fetch_departments()} == {"研发中心", "电控部"}

    def test_handles_pagination(self, service):
        client = FakeFeishu(departments={
            "0": [[dept("d1", "一部")], [dept("d2", "二部")]],
            "d1": [[]], "d2": [[]],
        })
        assert len(service(client).fetch_departments()) == 2

    def test_cycle_does_not_hang(self, service):
        # 父子互指的脏数据不能让遍历死循环
        client = FakeFeishu(departments={
            "0": [[dept("d1", "A")]], "d1": [[dept("d1", "A")]],
        })
        assert len(service(client).fetch_departments()) == 1

    def test_department_failure_raises(self, service):
        client = FakeFeishu(fail_on={"/contact/v3/departments"})
        with pytest.raises(OrgSyncError, match="部门"):
            service(client).fetch_departments()

    def test_merges_person_across_departments(self, service):
        client = FakeFeishu(
            departments={"0": [[dept("d1", "电控部"), dept("d2", "测试部")]], "d1": [[]], "d2": [[]]},
            members={
                "d1": [member("ou_a", "张三", ["d1", "d2"])],
                "d2": [member("ou_a", "张三", ["d1", "d2"])],
            },
        )
        svc = service(client)
        users = svc.fetch_members(svc.fetch_departments())
        assert len(users) == 1
        assert set(users[0].department_names) == {"电控部", "测试部"}

    def test_single_department_failure_does_not_abort(self, service):
        client = FakeFeishu(
            departments={"0": [[dept("d1", "A"), dept("d2", "B")]], "d1": [[]], "d2": [[]]},
            members={"d1": [member("ou_a", "张三", ["d1"])]},
        )
        svc = service(client)
        users = svc.fetch_members(svc.fetch_departments())
        assert [u.name for u in users] == ["张三"]

    def test_root_level_members_included(self, service):
        client = FakeFeishu(departments={"0": [[]]}, members={"0": [member("ou_x", "直属老板")]})
        svc = service(client)
        assert [u.name for u in svc.fetch_members([])] == ["直属老板"]


class TestSync:
    def _client(self, **overrides):
        base = dict(
            departments={"0": [[dept("d1", "电控部")]], "d1": [[]]},
            members={"d1": [member("ou_a", "张三", ["d1"], email="z@x.com"),
                            member("ou_b", "李四", ["d1"])]},
        )
        base.update(overrides)
        return FakeFeishu(**base)

    def test_writes_to_store(self, store, service):
        result = service(self._client()).sync(actor="ou_admin")
        assert result.users == 2
        assert result.departments == 1
        assert result.tenant_name == "星辰汽车"
        assert {u.name for u in store.search_org_users()} == {"张三", "李四"}

    def test_records_last_sync(self, store, service):
        service(self._client()).sync()
        assert store.get_setting("org_last_sync")["users"] == 2

    def test_audit_logged(self, store, service):
        service(self._client()).sync(actor="ou_admin")
        assert any(e["action"] == "org_sync" for e in store.recent_admin_audit())

    def test_preserves_admin_flags(self, store, service):
        store.upsert_org_users([OrgUser(open_id="ou_a", name="张三")])
        store.set_admin("ou_a", True)
        service(self._client()).sync()
        assert store.get_org_user("ou_a").is_admin is True

    def test_does_not_delete_departed_people(self, store, service):
        store.upsert_org_users([OrgUser(open_id="ou_gone", name="已离职")])
        service(self._client()).sync()
        # 人还在库里，历史预约才显示得出是谁约的
        assert store.get_org_user("ou_gone") is not None

    def test_no_members_is_an_error_with_guidance(self, service):
        client = self._client(members={})
        with pytest.raises(OrgSyncError, match="contact:user.base:readonly"):
            service(client).sync()

    def test_unconfigured_client_refused(self, service):
        with pytest.raises(OrgSyncError, match="凭据"):
            service(FakeFeishu(configured=False)).sync()

    def test_tenant_permission_missing_degrades(self, service):
        client = self._client(fail_on={"/tenant/v2/tenant/query"})
        result = service(client).sync()
        # 缺企业名称权限不该让整次同步失败，只提示一下
        assert result.users == 2
        assert any("tenant:tenant:readonly" in w for w in result.warnings)

    def test_no_departments_warns_but_proceeds(self, service):
        client = FakeFeishu(departments={"0": [[]]}, members={"0": [member("ou_a", "张三")]})
        result = service(client).sync()
        assert result.users == 1
        assert any("部门" in w for w in result.warnings)


class TestCheck:
    def test_ok(self, service):
        client = FakeFeishu(departments={"0": [[dept("d1", "电控部")]], "d1": [[]]})
        result = service(client).check()
        assert result["ok"] is True
        assert result["sample"] == ["电控部"]

    def test_failure_reports_reason(self, service):
        result = service(FakeFeishu(fail_on={"/contact/v3/departments"})).check()
        assert result["ok"] is False
        assert "权限" in result["error"]

    def test_unconfigured(self, service):
        assert service(FakeFeishu(configured=False)).check()["ok"] is False
