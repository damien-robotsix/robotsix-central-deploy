"""Integration tests for the fleet-internal Langfuse credentials endpoint."""

from __future__ import annotations

from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.registry.models import ComponentConfig


class TestFleetLangfuseAuth:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.get("/fleet/langfuse")
        assert resp.status_code == 401


class TestFleetLangfuseEndpoint:
    async def test_returns_all_registered_components(
        self, client: AsyncClient, auth_headers: dict
    ):
        """GET /fleet/langfuse returns every component in the registry."""
        registry = server_mod.app.state.registry
        registry.register(
            ComponentConfig(
                id="test-comp",
                image="test:latest",
                container_name="test-comp",
            )
        )
        registry.register(
            ComponentConfig(
                id="other-comp",
                image="other:latest",
                container_name="other-comp",
            )
        )

        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        ids = {c["component_id"] for c in data["components"]}
        assert "test-comp" in ids
        assert "other-comp" in ids

    async def test_component_without_langfuse_config_has_null_host(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Component with no Langfuse config returns host=None, projects=[]."""
        registry = server_mod.app.state.registry
        registry.register(
            ComponentConfig(
                id="no-langfuse",
                image="test:latest",
                container_name="no-langfuse",
            )
        )

        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        comp = next(c for c in data["components"] if c["component_id"] == "no-langfuse")
        assert comp["langfuse_host"] is None
        assert comp["projects"] == []

    async def test_component_with_langfuse_config_returns_credentials(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Component with langfuse_projects + langfuse_base_url returns them."""
        config_yaml_store = server_mod.app.state.config_yaml_store
        await config_yaml_store.update_current(
            "traced-comp",
            {
                "langfuse_base_url": "https://langfuse.example.com",
                "langfuse_projects": {
                    "my-project": {
                        "public_key": "pk-abc",
                        "secret_key": "sk-xyz",
                    }
                },
            },
        )
        registry = server_mod.app.state.registry
        registry.register(
            ComponentConfig(
                id="traced-comp",
                image="traced:latest",
                container_name="traced-comp",
            )
        )

        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        comp = next(c for c in data["components"] if c["component_id"] == "traced-comp")
        assert comp["langfuse_host"] == "https://langfuse.example.com"
        assert len(comp["projects"]) == 1
        assert comp["projects"][0]["alias"] == "my-project"
        assert comp["projects"][0]["public_key"] == "pk-abc"
        assert comp["projects"][0]["secret_key"] == "sk-xyz"

    async def test_filters_out_projects_with_empty_keys(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Projects missing public_key or secret_key are excluded."""
        config_yaml_store = server_mod.app.state.config_yaml_store
        await config_yaml_store.update_current(
            "partial-comp",
            {
                "langfuse_base_url": "https://langfuse.example.com",
                "langfuse_projects": {
                    "good": {"public_key": "pk-1", "secret_key": "sk-1"},
                    "no-secret": {"public_key": "pk-2", "secret_key": ""},
                    "no-public": {"public_key": "", "secret_key": "sk-3"},
                    "both-empty": {"public_key": "", "secret_key": ""},
                },
            },
        )
        registry = server_mod.app.state.registry
        registry.register(
            ComponentConfig(
                id="partial-comp",
                image="partial:latest",
                container_name="partial-comp",
            )
        )

        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        comp = next(
            c for c in data["components"] if c["component_id"] == "partial-comp"
        )
        assert len(comp["projects"]) == 1
        assert comp["projects"][0]["alias"] == "good"

    async def test_empty_langfuse_base_url_is_treated_as_none(
        self, client: AsyncClient, auth_headers: dict
    ):
        """An empty string langfuse_base_url is reported as None."""
        config_yaml_store = server_mod.app.state.config_yaml_store
        await config_yaml_store.update_current(
            "empty-host-comp",
            {
                "langfuse_base_url": "",
                "langfuse_projects": {
                    "p": {"public_key": "pk", "secret_key": "sk"},
                },
            },
        )
        registry = server_mod.app.state.registry
        registry.register(
            ComponentConfig(
                id="empty-host-comp",
                image="empty:latest",
                container_name="empty-host-comp",
            )
        )

        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        comp = next(
            c for c in data["components"] if c["component_id"] == "empty-host-comp"
        )
        assert comp["langfuse_host"] is None
        # Projects with valid keys ARE still included even without a host
        assert len(comp["projects"]) == 1

    async def test_status_reflects_service_record(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Component status matches the ServiceRecord state."""
        store = server_mod.app.state.store
        from robotsix_central_deploy.lifecycle.models import ServiceRecord, ServiceState

        await store.put(
            ServiceRecord(
                name="running-comp",
                state=ServiceState.RUNNING,
                container_name="running-comp",
            )
        )
        registry = server_mod.app.state.registry
        registry.register(
            ComponentConfig(
                id="running-comp",
                image="run:latest",
                container_name="running-comp",
            )
        )

        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        comp = next(
            c for c in data["components"] if c["component_id"] == "running-comp"
        )
        assert comp["status"] == "running"

    async def test_registry_empty_returns_empty_list(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Empty registry returns an empty components list."""
        # registry is already empty by default in tests
        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["components"] == []
