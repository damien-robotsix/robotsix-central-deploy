"""Integration tests for service maintenance endpoints (refresh-contract, delete)."""

from __future__ import annotations

import json

from httpx import AsyncClient

from robotsix_central_deploy.lifecycle.models import (
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.onboard.fetcher import RepoFiles
from robotsix_central_deploy.onboard.models import DerivedSpec
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    PortMapping,
    ServiceConfig,
    VolumeMount,
)

import robotsix_central_deploy.lifecycle.app as server_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_store(*names: str, image: str = "") -> None:
    """Populate the server's store with records for testing."""
    s = server_mod.app.state.store
    assert s is not None
    for name in names:
        rec = ServiceRecord(
            name=name, state=ServiceState.STOPPED, image=image or f"{name}:latest"
        )
        await s.put(rec)


async def _seed_config(
    config_store: ComponentConfigStore,
    name: str,
    *,
    siblings: list | None = None,
    named_volumes: list | None = None,
    git_url: str | None = None,
    image: str | None = None,
    ports: list | None = None,
    mounts: list | None = None,
    command: list | None = None,
) -> ComponentConfig:
    """Create and persist a ComponentConfig, plus register it."""
    cfg = ComponentConfig(
        id=name,
        image=image or f"{name}:latest",
        container_name=name,
        siblings=siblings or [],
        named_volumes=named_volumes or [],
        git_url=git_url or "",
        ports=ports or [],
        mounts=mounts or [],
        command=command or [],
    )
    await config_store.put(cfg)
    server_mod.app.state.registry.register(cfg)
    return cfg


def _make_derived_spec(
    *,
    name: str = "svc-a",
    image: str = "svc-a:latest",
    ports: list[PortMapping] | None = None,
    volume_mounts: list[VolumeMount] | None = None,
    command: list[str] | None = None,
) -> DerivedSpec:
    return DerivedSpec(
        name=name,
        git_url="https://github.com/org/test.git",
        image=image,
        ports=ports or [],
        volume_mounts=volume_mounts or [],
        env={},
        claude_mount=False,
        host_docker_sock=False,
        health_check=None,
        command=command or [],
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


# ---------------------------------------------------------------------------
# DELETE /services/{name}
# ---------------------------------------------------------------------------


class TestDeleteService:
    async def test_nonexistent_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.delete("/services/nonexistent", headers=auth_headers)
        assert resp.status_code == 404

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        resp = await client.delete("/services/svc-a")
        assert resp.status_code == 401

    async def test_delete_existing_returns_204(
        self, client: AsyncClient, auth_headers: dict
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")
        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204

    async def test_delete_missing_config_still_succeeds(
        self, client: AsyncClient, auth_headers: dict
    ):
        store = server_mod.app.state.store
        prim = ServiceRecord(name="orphan", image="orphan:latest")
        await store.put(prim)
        resp = await client.delete("/services/orphan", headers=auth_headers)
        assert resp.status_code == 204
        assert await store.get("orphan") is None

    async def test_stop_container_false_backend_not_called(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")

        stop_called = False
        remove_called = False

        async def _fake_stop(service):
            nonlocal stop_called
            stop_called = True

        async def _fake_remove(service):
            nonlocal remove_called
            remove_called = True

        monkeypatch.setattr(server_mod.app.state.backend, "stop", _fake_stop)
        monkeypatch.setattr(
            server_mod.app.state.backend, "remove_container", _fake_remove
        )

        resp = await client.delete(
            "/services/svc-a?stop_container=false", headers=auth_headers
        )
        assert resp.status_code == 204
        assert not stop_called
        assert not remove_called

    async def test_stop_container_true_calls_backend(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")

        stop_called = False
        remove_called = False

        async def _fake_stop(service):
            nonlocal stop_called
            stop_called = True

        async def _fake_remove(service):
            nonlocal remove_called
            remove_called = True

        monkeypatch.setattr(server_mod.app.state.backend, "stop", _fake_stop)
        monkeypatch.setattr(
            server_mod.app.state.backend, "remove_container", _fake_remove
        )

        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204
        assert stop_called
        assert remove_called

    async def test_backend_stop_error_does_not_abort(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")

        async def _failing_stop(service):
            raise RuntimeError("docker daemon down")

        monkeypatch.setattr(server_mod.app.state.backend, "stop", _failing_stop)

        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204

    async def test_remove_volumes_default_false(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a", named_volumes=["svc-a-data"])
        await _seed_store("svc-a", image="svc-a:latest")

        removed: list[str] = []

        async def _fake_remove_volume(volume_name):
            removed.append(volume_name)

        monkeypatch.setattr(
            server_mod.app.state.backend, "remove_volume", _fake_remove_volume
        )

        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204
        assert removed == []

    async def test_remove_volumes_true_removes_volumes(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a", named_volumes=["svc-a-data"])
        await _seed_store("svc-a", image="svc-a:latest")

        removed: list[str] = []

        async def _fake_remove_volume(volume_name):
            removed.append(volume_name)

        monkeypatch.setattr(
            server_mod.app.state.backend, "remove_volume", _fake_remove_volume
        )

        resp = await client.delete(
            "/services/svc-a?remove_volumes=true", headers=auth_headers
        )
        assert resp.status_code == 204
        assert removed == ["svc-a-data"]

    async def test_remove_volume_error_does_not_abort(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store
        await _seed_config(config_store, "svc-a", named_volumes=["svc-a-data"])
        await store.put(ServiceRecord(name="svc-a", image="svc-a:latest"))

        async def _failing_remove_volume(volume_name):
            raise RuntimeError("volume in use")

        monkeypatch.setattr(
            server_mod.app.state.backend, "remove_volume", _failing_remove_volume
        )

        resp = await client.delete(
            "/services/svc-a?remove_volumes=true", headers=auth_headers
        )
        assert resp.status_code == 204
        assert await store.get("svc-a") is None

    async def test_delete_with_sibling(self, client: AsyncClient, auth_headers: dict):
        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store

        sibling = ServiceConfig(
            service_key="redis",
            container_name="svc-a-redis",
            image="redis:7",
        )
        cfg = ComponentConfig(
            id="svc-a",
            image="svc-a:latest",
            container_name="svc-a",
            siblings=[sibling],
        )
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        prim = ServiceRecord(name="svc-a", image="svc-a:latest")
        sib_rec = ServiceRecord(name="svc-a-redis", image="redis:7")
        await store.put(prim)
        await store.put(sib_rec)

        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204

        assert await store.get("svc-a") is None
        assert await store.get("svc-a-redis") is None
        assert config_store.get("svc-a") is None


# ---------------------------------------------------------------------------
# POST /services/{name}/refresh-contract
# ---------------------------------------------------------------------------


class TestRefreshContract:
    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        resp = await client.post("/services/svc-a/refresh-contract")
        assert resp.status_code == 401

    async def test_component_not_found_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.post(
            "/services/nonexistent/refresh-contract", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_missing_git_url_returns_400(
        self, client: AsyncClient, auth_headers: dict
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a", git_url="")

        resp = await client.post(
            "/services/svc-a/refresh-contract", headers=auth_headers
        )
        assert resp.status_code == 400

    async def test_no_compose_file_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(
            config_store, "svc-a", git_url="https://github.com/org/test.git"
        )

        async def _fake_fetch(name, ccs):
            return (
                config_store.get("svc-a"),
                RepoFiles(
                    compose_bytes=None, config_json=None, config_schema_json=None
                ),
            )

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.services_maintenance._fetch_component_repo_files",
            _fake_fetch,
        )

        resp = await client.post(
            "/services/svc-a/refresh-contract", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_parse_failure_returns_422(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(
            config_store, "svc-a", git_url="https://github.com/org/test.git"
        )

        async def _fake_fetch(name, ccs):
            return (
                config_store.get("svc-a"),
                RepoFiles(
                    compose_bytes=b"bad yaml: [",
                    config_json=None,
                    config_schema_json=None,
                ),
            )

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.services_maintenance._fetch_component_repo_files",
            _fake_fetch,
        )

        resp = await client.post(
            "/services/svc-a/refresh-contract", headers=auth_headers
        )
        assert resp.status_code == 422

    async def test_happy_path_no_changes(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(
            config_store,
            "svc-a",
            git_url="https://github.com/org/test.git",
            image="svc-a:latest",
            command=["run"],
        )

        spec = _make_derived_spec(name="svc-a", image="svc-a:latest", command=["run"])

        async def _fake_fetch(name, ccs):
            return (
                config_store.get("svc-a"),
                RepoFiles(
                    compose_bytes=b"services:\n  svc:\n    image: svc-a:latest",
                    config_json=None,
                    config_schema_json=None,
                ),
            )

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.services_maintenance._fetch_component_repo_files",
            _fake_fetch,
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.onboard.parser.parse_compose",
            lambda compose_bytes, name, git_url: spec,
        )

        resp = await client.post(
            "/services/svc-a/refresh-contract", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "svc-a"
        assert data["changed_fields"] == []

    async def test_happy_path_with_changes(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(
            config_store,
            "svc-a",
            git_url="https://github.com/org/test.git",
            image="svc-a:v1",
            command=["run"],
        )

        spec = _make_derived_spec(
            name="svc-a", image="svc-a:v2", command=["run", "--verbose"]
        )

        async def _fake_fetch(name, ccs):
            return (
                config_store.get("svc-a"),
                RepoFiles(
                    compose_bytes=b"services:\n  svc:\n    image: svc-a:v2",
                    config_json=None,
                    config_schema_json=None,
                ),
            )

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.services_maintenance._fetch_component_repo_files",
            _fake_fetch,
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.onboard.parser.parse_compose",
            lambda compose_bytes, name, git_url: spec,
        )

        resp = await client.post(
            "/services/svc-a/refresh-contract", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "svc-a"
        assert "image" in data["changed_fields"]
        assert "command" in data["changed_fields"]

    async def test_stored_schema_refreshed_from_repo(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """A key the component added since onboarding reaches the template.

        parse_compose only reads the compose file, so the schema has to be
        attached from the separately-fetched config/config.schema.json.  When
        it wasn't, the stored template stayed pinned at its onboarding version
        and the config editor silently dropped every key added since.
        """
        config_store = server_mod.app.state.component_config_store
        yaml_store = server_mod.app.state.config_yaml_store
        await _seed_config(
            config_store, "svc-a", git_url="https://github.com/org/test.git"
        )
        await yaml_store.save_template(
            "svc-a", {"properties": {"old_key": {"type": "string"}}}
        )

        new_schema = {
            "properties": {
                "old_key": {"type": "string"},
                "new_key": {"type": "string"},
            }
        }
        spec = _make_derived_spec(name="svc-a")

        async def _fake_fetch(name, ccs):
            return (
                config_store.get("svc-a"),
                RepoFiles(
                    compose_bytes=b"services:\n  svc:\n    image: svc-a:latest",
                    config_json=None,
                    config_schema_json=json.dumps(new_schema).encode(),
                ),
            )

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.services_maintenance._fetch_component_repo_files",
            _fake_fetch,
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.onboard.parser.parse_compose",
            lambda compose_bytes, name, git_url: spec,
        )

        resp = await client.post(
            "/services/svc-a/refresh-contract", headers=auth_headers
        )
        assert resp.status_code == 200
        assert "config_schema" in resp.json()["changed_fields"]
        assert await yaml_store.get_template("svc-a") == new_schema

    async def test_unchanged_schema_not_reported_as_changed(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        yaml_store = server_mod.app.state.config_yaml_store
        await _seed_config(
            config_store, "svc-a", git_url="https://github.com/org/test.git"
        )
        schema = {"properties": {"only_key": {"type": "string"}}}
        await yaml_store.save_template("svc-a", schema)

        spec = _make_derived_spec(name="svc-a")

        async def _fake_fetch(name, ccs):
            return (
                config_store.get("svc-a"),
                RepoFiles(
                    compose_bytes=b"services:\n  svc:\n    image: svc-a:latest",
                    config_json=None,
                    config_schema_json=json.dumps(schema).encode(),
                ),
            )

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.services_maintenance._fetch_component_repo_files",
            _fake_fetch,
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.onboard.parser.parse_compose",
            lambda compose_bytes, name, git_url: spec,
        )

        resp = await client.post(
            "/services/svc-a/refresh-contract", headers=auth_headers
        )
        assert resp.status_code == 200
        assert "config_schema" not in resp.json()["changed_fields"]

    async def test_malformed_schema_returns_422(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """A corrupt schema must not silently leave the old template in place."""
        config_store = server_mod.app.state.component_config_store
        await _seed_config(
            config_store, "svc-a", git_url="https://github.com/org/test.git"
        )
        spec = _make_derived_spec(name="svc-a")

        async def _fake_fetch(name, ccs):
            return (
                config_store.get("svc-a"),
                RepoFiles(
                    compose_bytes=b"services:\n  svc:\n    image: svc-a:latest",
                    config_json=None,
                    config_schema_json=b"{not json",
                ),
            )

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.services_maintenance._fetch_component_repo_files",
            _fake_fetch,
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.onboard.parser.parse_compose",
            lambda compose_bytes, name, git_url: spec,
        )

        resp = await client.post(
            "/services/svc-a/refresh-contract", headers=auth_headers
        )
        assert resp.status_code == 422
