"""Regression test for the structlog dictConfig processor-chain bug.

``foreign_pre_chain``/``processors`` entries in a ``ProcessorFormatter``
config must be actual callables — dictConfig's "()" resolution does not
recurse into a formatter's other keys, so dotted-path strings there are
passed through unresolved and raise ``TypeError: 'str' object is not
callable`` on the very first log record, silently swallowing the message.
"""

from __future__ import annotations

import json
import logging
import logging.config

import pytest

from robotsix_central_deploy.lifecycle._logging import LOGGING_CONFIG


def test_json_formatter_processors_are_callable() -> None:
    formatters = LOGGING_CONFIG["formatters"]
    assert isinstance(formatters, dict)
    json_formatter = formatters["json"]
    assert isinstance(json_formatter, dict)
    for key in ("processors", "foreign_pre_chain"):
        for proc in json_formatter[key]:
            assert callable(proc), f"{key} entry {proc!r} is not callable"


def test_stdlib_logger_emits_valid_json(capsys: pytest.CaptureFixture[str]) -> None:
    logging.config.dictConfig(LOGGING_CONFIG)
    logging.getLogger().setLevel("INFO")
    logger = logging.getLogger("robotsix_central_deploy.test_logging_config")

    logger.info("test message %s", "arg")

    out = capsys.readouterr().out.strip()
    record = json.loads(out)
    assert record["event"] == "test message arg"
    assert record["level"] == "info"
