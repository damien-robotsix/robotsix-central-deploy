"""Tests for POST /services/{name}/refresh-contract."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod
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
    volume_mounts: list[VolumeMount] | None = None,
    command: list[str] | None = None,
) -> DerivedSpec:
    return DerivedSpec(
        name=name,
        git_url="https://github.com/org/test.git",
        image=image,
        ports=ports or [PortMapping(host=8080, container=8080, protocol="tcp")],
        volume_mounts=volume_mounts or [VolumeMount(host="data", container="/data")],
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
async def test_refresh_preserves_operator_set_fields(
    client_with_component: AsyncClient,
) -> None:
    """mem_limit / allow_chat_access / claude_mount survive a refresh.

    Regression (2026-07-31): these three are settable by the operator through
    PUT /services/{name}/env, but refresh rebuilt the config from the manifest's
    labels alone and reset them to the label defaults. claude_mount flipping
    back to false strips a component's claude-auth volume on its next deploy,
    and allow_chat_access drops it from the chat roster.
    """
    ccs = server_mod.app.state.component_config_store
    comp = ccs.get("test-comp")
    assert comp is not None
    comp.mem_limit = "8g"
    comp.allow_chat_access = True
    comp.claude_mount = True
    await ccs.put(comp)

    # The manifest carries none of the corresponding labels, so the parsed
    # spec has all three at their defaults.
    new_spec = _make_derived_spec(image="ghcr.io/org/svc:v2")
    assert new_spec.claude_mount is False
    assert new_spec.allow_chat_access is False

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
    assert updated.mem_limit == "8g"
    assert updated.allow_chat_access is True
    assert updated.claude_mount is True
    # ...while genuinely contract-derived fields still refresh.
    assert updated.image == "ghcr.io/org/svc:v2"


@pytest.mark.asyncio
async def test_refresh_keeps_assigned_host_port(
    client_with_component: AsyncClient,
) -> None:
    """An onboarding-assigned host port is not reset to the manifest's value.

    Regression (2026-07-31): 'mail' ran on host port 10000 because onboarding
    shifted it off the manifest's 8080 to dodge a collision. Refreshing the
    contract reset it to 8080 — which another component already owned.
    """
    ccs = server_mod.app.state.component_config_store
    comp = ccs.get("test-comp")
    assert comp is not None
    comp.ports = [PortMapping(host=10000, container=8080, protocol="tcp")]
    await ccs.put(comp)

    # A second component genuinely holds 8080, exactly as invest did.
    other = ComponentConfig(
        id="other-comp",
        image="ghcr.io/org/other:v1",
        container_name="other-comp",
        ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        git_url="https://github.com/org/other.git",
    )
    await ccs.put(other)

    # The manifest still says 8080:8080.
    new_spec = _make_derived_spec(
        ports=[PortMapping(host=8080, container=8080, protocol="tcp")]
    )
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
    updated = ccs.get("test-comp")
    assert updated is not None
    assert [p.host for p in updated.ports] == [10000]
    assert "ports" not in resp.json()["changed_fields"]
    # The other component keeps 8080 — no collision was created.
    other_after = ccs.get("other-comp")
    assert other_after is not None
    assert [p.host for p in other_after.ports] == [8080]


@pytest.mark.asyncio
async def test_refresh_assigns_free_port_to_new_container_port(
    client_with_component: AsyncClient,
) -> None:
    """A newly exposed container port is shifted when its requested host is taken."""
    ccs = server_mod.app.state.component_config_store
    other = ComponentConfig(
        id="other-comp",
        image="ghcr.io/org/other:v1",
        container_name="other-comp",
        ports=[PortMapping(host=9090, container=9090, protocol="tcp")],
        git_url="https://github.com/org/other.git",
    )
    await ccs.put(other)

    # Manifest now exposes a second port, 9090 — already owned by other-comp.
    new_spec = _make_derived_spec(
        ports=[
            PortMapping(host=8080, container=8080, protocol="tcp"),
            PortMapping(host=9090, container=9090, protocol="tcp"),
        ]
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
    updated = ccs.get("test-comp")
    assert updated is not None
    by_container = {p.container: p.host for p in updated.ports}
    # The pre-existing mapping is untouched...
    assert by_container[8080] == 8080
    # ...and the new one was moved off the port other-comp owns.
    assert by_container[9090] != 9090


@pytest.mark.asyncio
async def test_refresh_no_longer_401(
    client: AsyncClient,
) -> None:
    resp = await client.post("/services/test-comp/refresh-contract")
    assert resp.status_code != 401


@pytest.mark.asyncio
async def test_fetch_component_repo_files_uses_github_app_token() -> None:
    """A configured GitHub App mints an installation token for the clone (private
    repos); without it hexarchy's refresh failed with 'could not read Username'."""
    from robotsix_central_deploy.lifecycle.config import LifecycleConfig
    from robotsix_central_deploy.lifecycle.deps.seed import _fetch_component_repo_files

    comp = ComponentConfig(
        id="hexarchy",
        image="ghcr.io/damien-robotsix/hexarchy:latest",
        container_name="hexarchy",
        git_url="https://github.com/damien-robotsix/hexarchy.git",
    )

    class _Store:
        def get(self, name):
            return comp if name == "hexarchy" else None

    cfg = LifecycleConfig(
        github_app_id="12345",
        github_app_private_key="not-a-real-key-material",
        installation_id="678",
    )
    repo_files = RepoFiles(
        compose_bytes=b"# central-deploy-contract-version: 1\nservices: {}\n",
        config_json=None,
        config_json_template=None,
        config_schema_json=None,
    )
    with (
        patch(
            "robotsix_central_deploy.lifecycle.github_app.get_installation_token_sync",
            return_value="ghs_token",
        ),
        patch(
            "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
            return_value=repo_files,
        ) as mock_fetch,
    ):
        got_cfg, got_files = await _fetch_component_repo_files(
            "hexarchy", _Store(), cfg
        )

    assert got_cfg is comp and got_files is repo_files
    mock_fetch.assert_called_once_with(comp.git_url, 30, "ghs_token")


@pytest.mark.asyncio
async def test_fetch_component_repo_files_without_config_clones_anonymously() -> None:
    from robotsix_central_deploy.lifecycle.deps.seed import _fetch_component_repo_files

    comp = ComponentConfig(
        id="pub",
        image="ghcr.io/x/pub:main",
        container_name="pub",
        git_url="https://github.com/x/pub.git",
    )

    class _Store:
        def get(self, name):
            return comp

    repo_files = RepoFiles(
        compose_bytes=b"x",
        config_json=None,
        config_json_template=None,
        config_schema_json=None,
    )
    with patch(
        "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
        return_value=repo_files,
    ) as mock_fetch:
        await _fetch_component_repo_files("pub", _Store())
    mock_fetch.assert_called_once_with(comp.git_url, 30, None)
