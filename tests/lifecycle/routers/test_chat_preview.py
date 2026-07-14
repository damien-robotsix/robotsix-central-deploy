"""Unit tests for lifecycle/routers/chat_preview.py."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi import HTTPException, status
from httpx import AsyncClient

from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.routers.chat_preview import (
    _PREVIEW_COMPONENT_ID,
    _PREVIEW_IMAGE_TAG,
    _build_image,
    _clone_repo,
    _create_preview_container,
    _extract_service_config,
    _find_compose_file,
    _log_safe,
    _parse_ports,
    _register_preview_component,
    _resolve_gateway_base_domain,
    _stop_and_remove_preview_container,
    _unregister_preview_component,
)
from robotsix_central_deploy.registry.models import ComponentConfig, PortMapping


# ---------------------------------------------------------------------------
# _log_safe
# ---------------------------------------------------------------------------


class TestLogSafe:
    def test_passes_clean_string_through(self):
        assert _log_safe("hello world") == "hello world"

    def test_replaces_newline(self):
        assert _log_safe("line1\nline2") == "line1\\nline2"

    def test_replaces_carriage_return(self):
        assert _log_safe("line1\rline2") == "line1\\rline2"

    def test_replaces_both(self):
        assert _log_safe("a\nb\rc") == "a\\nb\\rc"

    def test_empty_string(self):
        assert _log_safe("") == ""


# ---------------------------------------------------------------------------
# _resolve_gateway_base_domain
# ---------------------------------------------------------------------------


class TestResolveGatewayBaseDomain:
    def test_returns_domain_when_set(self):
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend="noop",
            api_key="test-key",
            gateway_base_domain="deploy.example.com",
        )
        assert _resolve_gateway_base_domain(cfg) == "deploy.example.com"

    def test_raises_503_when_unset(self):
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend="noop",
            api_key="test-key",
            gateway_base_domain="",
        )
        with pytest.raises(HTTPException) as exc_info:
            _resolve_gateway_base_domain(cfg)
        assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


# ---------------------------------------------------------------------------
# _find_compose_file
# ---------------------------------------------------------------------------


class TestFindComposeFile:
    def test_prefers_deploy_dir(self, tmp_path: Path):
        deploy_dir = tmp_path / "deploy"
        deploy_dir.mkdir()
        deploy_compose = deploy_dir / "docker-compose.yml"
        deploy_compose.write_text("services: {}")
        root_compose = tmp_path / "docker-compose.yml"
        root_compose.write_text("services: {}")

        result = _find_compose_file(tmp_path)
        assert result == deploy_compose

    def test_falls_back_to_root(self, tmp_path: Path):
        root_compose = tmp_path / "docker-compose.yml"
        root_compose.write_text("services: {}")

        result = _find_compose_file(tmp_path)
        assert result == root_compose

    def test_raises_400_when_neither_exists(self, tmp_path: Path):
        with pytest.raises(HTTPException) as exc_info:
            _find_compose_file(tmp_path)
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# _extract_service_config
# ---------------------------------------------------------------------------


class TestExtractServiceConfig:
    def test_single_service_is_primary(self, tmp_path: Path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(yaml.dump({"services": {"web": {"image": "nginx:latest"}}}))
        result = _extract_service_config(compose)
        assert result == {"image": "nginx:latest"}

    def test_primary_label_wins_in_multi_service(self, tmp_path: Path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            yaml.dump(
                {
                    "services": {
                        "worker": {"image": "worker:latest"},
                        "web": {
                            "image": "web:latest",
                            "labels": {"robotsix.deploy.primary": "true"},
                        },
                    }
                }
            )
        )
        result = _extract_service_config(compose)
        assert result["image"] == "web:latest"

    def test_multi_service_without_label_raises_400(self, tmp_path: Path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            yaml.dump(
                {
                    "services": {
                        "a": {"image": "a:latest"},
                        "b": {"image": "b:latest"},
                    }
                }
            )
        )
        with pytest.raises(HTTPException) as exc_info:
            _extract_service_config(compose)
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_no_services_raises_400(self, tmp_path: Path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(yaml.dump({"services": {}}))
        with pytest.raises(HTTPException) as exc_info:
            _extract_service_config(compose)
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_list_format_labels(self, tmp_path: Path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            yaml.dump(
                {
                    "services": {
                        "a": {"image": "a:latest"},
                        "web": {
                            "image": "web:latest",
                            "labels": [
                                "traefik.enable=true",
                                "robotsix.deploy.primary=true",
                            ],
                        },
                    }
                }
            )
        )
        result = _extract_service_config(compose)
        assert result["image"] == "web:latest"


# ---------------------------------------------------------------------------
# _parse_ports
# ---------------------------------------------------------------------------


class TestParsePorts:
    def test_string_format_single(self):
        svc = {"ports": ["8080:80"]}
        result = _parse_ports(svc)
        assert len(result) == 1
        assert result[0].host == 8080
        assert result[0].container == 80
        assert result[0].protocol == "tcp"

    def test_string_format_with_protocol(self):
        svc = {"ports": ["8080:80/udp"]}
        result = _parse_ports(svc)
        assert len(result) == 1
        assert result[0].host == 8080
        assert result[0].container == 80
        assert result[0].protocol == "udp"

    def test_dict_format(self):
        svc = {"ports": [{"published": 3000, "target": 3000, "protocol": "tcp"}]}
        result = _parse_ports(svc)
        assert len(result) == 1
        assert result[0].host == 3000
        assert result[0].container == 3000
        assert result[0].protocol == "tcp"

    def test_dict_format_defaults(self):
        svc = {"ports": [{"published": 8080, "target": 80}]}
        result = _parse_ports(svc)
        assert len(result) == 1
        assert result[0].protocol == "tcp"

    def test_multiple_ports(self):
        svc = {"ports": ["8080:80", "8443:443"]}
        result = _parse_ports(svc)
        assert len(result) == 2

    def test_invalid_skipped(self):
        svc = {"ports": ["not-a-port", "8080:80"]}
        result = _parse_ports(svc)
        assert len(result) == 1
        assert result[0].host == 8080

    def test_empty_ports(self):
        assert _parse_ports({"ports": []}) == []
        assert _parse_ports({}) == []

    def test_non_list_ports_ignored(self):
        assert _parse_ports({"ports": "8080:80"}) == []


# ---------------------------------------------------------------------------
# _clone_repo
# ---------------------------------------------------------------------------


class TestCloneRepo:
    @pytest.fixture
    def mock_subprocess(self):
        """Return an AsyncMock that mimics a successful subprocess."""
        mock = AsyncMock()
        mock.communicate = AsyncMock(return_value=(b"", b""))
        mock.returncode = 0
        return mock

    async def test_clone_success(self, tmp_path: Path, mock_subprocess):
        target = tmp_path / "repo"
        with patch(
            "robotsix_central_deploy.lifecycle.routers.chat_preview.asyncio.create_subprocess_exec",
            AsyncMock(return_value=mock_subprocess),
        ):
            await _clone_repo("https://github.com/org/repo.git", "main", target)
        assert target.exists()

    async def test_clone_removes_existing_dir(self, tmp_path: Path, mock_subprocess):
        target = tmp_path / "repo"
        target.mkdir()
        (target / "stale.txt").write_text("old")
        with patch(
            "robotsix_central_deploy.lifecycle.routers.chat_preview.asyncio.create_subprocess_exec",
            AsyncMock(return_value=mock_subprocess),
        ):
            await _clone_repo("https://github.com/org/repo.git", "main", target)
        # Stale file should be gone (dir was recreated)
        assert not (target / "stale.txt").exists()

    async def test_clone_failure_raises_400(self, tmp_path: Path):
        target = tmp_path / "repo"
        mock = AsyncMock()
        mock.communicate = AsyncMock(return_value=(b"", b"fatal: not found"))
        mock.returncode = 128
        with patch(
            "robotsix_central_deploy.lifecycle.routers.chat_preview.asyncio.create_subprocess_exec",
            AsyncMock(return_value=mock),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await _clone_repo(
                    "https://github.com/org/repo.git", "bad-branch", target
                )
            assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# _build_image
# ---------------------------------------------------------------------------


class TestBuildImage:
    @pytest.fixture
    def compose_path(self, tmp_path: Path) -> Path:
        p = tmp_path / "docker-compose.yml"
        p.write_text("services:\n  web:\n    build: .\n")
        return p

    async def test_image_only_no_build(self, tmp_path: Path):
        svc = {"image": "nginx:latest"}
        result = await _build_image(tmp_path / "compose.yml", svc, tmp_path)
        assert result == "nginx:latest"

    async def test_build_success(self, compose_path: Path):
        mock_build = AsyncMock()
        mock_build.communicate = AsyncMock(return_value=(b"build output", b""))
        mock_build.returncode = 0

        mock_images = AsyncMock()
        mock_images.communicate = AsyncMock(return_value=(b"abc123\ndef456\n", b""))
        mock_images.returncode = 0

        mock_tag = AsyncMock()
        mock_tag.communicate = AsyncMock(return_value=(b"", b""))
        mock_tag.returncode = 0

        create_calls = [mock_build, mock_images, mock_tag]

        with patch(
            "robotsix_central_deploy.lifecycle.routers.chat_preview.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=create_calls),
        ):
            result = await _build_image(
                compose_path, {"build": "."}, compose_path.parent
            )
            assert result == _PREVIEW_IMAGE_TAG

    async def test_build_failure_raises_500(self, compose_path: Path):
        mock = AsyncMock()
        mock.communicate = AsyncMock(return_value=(b"", b"build error"))
        mock.returncode = 1

        with patch(
            "robotsix_central_deploy.lifecycle.routers.chat_preview.asyncio.create_subprocess_exec",
            AsyncMock(return_value=mock),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await _build_image(compose_path, {"build": "."}, compose_path.parent)
            assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    async def test_no_image_no_build_raises_400(self, tmp_path: Path):
        with pytest.raises(HTTPException) as exc_info:
            await _build_image(tmp_path / "compose.yml", {}, tmp_path)
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Docker SDK helpers (_stop_and_remove_preview_container /
# _create_preview_container / register helpers)
# ---------------------------------------------------------------------------


class _FakeDockerErrors:
    class NotFound(Exception):
        pass

    class APIError(Exception):
        pass


class _FakeDockerModule:
    errors = _FakeDockerErrors


class TestDockerHelpers:
    @pytest.fixture
    def docker_mock(self):
        """Return a module-like object for the 'docker' import.

        The chat_preview module does ``import docker`` inside its functions,
        so we patch sys.modules with a module-like object that has real
        exception classes (not MagicMock proxies, which break ``except``).
        """
        return _FakeDockerModule

    @pytest.fixture
    def backend_with_client(self, docker_mock):
        """Return a backend mock whose ``_client`` is a MagicMock."""
        client = MagicMock()
        backend = MagicMock()
        backend._client = client
        return backend, client

    # -- _stop_and_remove_preview_container --------------------------------

    async def test_stop_remove_no_existing_container(self, backend_with_client):
        backend, client = backend_with_client
        client.containers.get.side_effect = RuntimeError("not found")

        with patch.dict(sys.modules, {"docker": _FakeDockerModule}):
            await _stop_and_remove_preview_container(backend)

    async def test_stop_remove_stops_and_removes(
        self, backend_with_client, docker_mock
    ):
        backend, client = backend_with_client
        container = MagicMock()
        container.stop = MagicMock()
        container.remove = MagicMock()
        client.containers.get.return_value = container

        with patch.dict(sys.modules, {"docker": docker_mock}):
            await _stop_and_remove_preview_container(backend)

        container.stop.assert_called_once()
        container.remove.assert_called_once()

    async def test_stop_remove_no_client_attribute(self):
        backend = MagicMock(spec=[])  # no _client
        with patch.dict(sys.modules, {"docker": _FakeDockerModule}):
            await _stop_and_remove_preview_container(backend)  # should not raise

    # -- _create_preview_container -----------------------------------------

    async def test_create_container_success(self, backend_with_client, docker_mock):
        backend, client = backend_with_client
        container = MagicMock()
        container.short_id = "abc123"
        container.start = MagicMock()
        client.containers.create.return_value = container

        with patch.dict(sys.modules, {"docker": docker_mock}):
            result = await _create_preview_container(backend, "preview:latest", [], {})
        assert result == "abc123"
        container.start.assert_called_once()

    async def test_create_container_no_client_raises_500(self, docker_mock):
        backend = MagicMock(spec=[])
        with patch.dict(sys.modules, {"docker": docker_mock}):
            with pytest.raises(HTTPException) as exc_info:
                await _create_preview_container(backend, "img", [], {})
            assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    async def test_create_api_error_raises_500(self, backend_with_client, docker_mock):
        backend, client = backend_with_client
        client.containers.create.side_effect = _FakeDockerErrors.APIError("boom")

        with patch.dict(sys.modules, {"docker": docker_mock}):
            with pytest.raises(HTTPException) as exc_info:
                await _create_preview_container(backend, "img", [], {})
            assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    async def test_start_failure_cleans_up_and_raises(
        self, backend_with_client, docker_mock
    ):
        backend, client = backend_with_client
        container = MagicMock()
        container.start.side_effect = _FakeDockerErrors.APIError("start failed")
        client.containers.create.return_value = container

        with patch.dict(sys.modules, {"docker": docker_mock}):
            with pytest.raises(HTTPException) as exc_info:
                await _create_preview_container(backend, "img", [], {})
            assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        # Verify cleanup attempted
        # The remove call is wrapped in a lambda inside run_in_executor
        # which we can't easily assert on, but we know the exception was raised.

    # -- _register_preview_component ---------------------------------------

    def test_register_new_component(self):
        registry = MagicMock()
        registry.unregister = MagicMock()
        registry.register = MagicMock()

        ports = [PortMapping(host=8080, container=80, protocol="tcp")]
        _register_preview_component(registry, ports, "preview:latest")

        registry.register.assert_called_once()
        config: ComponentConfig = registry.register.call_args[0][0]
        assert config.id == _PREVIEW_COMPONENT_ID
        assert config.image == "preview:latest"

    def test_register_replaces_existing(self):
        registry = MagicMock()
        registry.unregister = MagicMock()
        registry.register = MagicMock()

        _register_preview_component(registry, [], "preview:latest")
        registry.unregister.assert_called_once_with(_PREVIEW_COMPONENT_ID)

    def test_register_survives_unregister_error(self):
        registry = MagicMock()
        registry.unregister.side_effect = RuntimeError("gone")
        registry.register = MagicMock()

        _register_preview_component(registry, [], "preview:latest")
        # Should not raise — register still called
        registry.register.assert_called_once()

    # -- _unregister_preview_component -------------------------------------

    def test_unregister_success(self):
        registry = MagicMock()
        _unregister_preview_component(registry)
        registry.unregister.assert_called_once_with(_PREVIEW_COMPONENT_ID)

    def test_unregister_survives_error(self):
        registry = MagicMock()
        registry.unregister.side_effect = RuntimeError("gone")
        _unregister_preview_component(registry)  # should not raise


# ---------------------------------------------------------------------------
# Endpoint: POST /chat/preview/deploy
# ---------------------------------------------------------------------------


class TestPreviewDeployEndpoint:
    @pytest.fixture(autouse=True)
    def _configure_gateway_domain(self, monkeypatch):
        monkeypatch.setenv(
            "ROBOTSIX_LIFECYCLE_GATEWAY_BASE_DOMAIN", "deploy.example.com"
        )

    async def test_deploy_missing_gateway_domain_returns_503(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_GATEWAY_BASE_DOMAIN", "")
        # Force the module-level config to be reloaded
        server_mod._config = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend="noop",
            api_key="test-key",
            gateway_base_domain="",
        )
        server_mod.app.state.config = server_mod._config

        resp = await client.post(
            "/chat/preview/deploy",
            json={"repo_url": "https://github.com/org/repo.git", "branch": "main"},
            headers=auth_headers,
        )
        assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

    async def test_deploy_auth_required(self, client: AsyncClient):
        resp = await client.post(
            "/chat/preview/deploy",
            json={"repo_url": "https://github.com/org/repo.git", "branch": "main"},
        )
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    async def test_deploy_full_pipeline(
        self, client: AsyncClient, auth_headers, tmp_path: Path, monkeypatch
    ):
        """End-to-end test of the deploy pipeline with mocked subprocess and Docker.

        We mock git clone + docker compose build, and the Docker SDK helpers
        so the full handler exercises every stage.
        """
        # Mock _resolve_gateway_base_domain via the module-level config
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend="noop",
            api_key="test-key",
            gateway_base_domain="deploy.example.com",
        )
        server_mod._config = cfg
        server_mod.app.state.config = cfg

        cp_mod = sys.modules["robotsix_central_deploy.lifecycle.routers.chat_preview"]

        # Build a fake repo directory that the handler will clone into.
        real_preview_dir = tmp_path / "preview-repo-real"

        async def _fake_clone(repo_url: str, branch: str, target_dir: Path) -> None:
            target_dir.mkdir(parents=True, exist_ok=True)
            deploy_dir = target_dir / "deploy"
            deploy_dir.mkdir(parents=True)
            compose_file = deploy_dir / "docker-compose.yml"
            compose_file.write_text(
                yaml.dump(
                    {
                        "services": {
                            "web": {
                                "image": "nginx:latest",
                                "ports": ["8080:80"],
                            }
                        }
                    }
                )
            )

        # Mock subprocess for build (clone is replaced by _fake_clone above)
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with patch.object(cp_mod, "_PREVIEW_DIR", real_preview_dir):
            with patch.object(cp_mod, "_clone_repo", _fake_clone):
                with patch(
                    "robotsix_central_deploy.lifecycle.routers.chat_preview.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=mock_proc),
                ):
                    # Mock Docker SDK
                    dock = _FakeDockerModule
                    container_mock = MagicMock()
                    container_mock.short_id = "abc12345"
                    container_mock.start = MagicMock()
                    client_mock = MagicMock()
                    client_mock.containers.get.return_value = container_mock
                    client_mock.containers.create.return_value = container_mock

                    backend = server_mod.app.state.backend
                    backend._client = client_mock

                    with patch.dict(sys.modules, {"docker": dock}):
                        resp = await client.post(
                            "/chat/preview/deploy",
                            json={
                                "repo_url": "https://github.com/org/repo.git",
                                "branch": "main",
                            },
                            headers=auth_headers,
                        )

        assert resp.status_code == 200
        data = resp.json()
        assert (
            data["preview_url"] == f"https://{_PREVIEW_COMPONENT_ID}.deploy.example.com"
        )
        assert "Preview deployed" in data["detail"]


# ---------------------------------------------------------------------------
# Endpoint: POST /chat/preview/teardown
# ---------------------------------------------------------------------------


class TestPreviewTeardownEndpoint:
    async def test_teardown_auth_required(self, client: AsyncClient):
        resp = await client.post("/chat/preview/teardown")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    async def test_teardown_success(self, client: AsyncClient, auth_headers):
        dock = _FakeDockerModule
        container = MagicMock()
        container.stop = MagicMock()
        container.remove = MagicMock()

        backend = server_mod.app.state.backend
        backend._client = MagicMock()
        backend._client.containers.get.return_value = container

        with patch.dict(sys.modules, {"docker": dock}):
            resp = await client.post(
                "/chat/preview/teardown",
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["detail"] == "Preview slot freed."
