"""预约数据源：file / http 可插拔，失败一律抛错由上层降级。"""

from __future__ import annotations

import json

import httpx
import pytest

from hil2.config import Settings
from hil2.reservations import (
    FileReservationSource,
    HttpReservationSource,
    ReservationSourceError,
    build_source,
)


class TestFileSource:
    def test_missing_file_is_empty_queue_not_error(self, tmp_path):
        # 还没建队列文件属于正常情况，不该报错。
        assert FileReservationSource(tmp_path / "none.json").fetch() == []

    def test_reads_array(self, tmp_path):
        path = tmp_path / "r.json"
        path.write_text(json.dumps([
            {"cluster": "52", "current_open_id": "ou_a", "next_open_id": "ou_b",
             "handoff_at": "2026-09-01T16:00:00+08:00"}
        ]), encoding="utf-8")
        reservations = FileReservationSource(path).fetch()
        assert reservations[0].cluster == "52"
        assert reservations[0].current_open_id == "ou_a"

    def test_reads_wrapped_object(self, tmp_path):
        path = tmp_path / "r.json"
        path.write_text(json.dumps({"data": [{"cluster": "58", "current_open_id": "ou_x"}]}),
                        encoding="utf-8")
        assert FileReservationSource(path).fetch()[0].cluster == "58"

    def test_reads_cluster_keyed_object(self, tmp_path):
        path = tmp_path / "r.json"
        path.write_text(json.dumps({"60": {"current_open_id": "ou_y"}}), encoding="utf-8")
        reservations = FileReservationSource(path).fetch()
        assert reservations[0].cluster == "60"
        assert reservations[0].current_open_id == "ou_y"

    def test_broken_json_raises(self, tmp_path):
        path = tmp_path / "r.json"
        path.write_text("{ 坏掉了", encoding="utf-8")
        with pytest.raises(ReservationSourceError):
            FileReservationSource(path).fetch()

    def test_wrong_shape_raises(self, tmp_path):
        path = tmp_path / "r.json"
        path.write_text(json.dumps("just a string"), encoding="utf-8")
        with pytest.raises(ReservationSourceError):
            FileReservationSource(path).fetch()

    def test_entries_without_cluster_skipped(self, tmp_path):
        path = tmp_path / "r.json"
        path.write_text(json.dumps([{"current_open_id": "ou_a"}, {"cluster": "52"}]),
                        encoding="utf-8")
        assert [r.cluster for r in FileReservationSource(path).fetch()] == ["52"]


class TestHttpSource:
    def _source(self, monkeypatch, handler):
        source = HttpReservationSource("https://api.invalid/reservations", token="t")
        monkeypatch.setattr(httpx, "get", handler)
        return source

    def test_reads_json_array(self, monkeypatch):
        def handler(url, headers=None, timeout=None):
            assert headers["Authorization"] == "Bearer t"
            return httpx.Response(
                200,
                json=[{"cluster": "52", "current_open_id": "ou_a"}],
                request=httpx.Request("GET", url),
            )

        assert self._source(monkeypatch, handler).fetch()[0].cluster == "52"

    def test_http_error_raises_source_error(self, monkeypatch):
        def handler(url, headers=None, timeout=None):
            return httpx.Response(500, text="boom", request=httpx.Request("GET", url))

        with pytest.raises(ReservationSourceError):
            self._source(monkeypatch, handler).fetch()

    def test_network_error_raises_source_error(self, monkeypatch):
        def handler(url, headers=None, timeout=None):
            raise httpx.ConnectError("no route")

        with pytest.raises(ReservationSourceError):
            self._source(monkeypatch, handler).fetch()

    def test_non_json_raises_source_error(self, monkeypatch):
        def handler(url, headers=None, timeout=None):
            return httpx.Response(
                200, text="<html>maintenance</html>", request=httpx.Request("GET", url)
            )

        with pytest.raises(ReservationSourceError):
            self._source(monkeypatch, handler).fetch()


class TestBuildSource:
    def test_defaults_to_file(self, tmp_path):
        settings = Settings(reservation_file=tmp_path / "r.json")
        assert isinstance(build_source(settings), FileReservationSource)

    def test_http_selected_by_config(self, tmp_path):
        settings = Settings(reservation_source="http",
                            reservation_api_url="https://api.invalid/r")
        assert isinstance(build_source(settings), HttpReservationSource)

    def test_http_without_url_refused(self):
        # 配了 http 却没给地址属于配置错误，早失败好过运行期静默降级。
        with pytest.raises(ReservationSourceError):
            build_source(Settings(reservation_source="http", reservation_api_url=""))

    def test_swapping_source_does_not_touch_engine(self, tmp_path):
        # 交接文件第 6 节：后续只替换数据源，不改变判定引擎。
        from hil2 import flow_engine

        before = flow_engine.decide.__code__.co_code
        build_source(Settings(reservation_source="http", reservation_api_url="https://a.invalid"))
        assert flow_engine.decide.__code__.co_code == before
