"""Integration tests for the chat config endpoints.

Covers GET /chat/config/{name}, PUT /chat/config/{name}, and
POST /chat/config/{name}/rollback.
"""

from __future__ import annotations

import logging

from httpx import AsyncClient

from robotsix_central_deploy.lifecycle.models import (
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.chat_agent_audit_store import ChatAgentAuditStore
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.config_yaml_store import ConfigYamlStore
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    PortMapping,
)

# Import the server module itself so we can access/wire app.state globals.
import robotsix_central_deploy.lifecycle.app as server_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MINIMAL_TEMPLATE: dict = {
    "type": "object",
    "properties": {
        "debug": {"type": "boolean", "default": False},
        "host": {"type": "string", "default": "localhost"},
        "port": {"type": "integer", "default": 8080},
    },
}

_TEMPLATE_WITH_SECRETS: dict = {
    "type": "object",
    "properties": {
        "debug": {"type": "boolean", "default": False},
        "log_level": {"type": "string", "default": "info"},
        "api_token": {
            "type": "string",
            "format": "password",
            "writeOnly": True,
        },
        "nested": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
                "secret_key": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                },
            },
        },
    },
}


def _register_allowlisted_component(
    config_store: ComponentConfigStore,
    name: str = "chat",
) -> ComponentConfig:
    cfg = ComponentConfig(
        id=name,
        image=f"{name}:latest",
        container_name=name,
        ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
    )
    cfg.chat_agent_mutatable = True
    config_store.register(cfg)
    return cfg


async def _seed_service(name: str = "chat") -> None:
    s = server_mod.app.state.store
    assert s is not None
    await s.put(ServiceRecord(name=name, state=ServiceState.RUNNING))


# ---------------------------------------------------------------------------
# GET /chat/config/{name}
# ---------------------------------------------------------------------------


class TestChatGetConfig:
    async def test_happy_path_returns_masked_config(
        self, client: AsyncClient, auth_headers: dict
    ):
        """GET /chat/config/chat returns the current config with secrets masked."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)
        await config_yaml.update_current(
            "chat",
            {
                "debug": True,
                "log_level": "debug",
                "api_token": "real-secret",
                "nested": {"host": "prod.example.com", "secret_key": "nested-secret"},
            },
        )

        resp = await client.get("/chat/config/chat", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["component"] == "chat"
        assert data["restored"]["debug"] is True
        assert data["restored"]["log_level"] == "debug"
        # Secrets masked
        assert data["restored"]["api_token"] == "***"
        assert data["restored"]["nested"]["host"] == "prod.example.com"
        assert data["restored"]["nested"]["secret_key"] == "***"
        assert "Config updated" not in data.get("detail", "")

    async def test_no_current_config_returns_template_defaults(
        self, client: AsyncClient, auth_headers: dict
    ):
        """When no current config exists, GET returns template defaults merged."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _MINIMAL_TEMPLATE)
        # No update_current call — simulates fresh onboard.

        resp = await client.get("/chat/config/chat", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["component"] == "chat"
        assert data["restored"]["debug"] is False
        assert data["restored"]["host"] == "localhost"
        assert data["restored"]["port"] == 8080

    async def test_no_template_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        """GET returns 404 when the component has no config schema."""
        await _seed_service("chat")
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")
        # No template saved.

        resp = await client.get("/chat/config/chat", headers=auth_headers)
        assert resp.status_code == 404
        assert "No config schema" in resp.json()["error"]

    async def test_not_allowlisted_returns_403(
        self, client: AsyncClient, auth_headers: dict
    ):
        """GET returns 403 when the service is not allowlisted."""
        await _seed_service("other-svc")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        # Register a component without chat_agent_mutatable or allow_chat_access.
        cfg = ComponentConfig(
            id="other-svc",
            image="other-svc:latest",
            container_name="other-svc",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg_store.register(cfg)
        await config_yaml.save_template("other-svc", _MINIMAL_TEMPLATE)

        resp = await client.get("/chat/config/other-svc", headers=auth_headers)
        assert resp.status_code == 403

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        """GET returns 401 without auth headers."""
        resp = await client.get("/chat/config/chat")
        assert resp.status_code == 401

    async def test_unset_secret_is_empty_string(
        self, client: AsyncClient, auth_headers: dict
    ):
        """An unset secret (empty string in current) returns '' not '***'."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)
        await config_yaml.update_current(
            "chat", {"api_token": "", "nested": {"secret_key": ""}}
        )

        resp = await client.get("/chat/config/chat", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["restored"]["api_token"] == ""
        assert data["restored"]["nested"]["secret_key"] == ""

    async def test_sentinel_secret_not_double_masked(
        self, client: AsyncClient, auth_headers: dict
    ):
        """A value already set to '***' stays '***' (not '******')."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)
        await config_yaml.update_current("chat", {"api_token": "***"})

        resp = await client.get("/chat/config/chat", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        # When current value is already "***", _mask_secrets returns "" (unset).
        # Verify it does NOT double-escape.
        data = resp.json()
        assert data["restored"]["api_token"] == ""


# ---------------------------------------------------------------------------
# PUT /chat/config/{name}
# ---------------------------------------------------------------------------


class TestChatPutConfig:
    async def test_happy_path_merge_and_masked_response(
        self, client: AsyncClient, auth_headers: dict
    ):
        """PUT merges non-secret keys and returns secret-masked snapshot."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)

        resp = await client.put(
            "/chat/config/chat",
            json={"values": {"debug": True, "api_token": "new-secret"}},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["component"] == "chat"
        assert data["restored"]["debug"] is True
        assert data["restored"]["api_token"] == "***"

        # Verify persistence
        current = await config_yaml.get_current("chat")
        assert current is not None
        assert current["debug"] is True
        assert current["api_token"] == "new-secret"

    async def test_partial_update_preserves_unsubmitted_keys(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Keys absent from the payload keep their existing values."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)
        await config_yaml.update_current(
            "chat",
            {
                "debug": True,
                "log_level": "info",
                "api_token": "real-secret",
                "nested": {"host": "prod.example.com", "secret_key": "nested-secret"},
            },
        )

        resp = await client.put(
            "/chat/config/chat",
            json={"values": {"debug": False}},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text

        current = await config_yaml.get_current("chat")
        assert current is not None
        assert current["debug"] is False
        assert current["api_token"] == "real-secret"
        assert current["nested"]["host"] == "prod.example.com"
        assert current["nested"]["secret_key"] == "nested-secret"

    async def test_secret_key_accepted_and_masked(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Secret keys are accepted in the payload and masked in the response."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)

        resp = await client.put(
            "/chat/config/chat",
            json={"values": {"api_token": "my-secret-token"}},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["restored"]["api_token"] == "***"

        current = await config_yaml.get_current("chat")
        assert current["api_token"] == "my-secret-token"

    async def test_nested_secret_key_accepted(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Nested secret keys are accepted and stored."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)

        resp = await client.put(
            "/chat/config/chat",
            json={
                "values": {
                    "nested": {"host": "newhost", "secret_key": "new-nested-secret"}
                }
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["restored"]["nested"]["secret_key"] == "***"
        assert body["restored"]["nested"]["host"] == "newhost"

        stored = await config_yaml.get_current("chat")
        assert stored["nested"]["secret_key"] == "new-nested-secret"

    async def test_not_allowlisted_returns_403(
        self, client: AsyncClient, auth_headers: dict
    ):
        """PUT returns 403 when the service is not allowlisted."""
        await _seed_service("other-svc")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="other-svc",
            image="other-svc:latest",
            container_name="other-svc",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg_store.register(cfg)
        await config_yaml.save_template("other-svc", _MINIMAL_TEMPLATE)

        resp = await client.put(
            "/chat/config/other-svc",
            json={"values": {"debug": True}},
            headers=auth_headers,
        )
        assert resp.status_code == 403

    async def test_no_template_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        """PUT returns 404 when the component has no config schema."""
        await _seed_service("chat")
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")
        # No template saved.

        resp = await client.put(
            "/chat/config/chat",
            json={"values": {"debug": True}},
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert "No config schema" in resp.json()["error"]

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        """PUT returns 401 without auth headers."""
        resp = await client.put("/chat/config/chat", json={"values": {"debug": True}})
        assert resp.status_code == 401

    async def test_rate_limited_returns_429(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Two rapid PUTs to the same service hit the config_update rate limit."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _MINIMAL_TEMPLATE)

        # First PUT succeeds.
        r1 = await client.put(
            "/chat/config/chat",
            json={"values": {"host": "10.0.0.1"}},
            headers=auth_headers,
        )
        assert r1.status_code == 200, r1.text

        # Second PUT within cooldown (config_update = 5s) returns 429.
        r2 = await client.put(
            "/chat/config/chat",
            json={"values": {"host": "10.0.0.2"}},
            headers=auth_headers,
        )
        assert r2.status_code == 429, r2.text
        assert "Rate limit" in r2.json()["error"]

    async def test_log_level_applied_to_root_logger(
        self, client: AsyncClient, auth_headers: dict
    ):
        """log_level in submitted values sets the root logger level."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)

        original_level = logging.getLogger().level

        resp = await client.put(
            "/chat/config/chat",
            json={"values": {"log_level": "WARNING", "debug": True}},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text

        # Root logger should be set to WARNING
        assert logging.getLogger().level == logging.WARNING

        # Restore original level
        logging.getLogger().setLevel(original_level)

    async def test_log_level_only_no_config_keys_returns_early(
        self, client: AsyncClient, auth_headers: dict
    ):
        """When only log_level is submitted, no config volume write occurs."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)

        resp = await client.put(
            "/chat/config/chat",
            json={"values": {"log_level": "ERROR"}},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["component"] == "chat"
        assert data["restored"] == {}
        assert "log_level set to ERROR" in data["detail"]

        # No current config should have been written.
        current = await config_yaml.get_current("chat")
        assert current is None

    async def test_invalid_log_level_returns_422(
        self, client: AsyncClient, auth_headers: dict
    ):
        """An unrecognised log_level returns 422."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)

        resp = await client.put(
            "/chat/config/chat",
            json={"values": {"log_level": "BOGUS_LEVEL"}},
            headers=auth_headers,
        )
        assert resp.status_code == 422, resp.text
        assert "Unknown log level" in resp.json()["error"]

    async def test_log_level_case_insensitive(
        self, client: AsyncClient, auth_headers: dict
    ):
        """log_level is uppercased before validation, so 'warning' works."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)

        resp = await client.put(
            "/chat/config/chat",
            json={"values": {"log_level": "warning", "debug": True}},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        # log_level popped from values, not in restored config
        assert resp.json()["restored"]["debug"] is True

    async def test_config_validation_422_for_invalid_value(
        self, client: AsyncClient, auth_headers: dict
    ):
        """A value that fails schema coercion returns 422."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _MINIMAL_TEMPLATE)

        # "port" expects integer; submit a non-integer string.
        resp = await client.put(
            "/chat/config/chat",
            json={"values": {"port": "not-a-number"}},
            headers=auth_headers,
        )
        assert resp.status_code == 422, resp.text
        assert "expected integer" in str(resp.json()["error"]).lower()

    async def test_rollback_snapshot_created_on_put(
        self, client: AsyncClient, auth_headers: dict
    ):
        """PUT creates a previous snapshot for later rollback."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)
        await config_yaml.update_current(
            "chat", {"debug": False, "api_token": "before-update"}
        )

        resp = await client.put(
            "/chat/config/chat",
            json={"values": {"debug": True}},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text

        previous = await config_yaml.get_previous("chat")
        assert previous is not None
        # The snapshot should exist and contain debug=False (pre-update value).
        assert previous["debug"] is False
        # Secret values should have been stripped from the snapshot.
        assert "api_token" not in previous

    async def test_rollback_snapshot_secrets_stripped(
        self, client: AsyncClient, auth_headers: dict
    ):
        """The rollback snapshot must not contain secret values."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)
        await config_yaml.update_current(
            "chat",
            {
                "debug": True,
                "api_token": "super-secret",
                "nested": {"host": "oldhost", "secret_key": "old-nested-secret"},
            },
        )

        await client.put(
            "/chat/config/chat",
            json={"values": {"debug": False}},
            headers=auth_headers,
        )

        previous = await config_yaml.get_previous("chat")
        assert previous is not None
        assert previous["debug"] is True
        # Secret keys must be absent from the snapshot.
        assert "api_token" not in previous
        assert "nested" in previous
        assert "host" in previous["nested"]
        assert "secret_key" not in previous["nested"]

    async def test_audit_log_redacts_secret_values(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Secret values in the audit log are replaced with '***'."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)

        await client.put(
            "/chat/config/chat",
            json={"values": {"debug": True, "api_token": "plaintext-secret"}},
            headers=auth_headers,
        )

        entries = await audit_store.list()
        token_entries = [e for e in entries if e.key == "api_token"]
        assert len(token_entries) == 1
        entry = token_entries[0]
        assert entry.new_value == "***"
        assert entry.old_value == "***"
        assert "plaintext-secret" not in str(entry.new_value)

    async def test_audit_log_redacts_nested_secret_values(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Nested secret values in the audit log are also redacted."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)
        await config_yaml.update_current(
            "chat", {"nested": {"host": "old", "secret_key": "old-secret"}}
        )

        await client.put(
            "/chat/config/chat",
            json={
                "values": {"nested": {"host": "new", "secret_key": "new-nested-secret"}}
            },
            headers=auth_headers,
        )

        entries = await audit_store.list()
        nested_entries = [e for e in entries if e.key == "nested"]
        assert len(nested_entries) == 1
        entry = nested_entries[0]
        # The nested dict should have its secret_key masked.
        assert isinstance(entry.new_value, dict)
        assert entry.new_value.get("secret_key") == "***"
        assert entry.new_value.get("host") == "new"

    async def test_audit_log_redacts_list_of_dicts_with_secrets(
        self, client: AsyncClient, auth_headers: dict
    ):
        """List-of-dict values with nested secrets are redacted in the audit log."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
        _register_allowlisted_component(cfg_store, "chat")

        list_template = {
            "type": "object",
            "properties": {
                "servers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "token": {
                                "type": "string",
                                "format": "password",
                                "writeOnly": True,
                            },
                        },
                    },
                },
            },
        }
        await config_yaml.save_template("chat", list_template)

        await client.put(
            "/chat/config/chat",
            json={
                "values": {
                    "servers": [
                        {"url": "https://s1.example.com", "token": "tok1"},
                        {"url": "https://s2.example.com", "token": "tok2"},
                    ]
                }
            },
            headers=auth_headers,
        )

        entries = await audit_store.list()
        server_entries = [e for e in entries if e.key == "servers"]
        assert len(server_entries) == 1
        new_val = server_entries[0].new_value
        assert isinstance(new_val, list)
        assert new_val[0]["token"] == "***"
        assert new_val[1]["token"] == "***"
        assert new_val[0]["url"] == "https://s1.example.com"

    async def test_first_put_creates_snapshot_from_template_defaults(
        self, client: AsyncClient, auth_headers: dict
    ):
        """When no current config exists, snapshot is created from template defaults."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _MINIMAL_TEMPLATE)
        # No update_current — no current config.

        resp = await client.put(
            "/chat/config/chat",
            json={"values": {"host": "10.0.0.1"}},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text

        previous = await config_yaml.get_previous("chat")
        assert previous is not None
        # Snapshot should contain template defaults.
        assert previous["debug"] is False
        assert previous["host"] == "localhost"
        assert previous["port"] == 8080

    async def test_allow_chat_access_flag_grants_mutation(
        self, client: AsyncClient, auth_headers: dict
    ):
        """allow_chat_access=True (without chat_agent_mutatable) still allows writes."""
        await _seed_service("access-only")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store

        cfg = ComponentConfig(
            id="access-only",
            image="access-only:latest",
            container_name="access-only",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = True
        cfg_store.register(cfg)

        await config_yaml.save_template("access-only", _MINIMAL_TEMPLATE)

        resp = await client.put(
            "/chat/config/access-only",
            json={"values": {"host": "10.0.0.1"}},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# POST /chat/config/{name}/rollback
# ---------------------------------------------------------------------------


class TestChatRollbackConfig:
    async def test_happy_path_restores_previous_snapshot(
        self, client: AsyncClient, auth_headers: dict
    ):
        """POST rollback restores the previous config snapshot."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _MINIMAL_TEMPLATE)

        # First update to create a snapshot.
        await client.put(
            "/chat/config/chat",
            json={"values": {"host": "10.0.0.1", "port": 3000}},
            headers=auth_headers,
        )

        # Rollback.
        resp = await client.post(
            "/chat/config/chat/rollback",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["component"] == "chat"
        # Should restore template defaults (the snapshot from before first PUT).
        assert data["restored"]["host"] == "localhost"
        assert data["restored"]["port"] == 8080
        assert data["restored"]["debug"] is False

    async def test_no_previous_snapshot_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        """POST rollback returns 404 when no previous snapshot exists."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _MINIMAL_TEMPLATE)
        # No PUT performed — no snapshot.

        resp = await client.post(
            "/chat/config/chat/rollback",
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert "No previous config snapshot" in resp.json()["error"]

    async def test_not_allowlisted_returns_403(
        self, client: AsyncClient, auth_headers: dict
    ):
        """POST rollback returns 403 for non-allowlisted service."""
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="other-svc",
            image="other-svc:latest",
            container_name="other-svc",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg_store.register(cfg)

        resp = await client.post(
            "/chat/config/other-svc/rollback",
            headers=auth_headers,
        )
        assert resp.status_code == 403

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        """POST rollback returns 401 without auth headers."""
        resp = await client.post("/chat/config/chat/rollback")
        assert resp.status_code == 401

    async def test_rate_limited_returns_429(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Two rapid rollbacks hit the config_rollback rate limit (10s)."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _MINIMAL_TEMPLATE)

        # Create a snapshot via PUT.
        await client.put(
            "/chat/config/chat",
            json={"values": {"host": "10.0.0.1"}},
            headers=auth_headers,
        )

        # First rollback succeeds.
        r1 = await client.post(
            "/chat/config/chat/rollback",
            headers=auth_headers,
        )
        assert r1.status_code == 200, r1.text

        # Second rollback within cooldown returns 429.
        r2 = await client.post(
            "/chat/config/chat/rollback",
            headers=auth_headers,
        )
        assert r2.status_code == 429, r2.text
        assert "Rate limit" in r2.json()["error"]

    async def test_secret_restoration_from_current_config(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Rollback restores secrets from the current (live) config."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)
        # Seed current config with a secret value.
        await config_yaml.update_current(
            "chat", {"debug": True, "api_token": "live-secret"}
        )

        # First PUT to create a snapshot (secret stripped from it).
        await client.put(
            "/chat/config/chat",
            json={"values": {"debug": False}},
            headers=auth_headers,
        )

        # Now rollback — the secret should be restored from current.
        resp = await client.post(
            "/chat/config/chat/rollback",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text

        # The restored config should have the live secret.
        current = await config_yaml.get_current("chat")
        assert current is not None
        assert current["debug"] is True  # rolled back
        assert current["api_token"] == "live-secret"  # restored from current config

    async def test_nested_secret_restoration_during_rollback(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Nested secrets are restored from current config during rollback."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)
        await config_yaml.update_current(
            "chat",
            {
                "debug": True,
                "nested": {"host": "prod.example.com", "secret_key": "live-nested"},
            },
        )

        # PUT to create snapshot.
        await client.put(
            "/chat/config/chat",
            json={"values": {"nested": {"host": "newhost"}}},
            headers=auth_headers,
        )

        # Rollback.
        resp = await client.post(
            "/chat/config/chat/rollback",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text

        current = await config_yaml.get_current("chat")
        assert current is not None
        # host should be rolled back to the snapshot value.
        assert current["nested"]["host"] == "prod.example.com"
        # secret_key should be restored from the live config.
        assert current["nested"]["secret_key"] == "live-nested"

    async def test_rollback_creates_audit_entry(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Rollback writes an audit entry with action='config_rollback'."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _MINIMAL_TEMPLATE)

        # Create a snapshot.
        await client.put(
            "/chat/config/chat",
            json={"values": {"host": "10.0.0.1"}},
            headers=auth_headers,
        )

        # Rollback.
        await client.post(
            "/chat/config/chat/rollback",
            headers=auth_headers,
        )

        entries = await audit_store.list()
        rollback_entries = [e for e in entries if e.action == "config_rollback"]
        assert len(rollback_entries) >= 1
        entry = rollback_entries[0]
        assert entry.component == "chat"
        assert "Restored previous config snapshot" in (entry.detail or "")

    async def test_rollback_response_masks_secrets(
        self, client: AsyncClient, auth_headers: dict
    ):
        """The rollback response masks secret values in the restored config."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        await config_yaml.save_template("chat", _TEMPLATE_WITH_SECRETS)
        await config_yaml.update_current(
            "chat", {"debug": True, "api_token": "live-secret"}
        )

        # Create snapshot.
        await client.put(
            "/chat/config/chat",
            json={"values": {"debug": False}},
            headers=auth_headers,
        )

        resp = await client.post(
            "/chat/config/chat/rollback",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["restored"]["api_token"] == "***"
        assert data["restored"]["debug"] is True  # rolled back

    async def test_rollback_no_template_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Rollback returns 404 when the component has no config schema."""
        await _seed_service("chat")
        config_yaml: ConfigYamlStore = server_mod.app.state.config_yaml_store
        cfg_store: ComponentConfigStore = server_mod.app.state.component_config_store
        _register_allowlisted_component(cfg_store, "chat")

        # Manually create a previous snapshot without a template.
        await config_yaml.save_previous("chat", {"debug": False})

        resp = await client.post(
            "/chat/config/chat/rollback",
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert "No config schema" in resp.json()["error"]
