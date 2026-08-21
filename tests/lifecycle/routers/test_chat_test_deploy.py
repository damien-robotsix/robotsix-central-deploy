"""Tests for the chat-agent test-deploy endpoint (chat_test_deploy.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ConnectError, Response, TimeoutException

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.models import (
    DeployOutcome,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.chat_agent_audit_store import ChatAgentAuditStore
from robotsix_central_deploy.registry.models import ComponentConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_test_component(
    *, mutatable: bool = True, image: str = "ghcr.io/test/app:main"
) -> ComponentConfig:
    """Register a test component in the app's component config store."""
    cfg = ComponentConfig(
        id="test-app",
        image=image,
        container_name="test-app",
    )
    cfg.chat_agent_mutatable = mutatable
    server_mod.app.state.component_config_store.register(cfg)
    return cfg


def _mock_backend_deploy(
    *, digest: str = "sha256:abc123", previous: str = ""
) -> MagicMock:
    """Replace the backend with one whose deploy() returns a DeployOutcome."""
    mock = MagicMock()
    mock.deploy = AsyncMock(
        return_value=DeployOutcome(
            deployed_digest=digest,
            previous_digest=previous,
            state=ServiceState.RUNNING,
        )
    )
    mock.get_container_logs = AsyncMock(return_value="[test log output]\n")
    mock.stop = AsyncMock(return_value=ServiceState.STOPPED)
    mock.rollback = AsyncMock(
        return_value=MagicMock(
            deployed_digest=previous or "sha256:previous",
            state=ServiceState.RUNNING,
        )
    )
    server_mod.app.state.backend = mock
    return mock


def _make_httpx_response(status_code: int = 200, text: str = "OK") -> Response:
    """Build a synthetic httpx.Response for use with a mocked AsyncClient."""
    request = MagicMock()
    return Response(status_code, text=text, request=request)


# ---------------------------------------------------------------------------
# Happy path — probe passes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_probe_pass(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/deploy/test with a passing probe returns pass + logs."""
    _register_test_component()
    _mock_backend_deploy()

    mock_response = _make_httpx_response(200, "healthy")

    with patch(
        "robotsix_central_deploy.lifecycle.routers.chat_test_deploy.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        resp = await client.post(
            "/chat/deploy/test",
            headers=auth_headers,
            json={"stub_name": "test-app", "website": "http://localhost:8080/health"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["stub_name"] == "test-app"
    assert data["pass_fail"] == "pass"
    assert data["http_status"] == 200
    assert data["response_snippet"] == "healthy"
    assert data["container_logs"] == "[test log output]\n"
    assert data["deployed_digest"] == "sha256:abc123"
    assert "HTTP 200" in data["detail"]

    # Verify audit entry written.
    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    td_entries = [e for e in entries if e.action == "test-deploy"]
    assert len(td_entries) == 1
    assert td_entries[0].component == "test-app"
    assert "Probe passed" in td_entries[0].detail


# ---------------------------------------------------------------------------
# Failure path — probe returns HTTP 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_probe_fail_http_500(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/deploy/test with a 500 probe returns fail, logs, and rolls back."""
    _register_test_component()
    _mock_backend_deploy()

    mock_response = _make_httpx_response(500, "internal error")

    with patch(
        "robotsix_central_deploy.lifecycle.routers.chat_test_deploy.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        resp = await client.post(
            "/chat/deploy/test",
            headers=auth_headers,
            json={"stub_name": "test-app", "website": "http://localhost:8080/health"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["stub_name"] == "test-app"
    assert data["pass_fail"] == "fail"
    assert data["http_status"] == 500
    assert "internal error" in (data["response_snippet"] or "")
    assert data["container_logs"] == "[test log output]\n"

    # Verify audit entry records the failure.
    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    td_entries = [e for e in entries if e.action == "test-deploy"]
    assert len(td_entries) == 1
    assert "Probe failed" in td_entries[0].detail

    # Verify backend.stop was called (rollback).
    mock_backend = server_mod.app.state.backend
    mock_backend.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# Failure path — probe connection error (timeout)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_probe_timeout(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/deploy/test with a timed-out probe returns fail."""
    _register_test_component()
    _mock_backend_deploy()

    with patch(
        "robotsix_central_deploy.lifecycle.routers.chat_test_deploy.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=TimeoutException("timed out"))
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        resp = await client.post(
            "/chat/deploy/test",
            headers=auth_headers,
            json={"stub_name": "test-app", "website": "http://localhost:8080/health"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pass_fail"] == "fail"
    assert data["http_status"] is None
    assert "timed out" in data["detail"]


# ---------------------------------------------------------------------------
# Failure path — probe connection refused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_probe_connection_error(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/deploy/test with a connection error returns fail."""
    _register_test_component()
    _mock_backend_deploy()

    with patch(
        "robotsix_central_deploy.lifecycle.routers.chat_test_deploy.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=ConnectError("connection refused"))
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        resp = await client.post(
            "/chat/deploy/test",
            headers=auth_headers,
            json={"stub_name": "test-app", "website": "http://localhost:9999/health"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pass_fail"] == "fail"
    assert data["http_status"] is None
    assert "connection refused" in data["detail"]


# ---------------------------------------------------------------------------
# Not allowed (403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_not_allowed(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Test-deploy returns 403 when the component is not mutatable."""
    _register_test_component(mutatable=False)

    resp = await client.post(
        "/chat/deploy/test",
        headers=auth_headers,
        json={"stub_name": "test-app", "website": "http://localhost:8080/health"},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Not found — no config and no repo (404)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_no_config_no_repo(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Test-deploy returns 404 when component is not registered and no repo given."""
    # Do NOT register anything. But stub_name must pass the pattern validation.
    resp = await client.post(
        "/chat/deploy/test",
        headers=auth_headers,
        json={"stub_name": "unknown-app", "website": "http://localhost:8080/health"},
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Rate limited (429)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_rate_limited(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Second test-deploy within cooldown window returns 429."""
    _register_test_component()
    _mock_backend_deploy()

    mock_response = _make_httpx_response(200, "ok")

    with patch(
        "robotsix_central_deploy.lifecycle.routers.chat_test_deploy.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        # First call succeeds.
        resp1 = await client.post(
            "/chat/deploy/test",
            headers=auth_headers,
            json={"stub_name": "test-app", "website": "http://localhost:8080/health"},
        )
        assert resp1.status_code == 200

        # Second call within cooldown fails.
        resp2 = await client.post(
            "/chat/deploy/test",
            headers=auth_headers,
            json={"stub_name": "test-app", "website": "http://localhost:8080/health"},
        )
        assert resp2.status_code == 429, resp2.text
        assert "Rate limit" in resp2.json()["error"]


# ---------------------------------------------------------------------------
# Unauthorized (no longer 401)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_unauthorized_no_longer_401(
    client: AsyncClient,
) -> None:
    """Component-level auth was removed; test-deploy without auth header no longer returns 401."""
    _register_test_component()
    resp = await client.post(
        "/chat/deploy/test",
        json={"stub_name": "test-app", "website": "http://localhost:8080/health"},
    )
    assert resp.status_code != 401, resp.text


# ---------------------------------------------------------------------------
# Deploy lock conflict (409)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_lock_conflict(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Test-deploy returns 409 when a deploy lock is already held."""
    _register_test_component()

    from robotsix_central_deploy.lifecycle.deploy_lock import try_acquire_deploy_lock

    # Pre-acquire the lock.
    acquired = await try_acquire_deploy_lock("test-app")
    assert acquired

    try:
        resp = await client.post(
            "/chat/deploy/test",
            headers=auth_headers,
            json={"stub_name": "test-app", "website": "http://localhost:8080/health"},
        )
        assert resp.status_code == 409, resp.text
        assert "already in progress" in resp.json()["error"]
    finally:
        from robotsix_central_deploy.lifecycle.deploy_lock import (
            release_deploy_lock,
        )

        release_deploy_lock("test-app")


# ---------------------------------------------------------------------------
# Deploy-phase failure (500)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_phase_failure(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """When backend.deploy() raises, handler returns 500 with audit entry."""
    _register_test_component()
    mock_backend = _mock_backend_deploy()
    mock_backend.deploy = AsyncMock(side_effect=RuntimeError("pull failed"))

    resp = await client.post(
        "/chat/deploy/test",
        headers=auth_headers,
        json={"stub_name": "test-app", "website": "http://localhost:8080/health"},
    )

    assert resp.status_code == 500, resp.text
    assert "Test-deploy failed during deploy phase" in resp.json()["error"]

    # Verify audit entry was written.
    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    td_entries = [e for e in entries if e.action == "test-deploy"]
    assert len(td_entries) == 1
    assert "Deploy phase failed" in td_entries[0].detail


# ---------------------------------------------------------------------------
# Rollback with previous digest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_rollback_with_previous_digest(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """On probe failure, rolls back to previous digest when one exists."""
    _register_test_component()
    _mock_backend_deploy(previous="sha256:previous")

    # Pre-seed a ServiceRecord with a deployed digest so the handler
    # takes the "previous digest → rollback" branch rather than the
    # "no previous digest → stop container" branch.
    store = server_mod.app.state.store
    await store.put(
        ServiceRecord(
            name="test-app",
            state=ServiceState.RUNNING,
            deployed_image_digest="sha256:previous",
        )
    )

    mock_response = _make_httpx_response(500, "internal error")

    with patch(
        "robotsix_central_deploy.lifecycle.routers.chat_test_deploy.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        resp = await client.post(
            "/chat/deploy/test",
            headers=auth_headers,
            json={"stub_name": "test-app", "website": "http://localhost:8080/health"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pass_fail"] == "fail"
    assert "Rolled back to" in data["detail"]

    # Verify backend.rollback was called (not just stop).
    mock_backend = server_mod.app.state.backend
    mock_backend.rollback.assert_awaited_once()
    mock_backend.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# Rollback failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_rollback_failure(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """When rollback itself raises, handler catches and reports it inline."""
    _register_test_component()
    mock_backend = _mock_backend_deploy()
    mock_backend.stop = AsyncMock(side_effect=RuntimeError("cannot stop"))

    mock_response = _make_httpx_response(500, "internal error")

    with patch(
        "robotsix_central_deploy.lifecycle.routers.chat_test_deploy.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        resp = await client.post(
            "/chat/deploy/test",
            headers=auth_headers,
            json={"stub_name": "test-app", "website": "http://localhost:8080/health"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pass_fail"] == "fail"
    assert "Rollback also failed" in data["detail"]
