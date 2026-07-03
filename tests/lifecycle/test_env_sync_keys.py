"""Tests for POST /services/{name}/env/sync-keys."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.onboard.fetcher import RepoFiles
from robotsix_central_deploy.registry.models import ComponentConfig

HEADERS = {"X-API-Key": "test-key"}
SYNC_URL = "/services/env-comp/env/sync-keys"


@pytest.fixture
async def client_with_env_component() -> AsyncClient:
    from robotsix_central_deploy.lifecycle.models import ServiceRecord

    store = server_mod.app.state.store
    component_config_store = server_mod.app.state.component_config_store
    env_store = server_mod.app.state.env_store

    comp = ComponentConfig(
        id="env-comp",
        image="ghcr.io/org/env:latest",
        container_name="env-comp",
        git_url="https://github.com/org/env.git",
    )
    await component_config_store.put(comp)
    await store.put(ServiceRecord(name="env-comp", image="ghcr.io/org/env:latest"))
    # Pre-existing store: one env value the operator already customised
    await env_store.upsert("env-comp", {"LOG_LEVEL": "DEBUG"}, {"OLD_TOKEN": "x"})

    transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await env_store.delete("env-comp")


def _patched_contract(env: dict[str, str]):
    repo_files = RepoFiles(
        compose_bytes=b"services: {}",
        config_json=None,
        config_json_template=None,
        config_schema_json=None,
    )
    spec = MagicMock()
    spec.env = env
    return (
        patch(
            "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
            return_value=repo_files,
        ),
        patch(
            "robotsix_central_deploy.onboard.parser.parse_compose",
            return_value=spec,
        ),
    )


@pytest.mark.asyncio
async def test_sync_adds_new_keys_without_touching_existing(
    client_with_env_component: AsyncClient,
) -> None:
    p1, p2 = _patched_contract(
        {"LOG_LEVEL": "INFO", "NEW_URL": "https://x", "NEW_SECRET": ""}
    )
    with p1, p2:
        resp = await client_with_env_component.post(
            SYNC_URL,
            headers=HEADERS,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["added_env"] == ["NEW_URL"]
    assert body["added_secrets"] == ["NEW_SECRET"]
    assert body["undeclared"] == ["OLD_TOKEN"]

    stored = await server_mod.app.state.env_store.get("env-comp")
    # operator's customised value survives; new keys seeded
    assert stored.env["LOG_LEVEL"] == "DEBUG"
    assert stored.env["NEW_URL"] == "https://x"
    assert "NEW_SECRET" in stored.secret_tokens
    # undeclared key reported but NOT deleted
    assert "OLD_TOKEN" in stored.secret_tokens


@pytest.mark.asyncio
async def test_sync_noop_when_contract_matches(
    client_with_env_component: AsyncClient,
) -> None:
    p1, p2 = _patched_contract({"LOG_LEVEL": "INFO", "OLD_TOKEN": ""})
    with p1, p2:
        resp = await client_with_env_component.post(
            SYNC_URL,
            headers=HEADERS,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["added_env"] == []
    assert body["added_secrets"] == []
    assert body["undeclared"] == []


@pytest.mark.asyncio
async def test_sync_400_without_git_url(
    client_with_env_component: AsyncClient,
) -> None:
    ccs = server_mod.app.state.component_config_store
    comp = ccs.get("env-comp")
    await ccs.put(comp.model_copy(update={"git_url": ""}))
    resp = await client_with_env_component.post(
        SYNC_URL,
        headers=HEADERS,
    )
    assert resp.status_code == 400
