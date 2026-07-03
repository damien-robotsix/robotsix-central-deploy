"""Tests for POST /services/{name}/config/refresh-schema."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.onboard.fetcher import RepoFiles
from robotsix_central_deploy.registry.models import ComponentConfig

HEADERS = {"X-API-Key": "test-key"}

FRESH_SCHEMA = {
    "type": "object",
    "properties": {
        "host": {"type": "string", "description": "Hostname to connect to."},
        "port": {"type": "integer", "description": "Port to connect to."},
    },
}


@pytest.fixture
async def client_with_legacy_component() -> AsyncClient:
    """Component with a git_url and a stored legacy (non-JSON-Schema) template."""
    from robotsix_central_deploy.lifecycle.models import ServiceRecord

    store = server_mod.app.state.store
    config_yaml_store = server_mod.app.state.config_yaml_store
    component_config_store = server_mod.app.state.component_config_store

    comp = ComponentConfig(
        id="legacy-comp",
        image="ghcr.io/org/legacy:latest",
        container_name="legacy-comp",
        has_config_yaml=True,
        config_volume="legacy-comp-config",
        git_url="https://github.com/org/legacy.git",
    )
    await component_config_store.put(comp)
    await store.put(
        ServiceRecord(name="legacy-comp", image="ghcr.io/org/legacy:latest")
    )
    # Legacy raw template, as captured by a pre-schema onboard
    await config_yaml_store.save_template(
        "legacy-comp", {"host": "localhost", "port": 8080, "api_key": "SECRET"}
    )

    transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_refresh_replaces_template_with_repo_schema(
    client_with_legacy_component: AsyncClient,
) -> None:
    repo_files = RepoFiles(
        compose_bytes=b"services: {}",
        config_yaml=None,
        config_yaml_template=None,
        config_schema_json=json.dumps(FRESH_SCHEMA).encode(),
    )
    with patch(
        "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
        return_value=repo_files,
    ):
        resp = await client_with_legacy_component.post(
            "/services/legacy-comp/config/refresh-schema", headers=HEADERS
        )
    assert resp.status_code == 200
    assert resp.json()["schema"] == FRESH_SCHEMA

    stored = await server_mod.app.state.config_yaml_store.get_template("legacy-comp")
    assert stored == FRESH_SCHEMA


@pytest.mark.asyncio
async def test_refresh_404_when_repo_has_no_schema(
    client_with_legacy_component: AsyncClient,
) -> None:
    repo_files = RepoFiles(
        compose_bytes=b"services: {}",
        config_yaml=b"host: x",
        config_yaml_template=None,
        config_schema_json=None,
    )
    with patch(
        "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
        return_value=repo_files,
    ):
        resp = await client_with_legacy_component.post(
            "/services/legacy-comp/config/refresh-schema", headers=HEADERS
        )
    assert resp.status_code == 404
    # Stored template untouched
    stored = await server_mod.app.state.config_yaml_store.get_template("legacy-comp")
    assert stored == {"host": "localhost", "port": 8080, "api_key": "SECRET"}


@pytest.mark.asyncio
async def test_refresh_400_without_git_url(
    client_with_legacy_component: AsyncClient,
) -> None:
    ccs = server_mod.app.state.component_config_store
    comp = ccs.get("legacy-comp")
    await ccs.put(comp.model_copy(update={"git_url": ""}))

    resp = await client_with_legacy_component.post(
        "/services/legacy-comp/config/refresh-schema", headers=HEADERS
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_refresh_422_on_invalid_schema_json(
    client_with_legacy_component: AsyncClient,
) -> None:
    repo_files = RepoFiles(
        compose_bytes=b"services: {}",
        config_yaml=None,
        config_yaml_template=None,
        config_schema_json=b"{not json",
    )
    with patch(
        "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
        return_value=repo_files,
    ):
        resp = await client_with_legacy_component.post(
            "/services/legacy-comp/config/refresh-schema", headers=HEADERS
        )
    assert resp.status_code == 422
