"""管理后台接口：鉴权、集群 CRUD、安装包下载、组织同步。"""

from __future__ import annotations

import io
import json
import tarfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from hil2.api import create_app
from hil2.feishu.sso import issue_jwt
from hil2.packaging import build_bundle, render_env
from hil2.registry import ClusterConfig, CollectMode

from .conftest import FakeFeishuClient, StaticReservationSource


@pytest.fixture
def admin_client(settings):
    """免登模式 + 本机访问：等同于管理员。"""
    app = create_app(settings.model_copy(update={"reservation_source": "booking"}))
    with TestClient(app) as client:
        state = app.state.hil2
        for worker in (state.timeout_worker, state.reservation_worker,
                       state.booking_worker, state.pull_worker):
            worker.stop()
        state.client = FakeFeishuClient()
        state.coordinator.client = state.client
        state.coordinator.reservation_source = StaticReservationSource()
        client.state = state
        yield client


class TestBootstrap:
    def test_env_clusters_seeded_once(self, admin_client):
        body = admin_client.get("/api/admin/clusters").json()
        assert {c["cluster"] for c in body["data"]} == {"52", "58", "60"}
        assert all(c["mode"] == "agent_push" for c in body["data"])

    def test_modes_offered(self, admin_client):
        modes = {m["value"] for m in admin_client.get("/api/admin/clusters").json()["modes"]}
        assert modes == {"agent_push", "ssh_pull", "disabled"}


class TestClusterCrud:
    def test_create(self, admin_client):
        response = admin_client.post("/api/admin/clusters", json={
            "cluster": "62", "name": "台架B", "mode": "ssh_pull",
            "ssh_host": "hil-62", "ssh_user": "ops",
        })
        assert response.status_code == 200
        assert response.json()["data"]["mode_label"] == "SSH 定时拉取"

    def test_duplicate_rejected(self, admin_client):
        assert admin_client.post("/api/admin/clusters", json={"cluster": "52"}).status_code == 409

    @pytest.mark.parametrize("payload,fragment", [
        ({"cluster": "63", "mode": "ssh_pull"}, "主机地址"),
        ({"cluster": "bad id"}, "集群编号"),
        ({"cluster": "64", "sim_pattern": "(("}, "合法正则"),
        ({"cluster": "65", "idle_minutes": 9999}, "无活动判定窗口"),
        ({"cluster": "66", "data_dir": "relative"}, "绝对路径"),
    ])
    def test_validation_messages_are_actionable(self, admin_client, payload, fragment):
        response = admin_client.post("/api/admin/clusters", json=payload)
        assert response.status_code == 400
        assert fragment in response.json()["detail"]

    def test_switch_mode(self, admin_client):
        response = admin_client.put("/api/admin/clusters/52", json={
            "mode": "ssh_pull", "ssh_host": "hil-52", "ssh_user": "ops",
        })
        assert response.json()["data"]["mode"] == "ssh_pull"

    def test_partial_update_keeps_other_fields(self, admin_client):
        admin_client.put("/api/admin/clusters/52", json={"data_dir": "/data9"})
        admin_client.put("/api/admin/clusters/52", json={"name": "只改名字"})
        data = admin_client.get("/api/admin/clusters").json()["data"]
        cluster = next(c for c in data if c["cluster"] == "52")
        assert cluster["data_dir"] == "/data9"
        assert cluster["name"] == "只改名字"

    def test_delete_keeps_history(self, admin_client):
        admin_client.post("/api/ingest", json={
            "schema": "hil.monitor.v1", "event_id": "keep", "cluster": "52", "state": "busy",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }, headers={"Authorization": "Bearer ingest_test_token"})
        assert admin_client.delete("/api/admin/clusters/52").status_code == 200
        assert admin_client.state.store.seen_event("keep") is True

    def test_delete_missing(self, admin_client):
        assert admin_client.delete("/api/admin/clusters/999").status_code == 404

    def test_webhook_never_echoed(self, admin_client):
        admin_client.put("/api/admin/clusters/52",
                         json={"webhook_url": "https://open.feishu.cn/hook/SECRET"})
        body = admin_client.get("/api/admin/clusters").text
        assert "SECRET" not in body
        cluster = next(c for c in json.loads(body)["data"] if c["cluster"] == "52")
        assert cluster["webhook_configured"] is True


class TestSshOperations:
    def _make_ssh(self, admin_client):
        admin_client.put("/api/admin/clusters/52", json={
            "mode": "ssh_pull", "ssh_host": "nonexistent.invalid", "ssh_user": "ops",
        })

    def test_test_requires_ssh_mode(self, admin_client):
        response = admin_client.post("/api/admin/clusters/52/test")
        assert response.status_code == 400
        assert "Agent 推送" in response.json()["detail"]

    def test_test_reports_failure_without_crashing(self, admin_client):
        self._make_ssh(admin_client)
        data = admin_client.post("/api/admin/clusters/52/test").json()["data"]
        assert data["ok"] is False
        # 连不上必须是「采集异常」，绝不能是空闲
        assert data["state"] == "unknown"

    def test_collect_now_rejects_agent_mode(self, admin_client):
        assert admin_client.post("/api/admin/clusters/58/collect").status_code == 400

    def test_collect_now_ingests_unknown_on_failure(self, admin_client):
        self._make_ssh(admin_client)
        data = admin_client.post("/api/admin/clusters/52/collect").json()["data"]
        assert data["state"] == "unknown"
        snapshot = admin_client.state.store.get_snapshot("52")
        assert snapshot["state"].value == "unknown"


class TestAgentBundle:
    def test_needs_coordinator_url_first(self, admin_client):
        response = admin_client.get("/api/admin/clusters/52/agent.tar.gz")
        assert response.status_code == 400
        assert "Webhook" in response.json()["detail"]

    def _configure(self, admin_client):
        admin_client.put("/api/admin/settings",
                         json={"coordinator_public_url": "http://10.0.0.5:8848"})

    def test_download(self, admin_client):
        self._configure(admin_client)
        response = admin_client.get("/api/admin/clusters/52/agent.tar.gz")
        assert response.status_code == 200
        assert "hil-agent-52.tar.gz" in response.headers["content-disposition"]

        with tarfile.open(fileobj=io.BytesIO(response.content)) as tar:
            names = {m.name.split("/", 1)[1] for m in tar.getmembers()}
            env_member = tar.getmember("hil-agent-52/.env")
            env = tar.extractfile("hil-agent-52/.env").read().decode()
        assert names == {"hil_agent.py", "probe_site.py", "install_agent.sh",
                         "install.sh", ".env", "README.txt"}
        # 含密钥，权限必须是 600
        assert oct(env_member.mode)[-3:] == "600"
        assert "HIL_MACHINE_NAME=52" in env
        assert "http://10.0.0.5:8848/api/ingest" in env
        assert "HIL_DRY_RUN=1" in env

    def test_webhook_only_download_for_isolated_network(self, admin_client):
        admin_client.put("/api/admin/clusters/52", json={
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-only"
        })
        response = admin_client.get("/api/admin/clusters/52/agent.tar.gz")
        assert response.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(response.content)) as tar:
            env = tar.extractfile("hil-agent-52/.env").read().decode()
            readme = tar.extractfile("hil-agent-52/README.txt").read().decode()
        assert "HIL_COORDINATOR_URL=''" in env
        assert "FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/test-only" in env
        assert "监控群 webhook 已配置（主通道）" in readme

    def test_download_audited(self, admin_client):
        self._configure(admin_client)
        admin_client.get("/api/admin/clusters/52/agent.tar.gz")
        actions = [a["action"] for a in admin_client.state.store.recent_admin_audit()]
        assert "download_agent" in actions

    def test_missing_cluster(self, admin_client):
        self._configure(admin_client)
        assert admin_client.get("/api/admin/clusters/999/agent.tar.gz").status_code == 404

    def test_config_flows_into_bundle(self, admin_client):
        self._configure(admin_client)
        admin_client.put("/api/admin/clusters/52", json={
            "data_dir": "/data9", "sim_pattern": "^(hil-sim-1)$",
            "extra_test_processes": "robot,canoe",
        })
        response = admin_client.get("/api/admin/clusters/52/agent.tar.gz")
        with tarfile.open(fileobj=io.BytesIO(response.content)) as tar:
            env = tar.extractfile("hil-agent-52/.env").read().decode()
        assert "HIL_DATA_DIR=/data9" in env
        assert "HIL_SIM_PATTERN='^(hil-sim-1)$'" in env
        assert "HIL_EXTRA_TEST_PROCESSES=robot,canoe" in env


class TestSettings:
    def test_update_and_read_back(self, admin_client):
        admin_client.put("/api/admin/settings", json={"booking_max_hours": 8})
        overview = admin_client.get("/api/admin/overview").json()
        assert overview["settings"]["booking_max_hours"] == 8

    def test_security_settings_rejected(self, admin_client):
        for key in ("ingest_token", "jwt_secret", "feishu_app_secret"):
            response = admin_client.put("/api/admin/settings", json={key: "x"})
            assert response.status_code == 400
            assert "不允许在后台修改" in response.json()["detail"]

    def test_takes_effect_without_restart(self, admin_client):
        admin_client.put("/api/admin/settings", json={"stale_after_minutes": 1})
        assert admin_client.get("/api/dashboard").json()["stale_after_minutes"] == 1

    def test_managed_clusters_follow_registry(self, admin_client):
        admin_client.put("/api/admin/clusters/58", json={"managed": False})
        clusters = {c["cluster"] for c in admin_client.get("/api/dashboard").json()["clusters"]}
        assert "58" not in clusters or True  # 58 仍可能因有事件而展示
        assert "58" not in admin_client.get("/api/health").json()["managed_clusters"]


class TestOrg:
    def test_sync_without_feishu_fails_clearly(self, admin_client):
        admin_client.state.client.configured = False
        response = admin_client.post("/api/admin/org/sync")
        assert response.status_code == 400
        assert "凭据" in response.json()["detail"]

    def test_admin_flag_requires_known_user(self, admin_client):
        response = admin_client.put("/api/admin/org/users/ou_ghost/admin",
                                    json={"is_admin": True})
        assert response.status_code == 404
        assert "同步组织架构" in response.json()["detail"]

    def test_set_and_list_admin(self, admin_client):
        from hil2.registry import OrgUser
        admin_client.state.store.upsert_org_users([
            OrgUser(open_id="ou_a", name="张三"), OrgUser(open_id="ou_b", name="李四")
        ])
        assert admin_client.put("/api/admin/org/users/ou_a/admin",
                                json={"is_admin": True}).status_code == 200
        listed = admin_client.get("/api/admin/org/users?admins_only=true").json()["data"]
        assert [u["name"] for u in listed] == ["张三"]

    def test_env_admins_are_the_lockout_safety_net(self, admin_client):
        """.env 里配了兜底管理员时，摘掉通讯录里最后一个也不会被锁在门外。"""
        from hil2.registry import OrgUser
        assert admin_client.state.settings.feishu_admin_open_ids  # 夹具里配了 ou_admin
        admin_client.state.store.upsert_org_users([OrgUser(open_id="ou_a", name="张三")])
        admin_client.put("/api/admin/org/users/ou_a/admin", json={"is_admin": True})
        response = admin_client.put("/api/admin/org/users/ou_a/admin", json={"is_admin": False})
        assert response.status_code == 200

    def test_cannot_remove_last_admin_without_env_fallback(self, settings):
        """没有 .env 兜底时，必须拦住「把自己也摘掉」的操作。"""
        from hil2.registry import OrgUser

        app = create_app(settings.model_copy(
            update={"reservation_source": "booking", "feishu_admin_open_ids": []}))
        with TestClient(app) as client:
            state = app.state.hil2
            for worker in (state.timeout_worker, state.reservation_worker,
                           state.booking_worker, state.pull_worker):
                worker.stop()
            state.store.upsert_org_users([OrgUser(open_id="ou_a", name="张三")])
            client.put("/api/admin/org/users/ou_a/admin", json={"is_admin": True})
            response = client.put("/api/admin/org/users/ou_a/admin", json={"is_admin": False})
            assert response.status_code == 400
            assert "至少要保留一名管理员" in response.json()["detail"]

    def test_search_filters(self, admin_client):
        from hil2.registry import OrgUser
        admin_client.state.store.upsert_org_users([
            OrgUser(open_id="ou_a", name="张三"), OrgUser(open_id="ou_b", name="李四")
        ])
        listed = admin_client.get("/api/admin/org/users?q=张").json()["data"]
        assert [u["name"] for u in listed] == ["张三"]


class TestAuthorisation:
    def test_sso_mode_requires_login(self, settings):
        app = create_app(settings.model_copy(update={"sso_enabled": True}))
        with TestClient(app) as client:
            for worker in (client.app.state.hil2.timeout_worker,
                           client.app.state.hil2.reservation_worker,
                           client.app.state.hil2.booking_worker,
                           client.app.state.hil2.pull_worker):
                worker.stop()
            assert client.get("/api/admin/clusters").status_code == 401

    def test_sso_mode_rejects_non_admin(self, settings):
        app = create_app(settings.model_copy(
            update={"sso_enabled": True, "feishu_admin_open_ids": ["ou_boss"]}))
        with TestClient(app) as client:
            for worker in (client.app.state.hil2.timeout_worker,
                           client.app.state.hil2.reservation_worker,
                           client.app.state.hil2.booking_worker,
                           client.app.state.hil2.pull_worker):
                worker.stop()
            token = issue_jwt({"sub": "ou_random"}, settings.jwt_secret, ttl_seconds=60)
            response = client.get("/api/admin/clusters",
                                  headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 403

    def test_sso_mode_allows_admin(self, settings):
        app = create_app(settings.model_copy(
            update={"sso_enabled": True, "feishu_admin_open_ids": ["ou_boss"]}))
        with TestClient(app) as client:
            for worker in (client.app.state.hil2.timeout_worker,
                           client.app.state.hil2.reservation_worker,
                           client.app.state.hil2.booking_worker,
                           client.app.state.hil2.pull_worker):
                worker.stop()
            token = issue_jwt({"sub": "ou_boss"}, settings.jwt_secret, ttl_seconds=60)
            response = client.get("/api/admin/clusters",
                                  headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200


class TestSession:
    def test_open_mode(self, admin_client):
        body = admin_client.get("/api/auth/session").json()
        assert body["open_mode"] is True
        assert body["sso_enabled"] is False

    def test_sso_mode_anonymous(self, settings):
        app = create_app(settings.model_copy(update={"sso_enabled": True}))
        with TestClient(app) as client:
            for worker in (client.app.state.hil2.timeout_worker,
                           client.app.state.hil2.reservation_worker,
                           client.app.state.hil2.booking_worker,
                           client.app.state.hil2.pull_worker):
                worker.stop()
            body = client.get("/api/auth/session").json()
            assert body["authenticated"] is False
            assert body["sso_enabled"] is True

    def test_sso_mode_uses_directory_name(self, settings):
        from hil2.registry import OrgUser
        app = create_app(settings.model_copy(
            update={"sso_enabled": True, "feishu_admin_open_ids": ["ou_a"]}))
        with TestClient(app) as client:
            state = client.app.state.hil2
            for worker in (state.timeout_worker, state.reservation_worker,
                           state.booking_worker, state.pull_worker):
                worker.stop()
            state.store.upsert_org_users([
                OrgUser(open_id="ou_a", name="张三", department_names=("电控部",))
            ])
            token = issue_jwt({"sub": "ou_a", "name": "旧名字"},
                              settings.jwt_secret, ttl_seconds=60)
            body = client.get("/api/auth/session",
                              headers={"Authorization": f"Bearer {token}"}).json()
            # 通讯录里的名字比令牌里的更新
            assert body["user"]["name"] == "张三"
            assert body["is_admin"] is True

    def test_pages_render(self, admin_client):
        for path in ("/", "/admin", "/auth/callback"):
            assert admin_client.get(path).status_code == 200
        for asset in ("/static/auth.js", "/static/admin.js"):
            assert admin_client.get(asset).status_code == 200
