"""Integration tests for the fleet-internal Langfuse credentials endpoint."""

from __future__ import annotations

from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.registry.models import ComponentConfig


class TestFleetLangfuseAuth:
    async def test_unauthorized_no_longer_401(self, client: AsyncClient):
        resp = await client.get("/fleet/langfuse")
        assert resp.status_code != 401


class _VolumeBackend:
    """Backend that serves component config from an in-memory volume.

    Langfuse discovery reads each component's own config file rather than a
    deploy-plane copy, so these tests seed the volume the component reads.
    """

    def __init__(self) -> None:
        self.volumes: dict[str, dict] = {}

    async def read_config_from_volume(self, volume_name: str) -> dict:
        return dict(self.volumes.get(volume_name, {}))


def _seed_component_config(component_id: str, config: dict) -> None:
    """Put *config* on the volume that *component_id* would read."""
    backend = getattr(server_mod.app.state, "backend", None)
    if not isinstance(backend, _VolumeBackend):
        backend = _VolumeBackend()
        server_mod.app.state.backend = backend
    backend.volumes[f"{component_id}-config"] = config


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
                config_volume="test-comp-config",
            )
        )
        registry.register(
            ComponentConfig(
                id="other-comp",
                image="other:latest",
                container_name="other-comp",
                config_volume="other-comp-config",
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
                config_volume="no-langfuse-config",
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
        _seed_component_config(
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
                config_volume="traced-comp-config",
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
        _seed_component_config(
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
                config_volume="partial-comp-config",
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
        _seed_component_config(
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
                config_volume="empty-host-comp-config",
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
                config_volume="running-comp-config",
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
                "openrouter_key": None,
            }
        ]

    async def test_operator_overrides_component_declared_alias(
        self, client: AsyncClient, auth_headers: dict
    ):
        """On alias collision the operator's key wins — same as the chat proxy."""
        from robotsix_central_deploy.lifecycle.routers.fleet_langfuse import (
            OPERATOR_COMPONENT_ID,
        )

        _seed_component_config(
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
                id="owned-comp",
                image="owned:latest",
                container_name="owned-comp",
                config_volume="owned-comp-config",
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


class TestFleetLangfuseOpenRouterKeys:
    """OpenRouter keys, joined to Langfuse projects by the shared alias.

    Reconciliation compares what a provider billed for one LLM function
    against what Langfuse traced for that same function, so the two
    credentials must be joinable — hence the shared alias.
    """

    @staticmethod
    def _set_operator_openrouter(**keys: str) -> None:
        from pydantic import SecretStr

        server_mod.app.state.config.openrouter_keys = {
            alias: SecretStr(v) for alias, v in keys.items()
        }

    async def test_component_declared_openrouter_key_is_returned(
        self, client: AsyncClient, auth_headers: dict
    ):
        """A canonical `openrouter.keys.<alias>` block is joined by alias."""
        self._set_operator_openrouter()
        _seed_component_config(
            "or-comp",
            {
                "langfuse": {
                    "host": "https://langfuse.example.com",
                    "projects": {
                        "proj-a": {"public_key": "pk-a", "secret_key": "sk-a"},
                        "proj-b": {"public_key": "pk-b", "secret_key": "sk-b"},
                    },
                },
                "openrouter": {"keys": {"proj-a": "sk-or-aaa"}},
            },
        )
        registry = server_mod.app.state.registry
        registry.register(
            ComponentConfig(
                id="or-comp",
                image="or:latest",
                container_name="or-comp",
                config_volume="or-comp-config",
            )
        )

        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        comp = next(
            c for c in resp.json()["components"] if c["component_id"] == "or-comp"
        )
        by_alias = {p["alias"]: p for p in comp["projects"]}
        assert by_alias["proj-a"]["openrouter_key"] == "sk-or-aaa"
        # A project with no key declared stays None rather than borrowing one.
        assert by_alias["proj-b"]["openrouter_key"] is None

    async def test_operator_key_bridges_unmigrated_component(
        self, client: AsyncClient, auth_headers: dict
    ):
        """An operator key reaches a component that declares no openrouter block.

        This is the case that makes reconciliation work today: chat and mill
        declare Langfuse projects but no canonical `openrouter` block.
        """
        _seed_component_config(
            "unmigrated",
            {
                "langfuse": {
                    "host": "https://langfuse.example.com",
                    "projects": {
                        "proj-c": {"public_key": "pk-c", "secret_key": "sk-c"}
                    },
                },
            },
        )
        registry = server_mod.app.state.registry
        registry.register(
            ComponentConfig(
                id="unmigrated",
                image="u:latest",
                container_name="unmigrated",
                config_volume="unmigrated-config",
            )
        )
        self._set_operator_openrouter(**{"proj-c": "sk-or-operator"})

        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        comp = next(
            c for c in resp.json()["components"] if c["component_id"] == "unmigrated"
        )
        assert comp["projects"][0]["openrouter_key"] == "sk-or-operator"

    async def test_operator_key_overrides_component_declared(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Operator wins on collision, so a key can be rotated centrally."""
        _seed_component_config(
            "or-override",
            {
                "langfuse": {
                    "host": "https://langfuse.example.com",
                    "projects": {
                        "proj-d": {"public_key": "pk-d", "secret_key": "sk-d"}
                    },
                },
                "openrouter": {"keys": {"proj-d": "sk-or-stale"}},
            },
        )
        registry = server_mod.app.state.registry
        registry.register(
            ComponentConfig(
                id="or-override",
                image="o:latest",
                container_name="or-override",
                config_volume="or-override-config",
            )
        )
        self._set_operator_openrouter(**{"proj-d": "sk-or-rotated"})

        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        comp = next(
            c for c in resp.json()["components"] if c["component_id"] == "or-override"
        )
        assert comp["projects"][0]["openrouter_key"] == "sk-or-rotated"

    async def test_empty_operator_key_is_skipped(
        self, client: AsyncClient, auth_headers: dict
    ):
        """An empty key is unconfigured, not a credential to hand out."""
        _seed_component_config(
            "or-empty",
            {
                "langfuse": {
                    "host": "https://langfuse.example.com",
                    "projects": {
                        "proj-e": {"public_key": "pk-e", "secret_key": "sk-e"}
                    },
                },
            },
        )
        registry = server_mod.app.state.registry
        registry.register(
            ComponentConfig(
                id="or-empty",
                image="e:latest",
                container_name="or-empty",
                config_volume="or-empty-config",
            )
        )
        self._set_operator_openrouter(**{"proj-e": ""})

        resp = await client.get("/fleet/langfuse", headers=auth_headers)
        assert resp.status_code == 200
        comp = next(
            c for c in resp.json()["components"] if c["component_id"] == "or-empty"
        )
        assert comp["projects"][0]["openrouter_key"] is None
