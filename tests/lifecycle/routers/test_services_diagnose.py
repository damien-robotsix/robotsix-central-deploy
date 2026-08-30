"""Tests for the diagnose endpoint and verdict rules."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle._diagnose import _compute_verdict
from robotsix_central_deploy.lifecycle.models import ServiceRecord, ServiceState
from robotsix_central_deploy.lifecycle.schemas import (
    DiagnoseEdgeProbe,
    DiagnoseRepoContract,
    DiagnoseRouting,
    DiagnoseRuntime,
)
from robotsix_central_deploy.registry.constants import PROXY_NETWORK
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    HealthCheck,
    PortMapping,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    id: str = "test-svc",
    ports: list[PortMapping] | None = None,
    routable: bool = True,
    git_url: str = "",
    health_check: HealthCheck | None = None,
) -> ComponentConfig:
    """Build a minimal ComponentConfig for testing."""
    cfg = ComponentConfig(
        id=id,
        image=f"{id}:latest",
        container_name=id,
        ports=ports if ports is not None else [PortMapping(host=8080, container=8080)],
        routable=routable,
        git_url=git_url,
        health_check=health_check,
    )
    return cfg


def _register_component(
    cfg: ComponentConfig,
    *,
    mutatable: bool = True,
    allow_chat_access: bool = True,
) -> None:
    """Register a component in the app's config store and registry."""
    cfg.chat_agent_mutatable = mutatable
    cfg.allow_chat_access = allow_chat_access
    server_mod.app.state.component_config_store.register(cfg)
    server_mod.app.state.registry.register(cfg)


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
# Verdict rule unit tests
# ---------------------------------------------------------------------------


class TestVerdictRules:
    """Test the _compute_verdict function directly."""

    def test_not_routable(self) -> None:
        """Verdict is not-routable when config.routable is False."""
        config = _make_config(routable=False)
        verdict = _compute_verdict(
            config=config,
            repo_contract=None,
            routing=DiagnoseRouting(
                expected_labels={},
                actual_labels={},
                container_networks={},
                proxy_network_attached=False,
            ),
            edge_probe=None,
            runtime=None,
        )
        assert verdict.classification == "not-routable"
        assert "routable=false" in verdict.detail

    def test_no_proxy_network(self) -> None:
        """Verdict is no-proxy-network when container exists but proxy net is absent."""
        config = _make_config()
        runtime = DiagnoseRuntime(
            container_state="running",
            health="healthy",
            started_at="2026-01-01T00:00:00Z",
            restart_count=0,
            image_digest="sha256:abc",
            registry_digest="sha256:abc",
            recent_logs="",
        )
        verdict = _compute_verdict(
            config=config,
            repo_contract=None,
            routing=DiagnoseRouting(
                expected_labels={"traefik.enable": "true"},
                actual_labels={"traefik.enable": "true"},
                container_networks={"bridge": {"aliases": [], "ip": "172.17.0.2"}},
                proxy_network_attached=False,
            ),
            edge_probe=None,
            runtime=runtime,
        )
        assert verdict.classification == "no-proxy-network"
        assert "central-deploy-proxy" in verdict.detail

    def test_stale_contract_refresh_error(self) -> None:
        """Verdict is stale-contract when repo fetch fails."""
        config = _make_config(git_url="https://github.com/org/repo.git")
        repo_contract = DiagnoseRepoContract(
            fetched=False,
            error="could not read Username for 'https://github.com'",
        )
        verdict = _compute_verdict(
            config=config,
            repo_contract=repo_contract,
            routing=DiagnoseRouting(
                expected_labels={},
                actual_labels={},
                container_networks={PROXY_NETWORK: {"aliases": [], "ip": ""}},
                proxy_network_attached=True,
            ),
            edge_probe=None,
            runtime=DiagnoseRuntime(
                container_state="running",
                health="",
                started_at="",
                restart_count=0,
                image_digest="",
                registry_digest="",
                recent_logs="",
            ),
        )
        assert verdict.classification == "stale-contract"
        assert "could not read Username" in verdict.detail
        assert "refresh-contract" in verdict.remediation

    def test_stale_contract_changed_fields(self) -> None:
        """Verdict is stale-contract when stored spec differs from repo contract."""
        config = _make_config(git_url="https://github.com/org/repo.git")
        repo_contract = DiagnoseRepoContract(
            fetched=True,
            changed_fields=["ports", "health_check"],
            previous={"ports": [], "health_check": None},
            current={
                "ports": [{"host": 8080, "container": 8080, "protocol": "tcp"}],
                "health_check": {
                    "test": ["CMD", "curl", "-f", "http://localhost:8080/"]
                },
            },
        )
        verdict = _compute_verdict(
            config=config,
            repo_contract=repo_contract,
            routing=DiagnoseRouting(
                expected_labels={},
                actual_labels={},
                container_networks={PROXY_NETWORK: {"aliases": [], "ip": ""}},
                proxy_network_attached=True,
            ),
            edge_probe=None,
            runtime=DiagnoseRuntime(
                container_state="running",
                health="",
                started_at="",
                restart_count=0,
                image_digest="",
                registry_digest="",
                recent_logs="",
            ),
        )
        assert verdict.classification == "stale-contract"
        assert "ports" in verdict.detail
        assert "health_check" in verdict.detail
        assert "refresh-contract" in verdict.remediation

    def test_unhealthy(self) -> None:
        """Verdict is unhealthy when container health check fails."""
        config = _make_config()
        runtime = DiagnoseRuntime(
            container_state="running",
            health="unhealthy",
            started_at="2026-01-01T00:00:00Z",
            restart_count=0,
            image_digest="sha256:abc",
            registry_digest="sha256:abc",
            recent_logs="",
        )
        verdict = _compute_verdict(
            config=config,
            repo_contract=None,
            routing=DiagnoseRouting(
                expected_labels={"traefik.enable": "true"},
                actual_labels={"traefik.enable": "true"},
                container_networks={PROXY_NETWORK: {"aliases": [], "ip": ""}},
                proxy_network_attached=True,
            ),
            edge_probe=None,
            runtime=runtime,
        )
        assert verdict.classification == "unhealthy"
        assert "unhealthy" in verdict.detail

    def test_edge_mismatch_labels_differ(self) -> None:
        """Verdict is edge-mismatch when traefik labels differ."""
        config = _make_config()
        expected = {
            "traefik.enable": "true",
            "traefik.http.routers.test-svc.rule": "Host(`test-svc.example.com`)",
        }
        actual = {
            "traefik.enable": "true",
            # Missing router rule
        }
        verdict = _compute_verdict(
            config=config,
            repo_contract=None,
            routing=DiagnoseRouting(
                expected_labels=expected,
                actual_labels=actual,
                container_networks={PROXY_NETWORK: {"aliases": [], "ip": ""}},
                proxy_network_attached=True,
            ),
            edge_probe=None,
            runtime=DiagnoseRuntime(
                container_state="running",
                health="healthy",
                started_at="",
                restart_count=0,
                image_digest="",
                registry_digest="",
                recent_logs="",
            ),
        )
        assert verdict.classification == "edge-mismatch"
        assert "labels" in verdict.detail.lower()

    def test_edge_mismatch_http_error(self) -> None:
        """Verdict is edge-mismatch when edge probe returns 404."""
        config = _make_config()
        edge_probe = DiagnoseEdgeProbe(
            url="https://test-svc.example.com/health",
            status_code=404,
            body_preview="404 page not found",
        )
        verdict = _compute_verdict(
            config=config,
            repo_contract=None,
            routing=DiagnoseRouting(
                expected_labels={"traefik.enable": "true"},
                actual_labels={"traefik.enable": "true"},
                container_networks={PROXY_NETWORK: {"aliases": [], "ip": ""}},
                proxy_network_attached=True,
            ),
            edge_probe=edge_probe,
            runtime=DiagnoseRuntime(
                container_state="running",
                health="healthy",
                started_at="",
                restart_count=0,
                image_digest="",
                registry_digest="",
                recent_logs="",
            ),
        )
        assert verdict.classification == "edge-mismatch"
        assert "404" in verdict.detail

    def test_ok(self) -> None:
        """Verdict is ok when everything looks good."""
        config = _make_config()
        verdict = _compute_verdict(
            config=config,
            repo_contract=DiagnoseRepoContract(fetched=True, changed_fields=[]),
            routing=DiagnoseRouting(
                expected_labels={"traefik.enable": "true"},
                actual_labels={"traefik.enable": "true"},
                container_networks={PROXY_NETWORK: {"aliases": [], "ip": ""}},
                proxy_network_attached=True,
            ),
            edge_probe=DiagnoseEdgeProbe(
                url="https://test-svc.example.com/health",
                status_code=200,
                body_preview='{"status": "ok"}',
            ),
            runtime=DiagnoseRuntime(
                container_state="running",
                health="healthy",
                started_at="2026-01-01T00:00:00Z",
                restart_count=0,
                image_digest="sha256:abc",
                registry_digest="sha256:abc",
                recent_logs="",
            ),
        )
        assert verdict.classification == "ok"
        assert "No issues" in verdict.detail

    def test_hexarchy_scenario(self) -> None:
        """Hexarchy scenario: stored ports empty, refresh failing → stale-contract."""
        config = _make_config(
            ports=[],  # Stale onboard-time contract: no ports
            git_url="https://github.com/org/hexarchy.git",
        )
        repo_contract = DiagnoseRepoContract(
            fetched=False,
            error="could not read Username for 'https://github.com'",
        )
        routing = DiagnoseRouting(
            expected_labels={},  # No labels because no ports
            actual_labels={},  # No labels on container either
            container_networks={PROXY_NETWORK: {"aliases": [], "ip": ""}},
            proxy_network_attached=True,
        )
        verdict = _compute_verdict(
            config=config,
            repo_contract=repo_contract,
            routing=routing,
            edge_probe=None,
            runtime=DiagnoseRuntime(
                container_state="running",
                health="healthy",
                started_at="",
                restart_count=0,
                image_digest="",
                registry_digest="",
                recent_logs="",
            ),
        )
        assert verdict.classification == "stale-contract"
        assert "could not read Username" in verdict.detail
        assert "refresh-contract" in verdict.remediation


# ---------------------------------------------------------------------------
# Integration tests for the endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnose_not_found(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Diagnose returns 404 for an unknown component."""
    resp = await client.get("/services/unknown/diagnose", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_diagnose_basic(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Diagnose returns a valid report for a registered component."""
    cfg = _make_config()
    _register_component(cfg)
    await _seed_service_record()

    # Mock the backend to return container diagnostics with labels matching
    # what traefik_labels() would generate for test-svc on example.com
    expected_labels = {
        "traefik.enable": "true",
        "traefik.docker.network": PROXY_NETWORK,
        "traefik.http.services.test-svc.loadbalancer.server.port": "8080",
        "traefik.http.routers.test-svc-health.rule": "Host(`test-svc.example.com`) && Path(`/health`)",
        "traefik.http.routers.test-svc-health.priority": "30",
        "traefik.http.routers.test-svc-health.entrypoints": "websecure",
        "traefik.http.routers.test-svc-health.service": "test-svc",
        "traefik.http.routers.test-svc-bearer.rule": "Host(`test-svc.example.com`) && HeadersRegexp(`Authorization`, `^Bearer .+`)",
        "traefik.http.routers.test-svc-bearer.priority": "20",
        "traefik.http.routers.test-svc-bearer.entrypoints": "websecure",
        "traefik.http.routers.test-svc-bearer.service": "test-svc",
        "traefik.http.routers.test-svc-bearer.middlewares": "mobile-token@file",
        "traefik.http.routers.test-svc.rule": "Host(`test-svc.example.com`)",
        "traefik.http.routers.test-svc.priority": "10",
        "traefik.http.routers.test-svc.entrypoints": "websecure",
        "traefik.http.routers.test-svc.service": "test-svc",
        "traefik.http.routers.test-svc.middlewares": "tinyauth@file",
    }
    mock_backend = MagicMock()
    mock_backend.get_container_diagnostics = AsyncMock(
        return_value={
            "exists": True,
            "labels": expected_labels,
            "networks": {PROXY_NETWORK: {"aliases": [], "ip": "172.20.0.2"}},
            "state": "running",
            "health": "healthy",
            "started_at": "2026-01-01T00:00:00Z",
            "restart_count": 0,
            "image_digest": "sha256:abc123",
        }
    )
    mock_backend.get_container_logs = AsyncMock(return_value="log line 1\nlog line 2\n")
    server_mod.app.state.backend = mock_backend

    # Set gateway_base_domain so traefik_labels generates labels
    server_mod.app.state.config.gateway_base_domain = "example.com"

    resp = await client.get("/services/test-svc/diagnose", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "test-svc"
    assert data["stored_spec"]["image"] == "test-svc:latest"
    assert data["stored_spec"]["routable"] is True
    assert data["routing"]["proxy_network_attached"] is True
    assert data["verdict"]["classification"] == "ok"


@pytest.mark.asyncio
async def test_diagnose_stale_contract_integration(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Diagnose returns stale-contract verdict when ports are empty and refresh fails."""
    cfg = _make_config(ports=[], git_url="https://github.com/org/repo.git")
    _register_component(cfg)
    await _seed_service_record()

    mock_backend = MagicMock()
    mock_backend.get_container_diagnostics = AsyncMock(
        return_value={
            "exists": True,
            "labels": {},
            "networks": {PROXY_NETWORK: {"aliases": [], "ip": ""}},
            "state": "running",
            "health": "",
            "started_at": "",
            "restart_count": 0,
            "image_digest": "",
        }
    )
    mock_backend.get_container_logs = AsyncMock(return_value="")
    server_mod.app.state.backend = mock_backend
    server_mod.app.state.config.gateway_base_domain = "example.com"

    # Mock _fetch_and_compare_contract to simulate a fetch failure
    with patch(
        "robotsix_central_deploy.lifecycle._diagnose._fetch_and_compare_contract",
        new_callable=AsyncMock,
        return_value=DiagnoseRepoContract(
            fetched=False,
            error="could not read Username for 'https://github.com'",
        ),
    ):
        resp = await client.get("/services/test-svc/diagnose", headers=auth_headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"]["classification"] == "stale-contract"
    assert "could not read Username" in data["verdict"]["detail"]
    assert "refresh-contract" in data["verdict"]["remediation"]


@pytest.mark.asyncio
async def test_chat_diagnose_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Chat diagnose returns 403 when component is not allowlisted."""
    cfg = _make_config()
    cfg.chat_agent_mutatable = False
    cfg.allow_chat_access = False
    server_mod.app.state.component_config_store.register(cfg)
    server_mod.app.state.registry.register(cfg)

    resp = await client.get("/chat/services/test-svc/diagnose", headers=auth_headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_chat_diagnose_allowed(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Chat diagnose succeeds when component is allowlisted."""
    cfg = _make_config()
    _register_component(cfg, allow_chat_access=True)
    await _seed_service_record()

    mock_backend = MagicMock()
    mock_backend.get_container_diagnostics = AsyncMock(return_value={"exists": False})
    mock_backend.get_container_logs = AsyncMock(return_value="")
    server_mod.app.state.backend = mock_backend
    server_mod.app.state.config.gateway_base_domain = "example.com"

    resp = await client.get("/chat/services/test-svc/diagnose", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "test-svc"
    assert data["verdict"]["classification"] == "ok"
