"""命令行入口：``python -m hil2`` 或 ``hil2-coordinator``。"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="HIL2.0 协调服务")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    args = parser.parse_args()

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    uvicorn.run(
        "hil2.api:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
