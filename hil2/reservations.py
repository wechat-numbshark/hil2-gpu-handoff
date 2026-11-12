"""可插拔预约数据源。

交接文件第 6 节：正式预约系统还没有 API 时，由管理员维护临时队列 JSON；
后续只替换数据源，不改变判定引擎。因此这里只暴露一个 ``fetch()`` 契约。

失败语义（fail-closed）：数据源不可用时抛异常，由上层保持上一次已知预约，
绝不把「读不到预约」当成「无人占用」。
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .models import Reservation

logger = logging.getLogger(__name__)


class ReservationSourceError(RuntimeError):
    """预约数据源不可用。上层必须降级为「维持现状」，不得宣告可用。"""


class ReservationSource(ABC):
    @abstractmethod
    def fetch(self) -> list[Reservation]:
        """返回全部集群的预约。失败请抛 ReservationSourceError。"""

    @property
    @abstractmethod
    def describe(self) -> str: ...


def _parse_payload(payload: Any, *, origin: str) -> list[Reservation]:
    if isinstance(payload, dict):
        # 兼容 {"data": [...]} / {"reservations": [...]} / {"52": {...}}
        for key in ("reservations", "data", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [
                {**value, "cluster": value.get("cluster", key)}
                for key, value in payload.items()
                if isinstance(value, dict)
            ]
    if not isinstance(payload, list):
        raise ReservationSourceError(f"{origin} 返回的不是预约数组")

    out: list[Reservation] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        reservation = Reservation.from_dict(item)
        if reservation.cluster:
            out.append(reservation)
    return out


class FileReservationSource(ReservationSource):
    """管理员维护的临时队列 JSON。文件不存在视为空队列（不是错误）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @property
    def describe(self) -> str:
        return f"file:{self.path}"

    def fetch(self) -> list[Reservation]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReservationSourceError(f"预约文件不可读：{type(exc).__name__}") from exc
        return _parse_payload(payload, origin=self.describe)


class HttpReservationSource(ReservationSource):
    """正式预约 API。只做 GET，读取失败一律抛错。"""

    def __init__(self, url: str, token: str = "", timeout: float = 10.0) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout

    @property
    def describe(self) -> str:
        return f"http:{self.url}"

    def fetch(self) -> list[Reservation]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = httpx.get(self.url, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            raise ReservationSourceError(f"预约接口失败：{type(exc).__name__}") from exc
        return _parse_payload(payload, origin=self.describe)


def build_source(settings: Settings) -> ReservationSource:
    if settings.reservation_source == "http":
        if not settings.reservation_api_url:
            raise ReservationSourceError("RESERVATION_SOURCE=http 但未配置 RESERVATION_API_URL")
        return HttpReservationSource(settings.reservation_api_url, settings.reservation_api_token)
    return FileReservationSource(settings.reservation_file)
