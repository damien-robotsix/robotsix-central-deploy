"""Tests for the chat observability endpoints (chat_observability.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.models import ServiceRecord, ServiceState
from robotsix_central_deploy.registry.models import ComponentConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_component(
    id: str = "test-svc",
    *,
    mutatable: bool = True,
    allow_chat_access: bool = False,
    named_volumes: list[str] | None = None,
) -> ComponentConfig:
    """Register a component in the app's config store and registry."""
    cfg = ComponentConfig(
        id=id,
        image=f"{id}:latest",
        container_name=id,
        named_volumes=named_volumes or [],
    )
    cfg.chat_agent_mutatable = mutatable
    cfg.allow_chat_access = allow_chat_access
    server_mod.app.state.component_config_store.register(cfg)
    server_mod.app.state.registry.register(cfg)
    return cfg


async def _seed_service_record(
    name: str = "test-svc",
    state: ServiceState = ServiceState.RUNNING,
    image: str = "test-svc:latest",
) -> ServiceRecord:
    """Create and persist a ServiceRecord in the store."""
    record = ServiceRecord(name=name, state=state, image=image)
    await server_mod.app.state.store.put(record)
    return record


# ---------------------------------------------------------------------------
# GET /chat/services/{name}/logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logs_mutatable(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Logs succeed when chat_agent_mutatable is True."""
    _register_component("test-svc", mutatable=True)
    await _seed_service_record("test-svc")

    mock = MagicMock()
    mock.stream_logs = MagicMock()
    mock.stream_logs.return_value = _async_iter(b"hello world\n")
    server_mod.app.state.backend = mock

    resp = await client.get(
        "/chat/services/test-svc/logs",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert "hello world" in resp.text


@pytest.mark.asyncio
async def test_logs_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Logs return 403 when component is not allowlisted."""
    _register_component("test-svc", mutatable=False, allow_chat_access=False)
    await _seed_service_record("test-svc")

    resp = await client.get(
        "/chat/services/test-svc/logs",
        headers=auth_headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_logs_allow_chat_access(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Logs succeed when allow_chat_access is True (but mutatable is False)."""
    _register_component("test-svc", mutatable=False, allow_chat_access=True)
    await _seed_service_record("test-svc")

    mock = MagicMock()
    mock.stream_logs = MagicMock()
    mock.stream_logs.return_value = _async_iter(b"data\n")
    server_mod.app.state.backend = mock

    resp = await client.get(
        "/chat/services/test-svc/logs",
        headers=auth_headers,
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_logs_capped(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Logs response is capped at ~256 KiB."""
    _register_component("test-svc", mutatable=True)
    await _seed_service_record("test-svc")

    # Simulate a backend that produces a lot of data.
    mock = MagicMock()
    mock.stream_logs = MagicMock()
    mock.stream_logs.return_value = _async_iter(b"x" * 300_000)
    server_mod.app.state.backend = mock

    resp = await client.get(
        "/chat/services/test-svc/logs",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    # Should be capped at ~256 KiB (262144 bytes).
    assert len(resp.content) <= 262_144


# ---------------------------------------------------------------------------
# GET /chat/services/{name}/status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_mutatable(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Status returns structured JSON when chat_agent_mutatable is True."""
    _register_component("test-svc", mutatable=True)
    await _seed_service_record("test-svc")

    mock = MagicMock()
    mock.status = AsyncMock()
    mock.status.return_value = MagicMock(
        state=ServiceState.RUNNING,
        image_revision="abc123",
        health="healthy",
        running_digest="sha256:def456",
    )
    server_mod.app.state.backend = mock

    resp = await client.get(
        "/chat/services/test-svc/status",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "test-svc"
    assert data["state"] == "running"
    assert "health" in data


@pytest.mark.asyncio
async def test_status_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Status returns 403 when component is not allowlisted."""
    _register_component("test-svc", mutatable=False, allow_chat_access=False)
    await _seed_service_record("test-svc")

    resp = await client.get(
        "/chat/services/test-svc/status",
        headers=auth_headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_status_not_found(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Status returns 404 for unknown component."""
    resp = await client.get(
        "/chat/services/no-such-svc/status",
        headers=auth_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /chat/services/{name}/volumes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_volumes_list(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Volumes returns the component's named_volumes list."""
    _register_component("test-svc", mutatable=True, named_volumes=["vol-a", "vol-b"])

    resp = await client.get(
        "/chat/services/test-svc/volumes",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data == ["vol-a", "vol-b"]


@pytest.mark.asyncio
async def test_volumes_empty(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Volumes returns empty list when component has no named volumes."""
    _register_component("test-svc", mutatable=True, named_volumes=[])

    resp = await client.get(
        "/chat/services/test-svc/volumes",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_volumes_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Volumes returns 403 when component is not allowlisted."""
    _register_component("test-svc", mutatable=False, allow_chat_access=False)

    resp = await client.get(
        "/chat/services/test-svc/volumes",
        headers=auth_headers,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /chat/services/{name}/volumes/{vol}/files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_volume_file_read(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """File read succeeds for a component-owned volume."""
    _register_component("test-svc", mutatable=True, named_volumes=["data-vol"])

    mock = MagicMock()
    mock.read_volume_file = AsyncMock(
        return_value={
            "size_bytes": 12,
            "content": "hello world\n",
            "binary": False,
            "truncated": False,
        }
    )
    server_mod.app.state.backend = mock

    resp = await client.get(
        "/chat/services/test-svc/volumes/data-vol/files?path=config.json",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"] == "hello world\n"
    assert data["binary"] is False
    assert data["truncated"] is False


@pytest.mark.asyncio
async def test_volume_file_on_a_directory_returns_400(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Reading a directory answered 200 with an empty 4096-byte body."""
    _register_component("test-svc", mutatable=True, named_volumes=["data-vol"])

    mock = MagicMock()
    mock.read_volume_file = AsyncMock(side_effect=IsADirectoryError(""))
    server_mod.app.state.backend = mock

    resp = await client.get(
        "/chat/services/test-svc/volumes/data-vol/files?path=/",
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "is a directory" in resp.json()["error"]


@pytest.mark.asyncio
async def test_volume_file_not_owned(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """File read returns 404 when volume does not belong to component."""
    _register_component("test-svc", mutatable=True, named_volumes=["data-vol"])

    resp = await client.get(
        "/chat/services/test-svc/volumes/other-vol/files?path=foo.txt",
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_volume_file_traversal_rejected(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """File read rejects path traversal attempts."""
    _register_component("test-svc", mutatable=True, named_volumes=["data-vol"])

    resp = await client.get(
        "/chat/services/test-svc/volumes/data-vol/files?path=../../../etc/passwd",
        headers=auth_headers,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_volume_file_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """File read returns 403 when component is not allowlisted."""
    _register_component(
        "test-svc", mutatable=False, allow_chat_access=False, named_volumes=["data-vol"]
    )

    resp = await client.get(
        "/chat/services/test-svc/volumes/data-vol/files?path=foo.txt",
        headers=auth_headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_volume_file_component_not_found(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """File read returns 404 when component does not exist."""
    resp = await client.get(
        "/chat/services/no-such-svc/volumes/data-vol/files?path=foo.txt",
        headers=auth_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_registry_check(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    """Status endpoint exercises the registry-check branch when digests are present."""
    _register_component("test-svc", mutatable=True)

    record = ServiceRecord(
        name="test-svc",
        state=ServiceState.RUNNING,
        image="test-svc:latest",
    )
    record.deployed_image_digest = "sha256:old"
    record.latest_registry_digest = "sha256:old"
    record.update_available = False
    await server_mod.app.state.store.put(record)

    # Backend returns live state with a running digest.
    mock_backend = MagicMock()
    mock_backend.status = AsyncMock()
    mock_backend.status.return_value = MagicMock(
        state=ServiceState.RUNNING,
        image_revision="abc123",
        health="healthy",
        running_digest="sha256:old",
    )
    server_mod.app.state.backend = mock_backend

    # Registry checker reports a newer digest available.
    mock_checker = MagicMock()
    mock_checker.get_latest_digest = AsyncMock(return_value="sha256:new")
    monkeypatch.setattr(server_mod.app.state, "registry_checker", mock_checker)

    resp = await client.get(
        "/chat/services/test-svc/status",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    # The registry check should have set update_available = True.
    assert data["update_available"] is True
    assert data["latest_digest"] == "sha256:new"


async def _async_iter(data: bytes):
    """Yield a single chunk for a mock stream_logs return value."""
    yield data
