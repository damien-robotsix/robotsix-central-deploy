"""Integration tests for the chat config endpoints.

Covers GET /chat/config/{name}, PUT /chat/config/{name}, and
POST /chat/config/{name}/rollback.
"""

from __future__ import annotations


from httpx import AsyncClient

# Import the server module itself so we can access/wire app.state globals.
import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.models import (
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.config_yaml_store import ConfigYamlStore
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    PortMapping,
)

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
        config_volume=f"{name}-config",
    )
    cfg.chat_agent_mutatable = True
    config_store.register(cfg)
    return cfg


class _VolumeBackend:
    """Backend serving component config from an in-memory volume.

    The chat-agent config endpoints read and write the component's own config
    file; the deploy plane keeps no copy, so tests seed the volume.
    """

    def __init__(self) -> None:
        self.volumes: dict[str, dict] = {}

    async def read_config_from_volume(self, volume_name: str) -> dict:
        return dict(self.volumes.get(volume_name, {}))

    async def write_config_to_volume(self, volume_name: str, data: dict) -> None:
        self.volumes[volume_name] = dict(data)


def _seed_values(name: str, values: dict) -> None:
    """Put *values* on the config volume that *name* reads."""
    backend = getattr(server_mod.app.state, "backend", None)
    if not isinstance(backend, _VolumeBackend):
        backend = _VolumeBackend()
        server_mod.app.state.backend = backend
    backend.volumes[f"{name}-config"] = values


def _read_values(name: str) -> dict | None:
    """Return what is on *name*'s config volume, or None when absent.

    Writes land on the component's own config file, so persistence is
    asserted there rather than against a deploy-plane copy.
    """
    backend = getattr(server_mod.app.state, "backend", None)
    if not isinstance(backend, _VolumeBackend):
        return None
    return backend.volumes.get(f"{name}-config")


async def _seed_service(name: str = "chat") -> None:
    # The config endpoints read and write the component's own file, so every
    # test needs a backend that actually holds one.
    if not isinstance(getattr(server_mod.app.state, "backend", None), _VolumeBackend):
        server_mod.app.state.backend = _VolumeBackend()
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
        _seed_values(
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
        _seed_values("chat", {"api_token": "", "nested": {"secret_key": ""}})

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
        _seed_values("chat", {"api_token": "***"})

        resp = await client.get("/chat/config/chat", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        # When current value is already "***", _mask_secrets returns "" (unset).
        # Verify it does NOT double-escape.
        data = resp.json()
        assert data["restored"]["api_token"] == ""


# ---------------------------------------------------------------------------
# Retired write endpoints
# ---------------------------------------------------------------------------


class TestRetiredConfigWriteEndpoints:
    """PUT /chat/config/{name} and its rollback are retired (410 Gone).

    They rebuilt the whole config document from the deploy plane's stored
    schema template, so they dropped keys the template did not know about and
    wrote back keys the component had removed. On 2026-08-08 that wiped chat's
    Langfuse project credentials.
    """

    async def test_put_returns_410(self, client: AsyncClient, auth_headers: dict):
        await _seed_service("chat")
        resp = await client.put(
            "/chat/config/chat",
            headers=auth_headers,
            json={"values": {"server_port": 9999}},
        )
        assert resp.status_code == 410
        assert "component's own" in resp.json()["error"]

    async def test_rollback_returns_410(self, client: AsyncClient, auth_headers: dict):
        await _seed_service("chat")
        resp = await client.post("/chat/config/chat/rollback", headers=auth_headers)
        assert resp.status_code == 410

    async def test_put_410_even_with_valid_body(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Retirement is unconditional — no payload shape revives it."""
        await _seed_service("chat")
        _seed_values("chat", {"server_port": 8080})
        resp = await client.put(
            "/chat/config/chat", headers=auth_headers, json={"values": {}}
        )
        assert resp.status_code == 410

    async def test_put_unauthenticated_still_401(self, client: AsyncClient):
        resp = await client.put("/chat/config/chat", json={"values": {}})
        assert resp.status_code == 401

    async def test_rollback_unauthenticated_still_401(self, client: AsyncClient):
        resp = await client.post("/chat/config/chat/rollback")
        assert resp.status_code == 401
