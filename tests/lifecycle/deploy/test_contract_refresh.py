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
    ServiceConfig,
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
    """repo_id and auto_update_enabled survive a contract refresh."""
    ccs = server_mod.app.state.component_config_store
    comp = ccs.get("test-comp")
    assert comp is not None
    await ccs.put(
        comp.model_copy(update={"repo_id": "my-repo", "auto_update_enabled": False})
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
    assert updated.auto_update_enabled is False
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
async def test_refresh_applies_mem_limit_and_memswap_labels(
    client_with_component: AsyncClient,
) -> None:
    """robotsix.deploy.mem-limit / memswap-limit labels reach the stored config.

    The label is the out-of-band OOM guard: a refresh must apply the declared
    limit (like the claude_mount label grant), not silently keep the old value
    — that is what lets a later recreate drop the guard.
    """
    ccs = server_mod.app.state.component_config_store
    comp = ccs.get("test-comp")
    assert comp is not None
    assert comp.mem_limit == "2g"
    assert comp.memswap_limit is None

    new_spec = _make_derived_spec(image="ghcr.io/org/svc:v2")
    new_spec.mem_limit = "4.5g"
    new_spec.memswap_limit = "4.5g"

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
    assert updated.mem_limit == "4.5g"
    assert updated.memswap_limit == "4.5g"


@pytest.mark.asyncio
async def test_refresh_preserves_operator_memswap_when_label_absent(
    client_with_component: AsyncClient,
) -> None:
    """Without a memswap label the stored memswap_limit survives a refresh."""
    ccs = server_mod.app.state.component_config_store
    comp = ccs.get("test-comp")
    assert comp is not None
    comp.memswap_limit = "6g"
    await ccs.put(comp)

    new_spec = _make_derived_spec(image="ghcr.io/org/svc:v2")
    assert new_spec.memswap_limit is None

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
    assert updated.memswap_limit == "6g"
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


# ---------------------------------------------------------------------------
# Regression: refresh must not clobber operator-set sibling secrets or a
# stored image tag (real memory/memory-hindsight data shape).
# ---------------------------------------------------------------------------

# The compose pins the primary by digest (an OLD build) and blanks the two
# sibling secrets with placeholders — the exact shape that crash-looped
# memory-hindsight.
MEMORY_DIGEST = (
    "ghcr.io/damien-robotsix/robotsix-memory"
    "@sha256:5064000000000000000000000000000000000000000000000000000000000000"
)


async def _seed_memory_component(
    *,
    primary_env: dict[str, str] | None = None,
    sibling_env: dict[str, str] | None = None,
) -> None:
    """Seed a memory-like component with one hindsight sibling holding secrets."""
    store = server_mod.app.state.store
    component_config_store = server_mod.app.state.component_config_store
    registry = server_mod.app.state.registry

    comp = ComponentConfig(
        id="memory",
        image="ghcr.io/damien-robotsix/robotsix-memory:main",
        container_name="memory",
        ports=[PortMapping(host=8300, container=8300, protocol="tcp")],
        env=primary_env or {},
        siblings=[
            ServiceConfig(
                service_key="hindsight",
                container_name="memory-hindsight",
                image="ghcr.io/damien-robotsix/robotsix-hindsight:main",
                env=sibling_env
                or {
                    "HINDSIGHT_API_LLM_API_KEY": "sk-real-llm-secret",
                    "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": "sk-real-emb-secret",
                    "HINDSIGHT_API_LOG_LEVEL": "INFO",
                },
            )
        ],
        git_url="https://github.com/damien-robotsix/robotsix-memory.git",
    )
    await component_config_store.put(comp)
    registry.register(comp)
    await store.put(ServiceRecord(name="memory", image=comp.image))


def _memory_refresh_spec(
    *,
    primary_image: str = MEMORY_DIGEST,
    sibling_image: str = "ghcr.io/damien-robotsix/robotsix-hindsight:main",
    primary_env: dict[str, str] | None = None,
    sibling_env: dict[str, str] | None = None,
) -> DerivedSpec:
    """A parsed compose whose sibling env carries placeholders for the secrets."""
    spec = _make_derived_spec(
        name="memory",
        image=primary_image,
        ports=[PortMapping(host=8300, container=8300, protocol="tcp")],
        volume_mounts=[],
        command=None,
    )
    return spec.model_copy(
        update={
            "env": primary_env or {},
            "siblings": [
                ServiceConfig(
                    service_key="hindsight",
                    container_name="memory-hindsight",
                    image=sibling_image,
                    env=sibling_env
                    or {
                        "HINDSIGHT_API_LLM_API_KEY": "",
                        "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": "",
                        "HINDSIGHT_API_LOG_LEVEL": "DEBUG",
                    },
                )
            ],
        }
    )


async def _post_memory_refresh(
    client: AsyncClient, spec: DerivedSpec, *, json: dict | None = None
):
    repo_files = RepoFiles(
        compose_bytes=b"services: {}",
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
            return_value=spec,
        ),
    ):
        return await client.post(
            "/services/memory/refresh-contract", headers=HEADERS, json=json
        )


@pytest.mark.asyncio
async def test_refresh_preserves_sibling_secrets_and_image_tag(
    client: AsyncClient,
) -> None:
    """Stored sibling secrets survive compose placeholders; :main survives a
    digest pin — the exact crash-loop the ticket describes."""
    await _seed_memory_component()
    spec = _memory_refresh_spec()

    resp = await _post_memory_refresh(client, spec)

    assert resp.status_code == 200
    body = resp.json()

    updated = server_mod.app.state.component_config_store.get("memory")
    assert updated is not None
    # Image tag not clobbered by the digest pin.
    assert updated.image == "ghcr.io/damien-robotsix/robotsix-memory:main"
    assert "image" not in body["changed_fields"]

    sib = updated.siblings[0]
    # Operator secrets preserved against the "" placeholders...
    assert sib.env["HINDSIGHT_API_LLM_API_KEY"] == "sk-real-llm-secret"
    assert sib.env["HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY"] == "sk-real-emb-secret"
    # ...while a genuinely-updated non-empty value is adopted.
    assert sib.env["HINDSIGHT_API_LOG_LEVEL"] == "DEBUG"

    # Preserved keys are reported back to the operator.
    preserved_keys = set(body["preserved"]["sibling_env"]["hindsight"])
    assert preserved_keys == {
        "HINDSIGHT_API_LLM_API_KEY",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY",
    }
    assert body["preserved"]["image"] == (
        "ghcr.io/damien-robotsix/robotsix-memory:main"
    )


@pytest.mark.asyncio
async def test_refresh_keeps_sibling_env_key_compose_omits(
    client: AsyncClient,
) -> None:
    """A stored sibling env key the compose does not mention is kept."""
    await _seed_memory_component()
    spec = _memory_refresh_spec(
        sibling_env={"HINDSIGHT_API_LOG_LEVEL": "DEBUG"}  # secrets omitted entirely
    )

    resp = await _post_memory_refresh(client, spec)

    assert resp.status_code == 200
    sib = server_mod.app.state.component_config_store.get("memory").siblings[0]
    assert sib.env["HINDSIGHT_API_LLM_API_KEY"] == "sk-real-llm-secret"
    assert sib.env["HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY"] == "sk-real-emb-secret"


@pytest.mark.asyncio
async def test_refresh_refuses_blanking_primary_secret(
    client: AsyncClient,
) -> None:
    """Blanking a non-empty primary env value is refused with 409."""
    await _seed_memory_component(primary_env={"MEMORY_API_KEY": "sk-primary"})
    spec = _memory_refresh_spec(primary_env={"MEMORY_API_KEY": ""})

    resp = await _post_memory_refresh(client, spec)

    assert resp.status_code == 409
    assert "MEMORY_API_KEY" in resp.json()["error"]
    # The stored value is untouched — refusal happens before persistence.
    stored = server_mod.app.state.component_config_store.get("memory")
    assert stored.env["MEMORY_API_KEY"] == "sk-primary"


@pytest.mark.asyncio
async def test_refresh_allow_env_clear_overrides_409(
    client: AsyncClient,
) -> None:
    """With allow_env_clear the primary secret may be blanked."""
    await _seed_memory_component(primary_env={"MEMORY_API_KEY": "sk-primary"})
    spec = _memory_refresh_spec(primary_env={"MEMORY_API_KEY": ""})

    resp = await _post_memory_refresh(client, spec, json={"allow_env_clear": True})

    assert resp.status_code == 200
    stored = server_mod.app.state.component_config_store.get("memory")
    assert stored.env.get("MEMORY_API_KEY", "") == ""


@pytest.mark.asyncio
async def test_refresh_adopts_sibling_digest_when_stored_is_digest(
    client: AsyncClient,
) -> None:
    """The image-tag policy only protects a stored *tag*; a stored digest is
    replaced by the compose digest as usual."""
    await _seed_memory_component()
    # Stored sibling image is a tag → a compose digest pin of the same repo is
    # rejected in favour of the stored tag.
    spec = _memory_refresh_spec(
        sibling_image=(
            "ghcr.io/damien-robotsix/robotsix-hindsight"
            "@sha256:"
            "1111111111111111111111111111111111111111111111111111111111111111"
        )
    )

    resp = await _post_memory_refresh(client, spec)

    assert resp.status_code == 200
    sib = server_mod.app.state.component_config_store.get("memory").siblings[0]
    assert sib.image == "ghcr.io/damien-robotsix/robotsix-hindsight:main"


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
