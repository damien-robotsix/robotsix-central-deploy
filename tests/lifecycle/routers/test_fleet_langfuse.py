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
        """Component with a canonical langfuse block returns host + projects."""
        config_yaml_store = server_mod.app.state.config_yaml_store
        await config_yaml_store.update_current(
            "traced-comp",
            {
                "langfuse": {
                    "host": "https://langfuse.example.com",
                    "projects": {
                        "my-project": {
                            "public_key": "pk-abc",
                            "secret_key": "sk-xyz",
                            "project_id": "cm-proj-1",
                        }
                    },
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
        assert comp["projects"][0]["project_id"] == "cm-proj-1"

    async def test_filters_out_projects_with_empty_keys(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Projects missing public_key or secret_key are excluded."""
        config_yaml_store = server_mod.app.state.config_yaml_store
        await config_yaml_store.update_current(
            "partial-comp",
            {
                "langfuse": {
                    "host": "https://langfuse.example.com",
                    "projects": {
                        "good": {"public_key": "pk-1", "secret_key": "sk-1"},
                        "no-secret": {"public_key": "pk-2", "secret_key": ""},
                        "no-public": {"public_key": "", "secret_key": "sk-3"},
                        "both-empty": {"public_key": "", "secret_key": ""},
                    },
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
        """An empty string langfuse.host is reported as None."""
        config_yaml_store = server_mod.app.state.config_yaml_store
        await config_yaml_store.update_current(
            "empty-host-comp",
            {
                "langfuse": {
                    "host": "",
                    "projects": {
                        "p": {"public_key": "pk", "secret_key": "sk"},
                    },
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


class TestFleetLangfuseOperatorConfigured:
    """Operator-configured credentials (LifecycleConfig.langfuse_projects).

    The chat trace proxy has always merged these over component-declared
    ones; this endpoint did not, so the same credential resolved differently
    depending on which consumer asked.
    """

    @staticmethod
    def _set_operator_projects(**projects: tuple[str, str]) -> None:
        from robotsix_central_deploy.lifecycle.config import LangfuseProjectCreds

        server_mod.app.state.config.langfuse_projects = {
            alias: LangfuseProjectCreds(public_key=pk, secret_key=sk)
            for alias, (pk, sk) in projects.items()
        }

    async def test_unclaimed_operator_projects_are_surfaced(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Aliases no component declares appear under the synthetic entry.

        This is the case that unblocks cost-monitor: components that have not
        migrated to the canonical block declare nothing, so without this the
        registry returns zero projects fleet-wide.
        """
        from robotsix_central_deploy.lifecycle.routers.fleet_langfuse import (
            OPERATOR_COMPONENT_ID,
        )

        self._set_operator_projects(orphan=("pk-op", "sk-op"))
        server_mod.app.state.config.langfuse_base_url = "https://lf.example.com"

        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        comp = next(
            c
            for c in resp.json()["components"]
            if c["component_id"] == OPERATOR_COMPONENT_ID
        )
        assert comp["langfuse_host"] == "https://lf.example.com"
        assert comp["projects"] == [
            {
                "alias": "orphan",
                "public_key": "pk-op",
                "secret_key": "sk-op",
                "project_id": None,
            }
        ]

    async def test_operator_overrides_component_declared_alias(
        self, client: AsyncClient, auth_headers: dict
    ):
        """On alias collision the operator's key wins — same as the chat proxy."""
        from robotsix_central_deploy.lifecycle.routers.fleet_langfuse import (
            OPERATOR_COMPONENT_ID,
        )

        config_yaml_store = server_mod.app.state.config_yaml_store
        await config_yaml_store.update_current(
            "owned-comp",
            {
                "langfuse": {
                    "host": "https://langfuse.example.com",
                    "projects": {
                        "shared": {"public_key": "pk-old", "secret_key": "sk-old"}
                    },
                },
            },
        )
        registry = server_mod.app.state.registry
        registry.register(
            ComponentConfig(
                id="owned-comp", image="owned:latest", container_name="owned-comp"
            )
        )
        self._set_operator_projects(shared=("pk-new", "sk-new"))

        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        comp = next(c for c in data["components"] if c["component_id"] == "owned-comp")
        assert comp["projects"][0]["public_key"] == "pk-new"
        assert comp["projects"][0]["secret_key"] == "sk-new"
        # Claimed by the component — not duplicated under the synthetic entry.
        assert not any(
            c["component_id"] == OPERATOR_COMPONENT_ID for c in data["components"]
        )

    async def test_half_filled_operator_entry_is_skipped(
        self, client: AsyncClient, auth_headers: dict
    ):
        """A project missing either key is unconfigured, not a broken credential."""
        from robotsix_central_deploy.lifecycle.routers.fleet_langfuse import (
            OPERATOR_COMPONENT_ID,
        )

        self._set_operator_projects(
            no_secret=("pk-1", ""), no_public=("", "sk-2"), good=("pk-3", "sk-3")
        )

        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        comp = next(
            c
            for c in resp.json()["components"]
            if c["component_id"] == OPERATOR_COMPONENT_ID
        )
        assert [p["alias"] for p in comp["projects"]] == ["good"]

    async def test_no_operator_projects_adds_no_synthetic_entry(
        self, client: AsyncClient, auth_headers: dict
    ):
        """With nothing operator-configured the response is unchanged."""
        from robotsix_central_deploy.lifecycle.routers.fleet_langfuse import (
            OPERATOR_COMPONENT_ID,
        )

        self._set_operator_projects()

        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        assert not any(
            c["component_id"] == OPERATOR_COMPONENT_ID
            for c in resp.json()["components"]
        )
