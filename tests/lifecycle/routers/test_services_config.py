"""Integration tests for the service config endpoints."""

from __future__ import annotations


from httpx import AsyncClient


from robotsix_central_deploy.lifecycle.models import (
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.config_yaml_store import ConfigYamlStore
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    ConfigAssistSeed,
)

# Import the server module itself (not just symbols) so we can set its globals.
import robotsix_central_deploy.lifecycle.app as server_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_store(*names: str, image: str = "", deployed_digest: str = "") -> None:
    """Populate the server's store with records for testing."""
    s = server_mod.app.state.store
    assert s is not None
    for name in names:
        rec = ServiceRecord(
            name=name, state=ServiceState.STOPPED, image=image or f"{name}:latest"
        )
        if deployed_digest:
            rec.deployed_image_digest = deployed_digest
        await s.put(rec)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------


class TestGetServiceConfig:
    async def test_returns_schema_and_masked_current(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "password": {"type": "string", "format": "password", "writeOnly": True},
            },
        }
        await store.save_template("chat", schema)
        await store.update_current("chat", {"host": "0.0.0.0", "password": "realpass"})

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "schema" in data
        # Schema is annotated with config-ownership metadata
        assert data["schema"]["type"] == "object"
        assert "host" in data["schema"]["properties"]
        assert data["schema"]["properties"]["host"]["x-deploy-plane"] == "component"
        assert data["schema"]["properties"]["password"]["x-deploy-plane"] == "component"
        assert "current" in data
        assert data["current"]["host"] == "0.0.0.0"
        assert data["current"]["password"] == "***"

    async def test_returns_template_as_current_when_no_current_stored(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
                "port": {"type": "integer", "default": 8080},
            },
        }
        await store.save_template("chat", template)

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        # Schema is annotated with config-ownership metadata
        assert data["schema"]["type"] == "object"
        assert "host" in data["schema"]["properties"]
        assert data["schema"]["properties"]["host"]["x-deploy-plane"] == "component"
        # No current stored — current is masked template (with defaults)
        assert data["current"]["host"] == "localhost"
        assert data["current"]["port"] == 8080

    async def test_no_config_schema_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        # No template saved for "chat"

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 404
        assert "No config schema" in resp.json()["error"]

    async def test_nonexistent_service_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get("/services/nonexistent/config", headers=auth_headers)
        assert resp.status_code == 404
        # Service not found takes priority
        assert "not found" in resp.json()["error"].lower()

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        resp = await client.get("/services/chat/config")
        assert resp.status_code == 401

    async def test_nested_secrets_are_masked(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        schema = {
            "type": "object",
            "properties": {
                "server": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "password": {
                            "type": "string",
                            "format": "password",
                            "writeOnly": True,
                        },
                    },
                },
            },
        }
        await store.save_template("chat", schema)
        await store.update_current(
            "chat", {"server": {"host": "0.0.0.0", "password": "s3cret"}}
        )

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current"]["server"]["host"] == "0.0.0.0"
        assert data["current"]["server"]["password"] == "***"

    async def test_null_template_secret_is_masked(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        schema = {
            "type": "object",
            "properties": {
                "api_key": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                },
            },
        }
        await store.save_template("chat", schema)
        await store.update_current("chat", {"api_key": "real-key"})

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current"]["api_key"] == "***"

    async def test_ref_properties_retain_x_deploy_plane(
        self, client: AsyncClient, auth_headers: dict
    ):
        """$ref properties retain x-deploy-plane on the wire.

        Regression test: _annotate_config_ownership annotates the original
        wrapper dict (not just the temporary merged dict from _resolve_ref),
        so x-deploy-plane survives in the JSON response even when a property
        uses ``$ref``.
        """
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        schema = {
            "type": "object",
            "$defs": {
                "ServerConfig": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "port": {"type": "integer", "default": 8080},
                    },
                },
            },
            "properties": {
                "robotsix_config_file": {"type": "string", "default": "/etc/app.yaml"},
                "server": {"$ref": "#/$defs/ServerConfig"},
            },
        }
        await store.save_template("chat", schema)

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()

        props = data["schema"]["properties"]
        # Direct property (not $ref): deploy-plane key → "deploy"
        assert props["robotsix_config_file"]["x-deploy-plane"] == "deploy"
        # $ref wrapper: annotation survives on the wrapper dict
        assert props["server"]["x-deploy-plane"] == "component"
        # $ref wrapper still carries the $ref pointer
        assert props["server"]["$ref"] == "#/$defs/ServerConfig"

        # Nested properties behind $ref are annotated when resolved
        # (verified via the schema's $defs — the server resolves refs
        # for the walker, but the wire schema keeps the $ref wrapper).
        defs = data["schema"].get("$defs", {})
        assert "ServerConfig" in defs


# ---------------------------------------------------------------------------
# POST /services/{name}/config/assist
# ---------------------------------------------------------------------------


class TestGetServiceConfigAssistFields:
    """GET /services/{name}/config returns config_assist_* fields correctly."""

    async def test_returns_assist_fields_when_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "account": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "password": {
                            "type": "string",
                            "format": "password",
                            "writeOnly": True,
                        },
                    },
                },
            },
        }
        await store.save_template("auto-mail", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            config_assist_command="detect",
            config_assist_seeds=[
                ConfigAssistSeed(key="account.email"),
                ConfigAssistSeed(key="account.password"),
            ],
        )
        await config_store.put(cfg)

        resp = await client.get("/services/auto-mail/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["config_assist_command"] == "detect"
        assert data["config_assist_seeds"] == [
            {"key": "account.email", "label": None},
            {"key": "account.password", "label": None},
        ]

    async def test_returns_seeds_with_labels(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        await store.save_template("auto-mail", {"host": ""})

        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            config_assist_command="detect",
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username", label="Email"),
                ConfigAssistSeed(key="accounts.0.auth.password", label="Password"),
            ],
        )
        await config_store.put(cfg)

        resp = await client.get("/services/auto-mail/config", headers=auth_headers)
        assert resp.status_code == 200
        seeds = resp.json()["config_assist_seeds"]
        assert seeds == [
            {"key": "accounts.0.auth.username", "label": "Email"},
            {"key": "accounts.0.auth.password", "label": "Password"},
        ]

    async def test_returns_null_and_empty_when_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
            },
        }
        await store.save_template("chat", template)

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["config_assist_command"] is None
        assert data["config_assist_seeds"] == []

    async def test_returns_null_and_empty_when_no_component_config(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Even without any ComponentConfig registered, the fields default safely."""
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
            },
        }
        await store.save_template("chat", template)

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["config_assist_command"] is None
        assert data["config_assist_seeds"] == []


class TestRemovedConfigWriteEndpoints:
    async def test_put_config_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.put(
            "/services/test-comp/config", json={"values": {}}, headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_config_import_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.post(
            "/services/test-comp/config/import", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_config_refresh_schema_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.post(
            "/services/test-comp/config/refresh-schema", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_config_assist_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.post(
            "/services/test-comp/config/assist",
            json={"values": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# _namespace_spec_volumes unit tests
# ---------------------------------------------------------------------------
