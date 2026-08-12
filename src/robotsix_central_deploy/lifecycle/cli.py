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
    """Parse CLI arguments and start the lifecycle uvicorn server.

    Loads configuration via :func:`robotsix_config.load_config`, then
    overlays any CLI-provided values on top (CLI arguments always take
    precedence over the config file).  Accepts six optional arguments:
    ``--host``, ``--port``, ``--store-backend``, ``--execution-backend``,
    ``--api-key``, and ``--target-disk``.

    The :mod:`uvicorn` and :data:`LOGGING_CONFIG` imports are deferred
    so that ``--help`` and argument-parsing failures respond quickly
    without pulling in the full server runtime.
    """
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
    parser.add_argument(
        "--target-disk",
        default=None,
        help=(
            "Default target disk for new component volumes "
            "(device path, mount point, or label)"
        ),
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
    if args.target_disk is not None:
        cfg.target_disk = args.target_disk

    import uvicorn

    from ._logging import LOGGING_CONFIG

    uvicorn.run(
        "robotsix_central_deploy.lifecycle.app:app",
        host=cfg.host,
        port=cfg.port,
        reload=False,
        log_config=LOGGING_CONFIG,
    )


if __name__ == "__main__":
    main()
