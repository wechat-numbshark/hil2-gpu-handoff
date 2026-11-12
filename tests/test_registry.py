"""集群注册表：配置校验、持久化、运行时设置、通讯录。"""

from __future__ import annotations

import pytest

from hil2.registry import ClusterConfig, CollectMode, OrgDepartment, OrgUser, RegistryError


class TestValidation:
    def test_defaults_are_valid(self):
        ClusterConfig(cluster="52").validate()

    @pytest.mark.parametrize("cluster", ["", "bad id", "a" * 33, "52/58", "../etc"])
    def test_bad_cluster_id_rejected(self, cluster):
        with pytest.raises(RegistryError, match="集群编号"):
            ClusterConfig(cluster=cluster).validate()

    def test_relative_data_dir_rejected(self):
        with pytest.raises(RegistryError, match="绝对路径"):
            ClusterConfig(cluster="52", data_dir="data1").validate()

    @pytest.mark.parametrize("field,value", [
        ("idle_minutes", 0), ("idle_minutes", 999),
        ("sample_seconds", 5), ("sample_seconds", 99999),
        ("confirm_samples", 0), ("command_timeout", 1),
    ])
    def test_out_of_range_thresholds_rejected(self, field, value):
        with pytest.raises(RegistryError):
            ClusterConfig(cluster="52", **{field: value}).validate()

    def test_empty_pattern_rejected(self):
        # 空正则会匹配一切或什么都不匹配，两种都会让判定失效
        with pytest.raises(RegistryError, match="不能为空"):
            ClusterConfig(cluster="52", sim_pattern="  ").validate()

    def test_invalid_regex_rejected(self):
        with pytest.raises(RegistryError, match="合法正则"):
            ClusterConfig(cluster="52", tester_pattern="(unclosed").validate()

    def test_ssh_mode_requires_host(self):
        with pytest.raises(RegistryError, match="主机地址"):
            ClusterConfig(cluster="52", mode=CollectMode.SSH_PULL).validate()

    def test_ssh_relative_key_path_rejected(self):
        with pytest.raises(RegistryError, match="绝对路径"):
            ClusterConfig(cluster="52", mode=CollectMode.SSH_PULL,
                          ssh_host="h", ssh_key_path="id_rsa").validate()

    def test_bastion_relative_key_path_rejected(self):
        with pytest.raises(RegistryError, match="堡垒机私钥路径"):
            ClusterConfig(cluster="52", mode=CollectMode.SSH_PULL, ssh_host="gpu",
                          ssh_jump_host="gate", ssh_jump_key_path="id_rsa").validate()

    def test_bastion_fields_require_host(self):
        with pytest.raises(RegistryError, match="必须先填写堡垒机主机"):
            ClusterConfig(cluster="52", mode=CollectMode.SSH_PULL, ssh_host="gpu",
                          ssh_jump_user="gate").validate()

    def test_agent_mode_needs_no_ssh(self):
        ClusterConfig(cluster="52", mode=CollectMode.AGENT_PUSH).validate()


class TestFromDict:
    def test_partial_update_keeps_existing(self):
        base = ClusterConfig(cluster="52", data_dir="/data9", idle_minutes=20,
                             tester_pattern="only-this")
        updated = ClusterConfig.from_dict({"name": "台架A"}, base=base)
        assert updated.data_dir == "/data9"
        assert updated.idle_minutes == 20
        assert updated.tester_pattern == "only-this"
        assert updated.name == "台架A"

    def test_bool_accepts_form_values(self):
        for raw, expected in (("on", True), ("true", True), ("1", True),
                              ("false", False), ("0", False), (False, False)):
            config = ClusterConfig.from_dict({"cluster": "52", "enabled": raw})
            assert config.enabled is expected

    def test_non_integer_rejected_with_readable_message(self):
        with pytest.raises(RegistryError, match="必须是整数"):
            ClusterConfig.from_dict({"cluster": "52", "idle_minutes": "很久"})

    def test_unknown_mode_rejected(self):
        with pytest.raises(RegistryError, match="采集方式"):
            ClusterConfig.from_dict({"cluster": "52", "mode": "telepathy"})

    def test_extra_processes_can_be_cleared(self):
        base = ClusterConfig(cluster="52", extra_test_processes="robot")
        assert ClusterConfig.from_dict({"extra_test_processes": ""}, base=base) \
            .extra_test_processes == ""


class TestSerialisation:
    def test_webhook_never_echoed_by_default(self):
        config = ClusterConfig(cluster="52", webhook_url="https://open.feishu.cn/hook/secret")
        data = config.as_dict()
        assert data["webhook_url"] == ""
        assert data["webhook_configured"] is True
        assert "secret" not in str(data)

    def test_agent_env_covers_all_knobs(self):
        config = ClusterConfig(cluster="52", data_dir="/data9", tester_pattern="t|x")
        env = config.agent_env(coordinator_url="http://c/api/ingest", ingest_token="tok")
        assert env["HIL_MACHINE_NAME"] == "52"
        assert env["HIL_DATA_DIR"] == "/data9"
        assert env["HIL_TESTER_PATTERN"] == "t|x"
        assert env["HIL_COORDINATOR_URL"] == "http://c/api/ingest"
        assert env["HIL_INGEST_TOKEN"] == "tok"


class TestStorePersistence:
    def test_save_and_read_back(self, store):
        store.save_cluster(ClusterConfig(cluster="52", name="台架A",
                                         mode=CollectMode.SSH_PULL, ssh_host="h",
                                         ssh_jump_host="gate", ssh_jump_user="ops"), actor="ou_a")
        loaded = store.get_cluster("52")
        assert loaded.name == "台架A"
        assert loaded.mode is CollectMode.SSH_PULL
        assert loaded.ssh_jump_host == "gate"
        assert loaded.ssh_jump_user == "ops"
        assert loaded.updated_at is not None

    def test_update_overwrites(self, store):
        store.save_cluster(ClusterConfig(cluster="52", data_dir="/data1"))
        store.save_cluster(ClusterConfig(cluster="52", data_dir="/data9"))
        assert store.get_cluster("52").data_dir == "/data9"
        assert len(store.list_clusters()) == 1

    def test_list_sorted_numerically(self, store):
        for cluster in ("108", "7", "52", "benchB"):
            store.save_cluster(ClusterConfig(cluster=cluster))
        assert [c.cluster for c in store.list_clusters()] == ["7", "52", "108", "benchB"]

    def test_only_enabled_filter(self, store):
        store.save_cluster(ClusterConfig(cluster="52", enabled=True))
        store.save_cluster(ClusterConfig(cluster="58", enabled=False))
        assert [c.cluster for c in store.list_clusters(only_enabled=True)] == ["52"]

    def test_delete_keeps_history(self, store):
        from .conftest import make_event

        store.save_cluster(ClusterConfig(cluster="52"))
        store.record_event(make_event("52", "busy", event_id="keep-me"))
        assert store.delete_cluster("52", actor="ou_a") is True
        assert store.get_cluster("52") is None
        # 历史事件不能跟着配置一起消失，否则事后没法追溯
        assert store.seen_event("keep-me") is True

    def test_mark_seen_records_error(self, store):
        store.save_cluster(ClusterConfig(cluster="52"))
        store.mark_cluster_seen("52", error="ssh 连不上")
        loaded = store.get_cluster("52")
        assert loaded.last_seen_at is not None
        assert loaded.last_error == "ssh 连不上"

    def test_save_rejects_invalid(self, store):
        with pytest.raises(RegistryError):
            store.save_cluster(ClusterConfig(cluster="52", mode=CollectMode.SSH_PULL))


class TestSettings:
    def test_round_trip_types(self, store):
        for key, value in (("a", "str"), ("b", 42), ("c", True), ("d", ["x", "y"])):
            store.set_setting(key, value)
            assert store.get_setting(key) == value

    def test_missing_returns_default(self, store):
        assert store.get_setting("nope", "fallback") == "fallback"

    def test_all_settings(self, store):
        store.set_setting("a", 1)
        store.set_setting("b", 2)
        assert store.all_settings() == {"a": 1, "b": 2}


class TestOrgDirectory:
    def test_upsert_and_search(self, store):
        store.upsert_org_users([
            OrgUser(open_id="ou_a", name="张三", email="zhang@x.com"),
            OrgUser(open_id="ou_b", name="李四", email="li@x.com"),
        ])
        assert len(store.search_org_users()) == 2
        assert [u.name for u in store.search_org_users("张")] == ["张三"]
        assert [u.name for u in store.search_org_users("li@")] == ["李四"]

    def test_sync_preserves_admin_flag(self, store):
        store.upsert_org_users([OrgUser(open_id="ou_a", name="张三")])
        store.set_admin("ou_a", True)
        # 再次同步（飞书那边没有管理员概念）不能把本地指派冲掉
        store.upsert_org_users([OrgUser(open_id="ou_a", name="张三丰")])
        loaded = store.get_org_user("ou_a")
        assert loaded.is_admin is True
        assert loaded.name == "张三丰"

    def test_admins_only_filter(self, store):
        store.upsert_org_users([OrgUser(open_id="ou_a", name="张"),
                                OrgUser(open_id="ou_b", name="李")])
        store.set_admin("ou_b", True)
        assert [u.open_id for u in store.search_org_users(admins_only=True)] == ["ou_b"]

    def test_departments(self, store):
        store.upsert_org_departments([
            OrgDepartment(department_id="d1", name="电控部", member_count=12)
        ])
        assert store.org_stats()["departments"] == 1

    def test_set_admin_on_unknown_user_fails(self, store):
        assert store.set_admin("ou_ghost", True) is False

    def test_inactive_users_excluded_from_search(self, store):
        store.upsert_org_users([OrgUser(open_id="ou_a", name="离职", is_active=False)])
        assert store.search_org_users() == []
        # 但仍能按 open_id 查到，历史预约才显示得出是谁
        assert store.get_org_user("ou_a").name == "离职"


class TestAdminAudit:
    def test_logged_and_listed(self, store):
        store.save_cluster(ClusterConfig(cluster="52"), actor="ou_admin")
        store.set_setting("k", "v", actor="ou_admin")
        entries = store.recent_admin_audit()
        assert any(e["action"] == "save_cluster" and e["actor"] == "ou_admin" for e in entries)


class TestHermeticSettings:
    """守住「测试不依赖开发者本地环境」这条线。

    曾经出过一次：本地 .env 里有 RESERVATION_SOURCE=file，测试全过；
    打包给客户的干净副本没有 .env，36 个测试当场失败。
    """

    def test_fixture_ignores_dotenv(self, settings, tmp_path, monkeypatch):
        # 就算真有一个 .env 摆在那儿，夹具也不该受影响
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            "RESERVATION_SOURCE=http\nMANAGED_CLUSTERS=999\n", encoding="utf-8"
        )
        from hil2.config import Settings

        fresh = Settings(_env_file=None, reservation_source="file")
        assert fresh.reservation_source == "file"
        assert settings.reservation_source == "file"

    def test_fixture_pins_the_fields_tests_rely_on(self, settings):
        # 这些字段的默认值将来若改动，不该悄悄改变已有测试的含义
        assert settings.reservation_source == "file"
        assert settings.managed_clusters == ["52", "58", "60"]
        assert settings.idle_minutes == 15
        assert settings.sso_enabled is False
