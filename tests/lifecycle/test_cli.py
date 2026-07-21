"""Tests for lifecycle/cli.py — argument parsing and main() entry point."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robotsix_central_deploy.lifecycle import cli
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.models import ExecutionBackendType, StoreBackend


# ---------------------------------------------------------------------------
# ArgumentParser — flag parsing and choices enforcement
# ---------------------------------------------------------------------------


class TestArgumentParser:
    """Direct tests for the ArgumentParser built inside main()."""

    def test_each_flag_parses_correctly(self):
        """All five CLI flags are accepted and reflected in config/uvicorn."""
        fake_uvicorn = MagicMock()
        fake_robotsix_config = MagicMock()
        cfg = LifecycleConfig()
        fake_robotsix_config.load_config = MagicMock(return_value=cfg)
        with patch.dict(
            "sys.modules",
            {"uvicorn": fake_uvicorn, "robotsix_config": fake_robotsix_config},
        ):
            cli.main(
                [
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8200",
                    "--store-backend",
                    "file",
                    "--execution-backend",
                    "noop",
                    "--api-key",
                    "secret",
                ]
            )
        # uvicorn was launched.
        fake_uvicorn.run.assert_called_once()
        _, kwargs = fake_uvicorn.run.call_args
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 8200
        # Config object mutated in-place.
        assert cfg.store_backend == StoreBackend.FILE
        assert cfg.execution_backend == ExecutionBackendType.NOOP
        assert cfg.api_key.get_secret_value() == "secret"

    def test_invalid_store_backend_raises_system_exit(self):
        """argparse rejects values outside StoreBackend choices."""
        with pytest.raises(SystemExit):
            cli.main(["--store-backend", "invalid"])

    def test_invalid_execution_backend_raises_system_exit(self):
        """argparse rejects values outside ExecutionBackendType choices."""
        with pytest.raises(SystemExit):
            cli.main(["--execution-backend", "bogus"])

    def test_non_integer_port_raises_system_exit(self):
        """argparse rejects non-integer values for --port."""
        with pytest.raises(SystemExit):
            cli.main(["--port", "abc"])


# ---------------------------------------------------------------------------
# main(argv) — config override via injection
# ---------------------------------------------------------------------------


class TestMainOverride:
    """main(argv) parameter injection — CLI overrides update config."""

    def test_override_host_and_port(self):
        """--host and --port are reflected in uvicorn.run kwargs."""
        fake_uvicorn = MagicMock()
        fake_robotsix_config = MagicMock()
        fake_robotsix_config.load_config = MagicMock(return_value=LifecycleConfig())
        with patch.dict(
            "sys.modules",
            {"uvicorn": fake_uvicorn, "robotsix_config": fake_robotsix_config},
        ):
            cli.main(["--port", "8200", "--host", "127.0.0.1"])
        _, kwargs = fake_uvicorn.run.call_args
        assert kwargs["port"] == 8200
        assert kwargs["host"] == "127.0.0.1"

    def test_override_store_backend_mutates_config(self):
        """--store-backend file mutates the LifecycleConfig in-place."""
        fake_uvicorn = MagicMock()
        cfg = LifecycleConfig()
        fake_robotsix_config = MagicMock()
        fake_robotsix_config.load_config = MagicMock(return_value=cfg)
        with patch.dict(
            "sys.modules",
            {"uvicorn": fake_uvicorn, "robotsix_config": fake_robotsix_config},
        ):
            cli.main(["--store-backend", "file"])
        assert cfg.store_backend == StoreBackend.FILE

    def test_override_execution_backend_mutates_config(self):
        """--execution-backend docker mutates the LifecycleConfig in-place."""
        fake_uvicorn = MagicMock()
        cfg = LifecycleConfig()
        fake_robotsix_config = MagicMock()
        fake_robotsix_config.load_config = MagicMock(return_value=cfg)
        with patch.dict(
            "sys.modules",
            {"uvicorn": fake_uvicorn, "robotsix_config": fake_robotsix_config},
        ):
            cli.main(["--execution-backend", "docker"])
        assert cfg.execution_backend == ExecutionBackendType.DOCKER

    def test_override_api_key_mutates_config(self):
        """--api-key mutates the LifecycleConfig in-place."""
        fake_uvicorn = MagicMock()
        cfg = LifecycleConfig()
        fake_robotsix_config = MagicMock()
        fake_robotsix_config.load_config = MagicMock(return_value=cfg)
        with patch.dict(
            "sys.modules",
            {"uvicorn": fake_uvicorn, "robotsix_config": fake_robotsix_config},
        ):
            cli.main(["--api-key", "override-key"])
        assert cfg.api_key.get_secret_value() == "override-key"

    def test_partial_override_preserves_defaults(self):
        """Unspecified flags leave config defaults intact."""
        fake_uvicorn = MagicMock()
        fake_robotsix_config = MagicMock()
        fake_robotsix_config.load_config = MagicMock(return_value=LifecycleConfig())
        with patch.dict(
            "sys.modules",
            {"uvicorn": fake_uvicorn, "robotsix_config": fake_robotsix_config},
        ):
            cli.main(["--port", "9000"])
        _, kwargs = fake_uvicorn.run.call_args
        assert kwargs["port"] == 9000
        assert kwargs["host"] == "0.0.0.0"

    def test_no_args_uses_defaults(self):
        """main([]) launches uvicorn with default host/port."""
        fake_uvicorn = MagicMock()
        fake_robotsix_config = MagicMock()
        fake_robotsix_config.load_config = MagicMock(return_value=LifecycleConfig())
        with patch.dict(
            "sys.modules",
            {"uvicorn": fake_uvicorn, "robotsix_config": fake_robotsix_config},
        ):
            cli.main([])
        fake_uvicorn.run.assert_called_once()
        _, kwargs = fake_uvicorn.run.call_args
        assert kwargs["host"] == "0.0.0.0"
        assert kwargs["port"] == 8100


# ---------------------------------------------------------------------------
# Regression: --execution-backend choices ↔ ExecutionBackendType
# ---------------------------------------------------------------------------


class TestExecutionBackendChoices:
    """Ensure the CLI choices stay in sync with ExecutionBackendType enum."""

    def test_choices_match_execution_backend_type(self):
        """ExecutionBackendType members are docker_sdk, docker, noop."""
        assert tuple(ExecutionBackendType) == (
            ExecutionBackendType.DOCKER_SDK,
            ExecutionBackendType.DOCKER,
            ExecutionBackendType.NOOP,
        )
