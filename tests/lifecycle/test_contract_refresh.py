"""Tests for POST /services/{name}/refresh-contract."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.lifecycle.models import ServiceRecord
from robotsix_central_deploy.onboard.fetcher import RepoFiles
from robotsix_central_deploy.onboard.models import DerivedSpec
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    PortMapping,
    VolumeMount,
)

HEADERS = {"X-API-Key": "test-key"}

ORIGINAL_COMPOSE = b"""services:
  svc:
    image: ghcr.io/org/svc:v1
    ports:
      - "8080:8080"
    volumes:
      - data:/data
    command: ["run"]
volumes:
  data:
"""

UPDATED_COMPOSE = b"""services:
  svc:
    image: ghcr.io/org/svc:v2
    ports:
      - "8080:8080"
      - "9090:9090"
    volumes:
      - data:/data
    command: ["run", "--verbose"]
    tmpfs:
      - /run
volumes:
  data:
"""


def _make_derived_spec(
    *,
    name: str = "test-comp",
    image: str = "ghcr.io/org/svc:v1",
    ports: list[PortMapping] | None = None,
    mounts: list[VolumeMount] | None = None,
    command: list[str] | None = None,
) -> DerivedSpec:
    return DerivedSpec(
        name=name,
        git_url="https://github.com/org/test.git",
        image=image,
        ports=ports or [PortMapping(host=8080, container=8080, protocol="tcp")],
        mounts=mounts or [VolumeMount(host="data", container="/data")],
        env={},
        claude_mount=False,
        host_docker_sock=False,
        health_check=None,
        command=command or ["run"],
        entrypoint=None,
        container_name="",
        siblings=[],
        config_schema=None,
        config_example_values=None,
        config_volume=None,
        config_assist_command=None,
        config_assist_seeds=[],
        llmio_tier_level=None,
        allow_chat_access=False,
    )


@pytest.fixture
async def client_with_component() -> AsyncClient:
    """Seed a component with a git_url, then yield an AsyncClient."""
    store = server_mod.app.state.store
    component_config_store = server_mod.app.state.component_config_store
    registry = server_mod.app.state.registry

    comp = ComponentConfig(
        id="test-comp",
        image="ghcr.io/org/svc:v1",
        container_name="test-comp",
        ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        mounts=[VolumeMount(host="test-comp-data", container="/data")],
        env={},
        command=["run"],
        named_volumes=["test-comp-data"],
        git_url="https://github.com/org/test.git",
    )
    await component_config_store.put(comp)
    registry.register(comp)
    await store.put(ServiceRecord(name="test-comp", image="ghcr.io/org/svc:v1"))

    transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_refresh_updates_image_and_command(
    client_with_component: AsyncClient,
) -> None:
    """When the compose changes image and command, both are updated."""
    new_spec = _make_derived_spec(
        image="ghcr.io/org/svc:v2",
        command=["run", "--verbose"],
    )
    repo_files = RepoFiles(
        compose_bytes=UPDATED_COMPOSE,
        config_json=None,
        config_json_template=None,
        config_schema_json=None,
    )
    with (
        patch(
            "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
            return_value=repo_files,
        ),
        patch(
            "robotsix_central_deploy.onboard.parser.parse_compose",
            return_value=new_spec,
        ),
    ):
        resp = await client_with_component.post(
            "/services/test-comp/refresh-contract", headers=HEADERS
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "test-comp"
    assert set(body["changed_fields"]) == {"image", "command"}
    assert body["previous"]["image"] == "ghcr.io/org/svc:v1"
    assert body["current"]["image"] == "ghcr.io/org/svc:v2"
    assert body["previous"]["command"] == ["run"]
    assert body["current"]["command"] == ["run", "--verbose"]

    # Verify store was updated
    updated = server_mod.app.state.component_config_store.get("test-comp")
    assert updated is not None
    assert updated.image == "ghcr.io/org/svc:v2"
    assert updated.command == ["run", "--verbose"]


@pytest.mark.asyncio
async def test_refresh_no_changes_returns_empty(
    client_with_component: AsyncClient,
) -> None:
    """When the compose is identical, changed_fields is empty."""
    new_spec = _make_derived_spec()  # same as stored
    repo_files = RepoFiles(
        compose_bytes=ORIGINAL_COMPOSE,
        config_json=None,
        config_json_template=None,
        config_schema_json=None,
    )
    with (
        patch(
            "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
            return_value=repo_files,
        ),
        patch(
            "robotsix_central_deploy.onboard.parser.parse_compose",
            return_value=new_spec,
        ),
    ):
        resp = await client_with_component.post(
            "/services/test-comp/refresh-contract", headers=HEADERS
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["changed_fields"] == []


@pytest.mark.asyncio
async def test_refresh_404_on_unknown_component(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    resp = await client.post(
        "/services/no-such-comp/refresh-contract", headers=auth_headers
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_refresh_400_without_git_url(
    client_with_component: AsyncClient,
) -> None:
    ccs = server_mod.app.state.component_config_store
    comp = ccs.get("test-comp")
    assert comp is not None
    await ccs.put(comp.model_copy(update={"git_url": ""}))

    resp = await client_with_component.post(
        "/services/test-comp/refresh-contract", headers=HEADERS
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_refresh_preserves_operator_fields(
    client_with_component: AsyncClient,
) -> None:
    """repo_id and caretaker_auto_update survive a contract refresh."""
    ccs = server_mod.app.state.component_config_store
    comp = ccs.get("test-comp")
    assert comp is not None
    await ccs.put(
        comp.model_copy(update={"repo_id": "my-repo", "caretaker_auto_update": False})
    )

    new_spec = _make_derived_spec(image="ghcr.io/org/svc:v2")
    repo_files = RepoFiles(
        compose_bytes=UPDATED_COMPOSE,
        config_json=None,
        config_json_template=None,
        config_schema_json=None,
    )
    with (
        patch(
            "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
            return_value=repo_files,
        ),
        patch(
            "robotsix_central_deploy.onboard.parser.parse_compose",
            return_value=new_spec,
        ),
    ):
        resp = await client_with_component.post(
            "/services/test-comp/refresh-contract", headers=HEADERS
        )

    assert resp.status_code == 200
    updated = ccs.get("test-comp")
    assert updated is not None
    assert updated.repo_id == "my-repo"
    assert updated.caretaker_auto_update is False
    assert updated.image == "ghcr.io/org/svc:v2"  # contract field still updated


@pytest.mark.asyncio
async def test_refresh_requires_auth(
    client: AsyncClient,
) -> None:
    resp = await client.post("/services/test-comp/refresh-contract")
    assert resp.status_code == 401
