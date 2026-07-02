"""CLI entry point for the lifecycle server.

Usage::

    robotsix-lifecycle          # start with defaults
    robotsix-lifecycle --port 8200 --host 127.0.0.1
"""

from __future__ import annotations

import argparse

from .config import LifecycleConfig
from .models import ExecutionBackendType, StoreBackend


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="robotsix-central-deploy lifecycle server"
    )
    parser.add_argument("--host", default=None, help="Bind address (default: 0.0.0.0)")
    parser.add_argument(
        "--port", type=int, default=None, help="Bind port (default: 8100)"
    )
    parser.add_argument("--store-backend", default=None, choices=tuple(StoreBackend))
    parser.add_argument(
        "--execution-backend", default=None, choices=tuple(ExecutionBackendType)
    )
    parser.add_argument(
        "--api-key", default=None, help="API key for mutating endpoints"
    )
    args = parser.parse_args(argv)

    import robotsix_config

    cfg = robotsix_config.load_config(LifecycleConfig)

    # Override from CLI when provided.
    if args.host is not None:
        cfg.host = args.host
    if args.port is not None:
        cfg.port = args.port
    if args.store_backend is not None:
        cfg.store_backend = args.store_backend
    if args.execution_backend is not None:
        cfg.execution_backend = args.execution_backend
    if args.api_key is not None:
        cfg.api_key = args.api_key

    import uvicorn

    uvicorn.run(
        "robotsix_central_deploy.lifecycle.server:app",
        host=cfg.host,
        port=cfg.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
