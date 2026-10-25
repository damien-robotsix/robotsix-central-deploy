"""Tests for the lifecycle server's structured-logging configuration.

``_logging.configure_logging`` delegates the structlog + stdlib
``ProcessorFormatter`` JSON bridge to ``robotsix_llmio.logging.setup_structlog``.
These tests verify JSON output parity (the ``request_id`` field bound via
``structlog.contextvars`` still lands on every record) and that the minimal
``UVICORN_LOG_CONFIG`` keeps the startup banner human-readable while letting
access logs reach the shared JSON root bridge.

This module is skipped in lightweight environments without a real structlog
(see ``pytest_ignore_collect`` in ``tests/conftest.py``).
"""

from __future__ import annotations

import io
import json
import logging

import structlog

from robotsix_central_deploy.lifecycle._logging import (
    UVICORN_LOG_CONFIG,
    configure_logging,
)


def test_configure_logging_emits_json_with_request_id() -> None:
    buf = io.StringIO()
    configure_logging(level="INFO", stream=buf)

    structlog.contextvars.bind_contextvars(request_id="corr-999")
    try:
        logging.getLogger("robotsix_central_deploy.test_logging_config").info("hello")
    finally:
        structlog.contextvars.unbind_contextvars("request_id")

    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    record = json.loads(lines[-1])
    assert record["event"] == "hello"
    assert record["level"] == "info"
    assert record["logger"] == "robotsix_central_deploy.test_logging_config"
    # Parity: the per-request id is merged onto the record.
    assert record["request_id"] == "corr-999"
    assert "timestamp" in record
    # The shared helper stamps the (inactive) OTel trace id as a placeholder.
    assert record["trace_id"] == "-"


def test_uvicorn_log_config_keeps_banner_human_readable() -> None:
    # No "root" key: dictConfig must not replace the root handler that
    # setup_structlog installs.
    assert "root" not in UVICORN_LOG_CONFIG

    formatters = UVICORN_LOG_CONFIG["formatters"]
    assert isinstance(formatters, dict)
    assert formatters["default"]["()"] == "uvicorn.logging.DefaultFormatter"

    loggers = UVICORN_LOG_CONFIG["loggers"]
    assert isinstance(loggers, dict)
    # Banner (uvicorn.error) stays on its own human-readable handler and is
    # NOT propagated to the JSON root bridge.
    assert loggers["uvicorn.error"]["handlers"] == ["default"]
    assert loggers["uvicorn.error"]["propagate"] is False
    # Access logs carry no own handler and propagate to the root JSON bridge.
    assert loggers["uvicorn.access"]["handlers"] == []
    assert loggers["uvicorn.access"]["propagate"] is True
