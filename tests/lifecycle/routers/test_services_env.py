"""Integration tests for the service environment endpoints."""

from __future__ import annotations

import asyncio

from httpx import AsyncClient


from robotsix_central_deploy.lifecycle.models import (
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
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


class TestEnvEndpoints:
    async def test_get_env_empty_for_fresh_component(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        resp = await client.get("/services/chat/env", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "env": {},
            "secrets": {},
            "env_scopes": {},
            "secret_scopes": {},
            "mem_limit": "2g",
            "allow_chat_access": False,
            "claude_mount": False,
        }

    async def test_put_then_get_returns_env_and_masked_secrets(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        put_body = {"env": {"LOG_LEVEL": "debug"}, "secrets": {"API_KEY": "my-token"}}
        r = await client.put("/services/chat/env", json=put_body, headers=auth_headers)
        assert r.status_code == 204

        r = await client.get("/services/chat/env", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["env"] == {"LOG_LEVEL": "debug"}
        assert data["secrets"] == {"API_KEY": "***"}

    async def test_put_merges_not_replaces(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        await client.put(
            "/services/chat/env", json={"env": {"A": "1"}}, headers=auth_headers
        )
        await client.put(
            "/services/chat/env", json={"env": {"B": "2"}}, headers=auth_headers
        )
        r = await client.get("/services/chat/env", headers=auth_headers)
        data = r.json()
        assert data["env"] == {"A": "1", "B": "2"}

    async def test_get_env_nonexistent_service_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get("/services/nonexistent/env", headers=auth_headers)
        assert resp.status_code == 404

    async def test_unauthenticated_get_returns_401(self, client: AsyncClient):
        await _seed_store("chat")
        resp = await client.get("/services/chat/env")
        assert resp.status_code == 401

    async def test_unauthenticated_put_returns_401(self, client: AsyncClient):
        await _seed_store("chat")
        resp = await client.put("/services/chat/env", json={"env": {"A": "1"}})
        assert resp.status_code == 401

    async def test_unauthenticated_delete_returns_401(self, client: AsyncClient):
        await _seed_store("chat")
        resp = await client.delete("/services/chat/env/A")
        assert resp.status_code == 401

    async def test_delete_key_removes_from_env(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        await client.put(
            "/services/chat/env",
            json={"env": {"A": "1", "B": "2"}},
            headers=auth_headers,
        )
        r = await client.delete("/services/chat/env/A", headers=auth_headers)
        assert r.status_code == 204
        r = await client.get("/services/chat/env", headers=auth_headers)
        data = r.json()
        assert data["env"] == {"B": "2"}

    async def test_delete_key_removes_from_secrets(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        await client.put(
            "/services/chat/env",
            json={"secrets": {"TOKEN": "val"}},
            headers=auth_headers,
        )
        r = await client.delete("/services/chat/env/TOKEN", headers=auth_headers)
        assert r.status_code == 204
        r = await client.get("/services/chat/env", headers=auth_headers)
        data = r.json()
        assert data["secrets"] == {}

    async def test_delete_absent_key_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        await client.put(
            "/services/chat/env", json={"env": {"A": "1"}}, headers=auth_headers
        )
        r = await client.delete("/services/chat/env/NOTFOUND", headers=auth_headers)
        assert r.status_code == 404

    async def test_deploy_injects_merged_env(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """deploy_service must call backend.deploy with merged env including secrets."""
        await _seed_store("chat", image="ghcr.io/o/img:main")

        # Set up a fake registry with a component config that has a base env
        from robotsix_central_deploy.registry.loader import ComponentRegistry

        cfg = ComponentConfig(
            id="chat",
            image="ghcr.io/o/img:main",
            container_name="chat",
            env={"BASE_KEY": "base-val", "OVERRIDE": "base"},
        )
        registry = ComponentRegistry([cfg])
        server_mod.app.state.registry = registry

        # Store a secret and an env override via the API
        await client.put(
            "/services/chat/env",
            json={"env": {"OVERRIDE": "user-val"}, "secrets": {"SECRET": "s3cret"}},
            headers=auth_headers,
        )

        # Monkeypatch backend.deploy to capture the config
        captured_configs: list = []
        original_deploy = server_mod.app.state.backend.deploy

        async def _fake_deploy(service, config, image_ref):
            captured_configs.append(config)
            return await original_deploy(service, config, image_ref)

        monkeypatch.setattr(server_mod.app.state.backend, "deploy", _fake_deploy)

        r = await client.post("/services/chat/deploy", headers=auth_headers)
        assert r.status_code == 202

        # Let the background task run to completion.
        await asyncio.sleep(0)

        assert len(captured_configs) == 1
        deployed_env = captured_configs[0].env
        assert deployed_env == {
            "BASE_KEY": "base-val",
            "OVERRIDE": "user-val",
            "SECRET": "s3cret",
        }

    async def test_put_with_mem_limit_updates_config(
        self, client: AsyncClient, auth_headers: dict
    ):
        """PUT /services/{name}/env with mem_limit persists to ComponentConfig."""
        await _seed_store("chat")
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "chat")

        r = await client.put(
            "/services/chat/env",
            json={"mem_limit": "4g"},
            headers=auth_headers,
        )
        assert r.status_code == 204

        # Verify the mem_limit was persisted
        r = await client.get("/services/chat/env", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["mem_limit"] == "4g"

    async def test_put_without_mem_limit_preserves_existing(
        self, client: AsyncClient, auth_headers: dict
    ):
        """PUT without mem_limit should not change the existing value."""
        await _seed_store("chat")
        config_store = server_mod.app.state.component_config_store
        cfg = await _seed_config(config_store, "chat")
        cfg.mem_limit = "8g"
        await config_store.put(cfg)

        r = await client.put(
            "/services/chat/env",
            json={"env": {"KEY": "val"}},
            headers=auth_headers,
        )
        assert r.status_code == 204

        r = await client.get("/services/chat/env", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["mem_limit"] == "8g"  # unchanged

    async def test_get_env_returns_stored_mem_limit(
        self, client: AsyncClient, auth_headers: dict
    ):
        """GET /services/{name}/env returns mem_limit from ComponentConfig."""
        await _seed_store("chat")
        config_store = server_mod.app.state.component_config_store
        cfg = await _seed_config(config_store, "chat")
        cfg.mem_limit = "512m"
        await config_store.put(cfg)

        r = await client.get("/services/chat/env", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["mem_limit"] == "512m"


class TestEnvScopeEndpoints:
    """Integration tests for scope-tag support on env/secrets."""

    async def test_put_then_get_returns_env_scopes(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("mill")
        put_body = {
            "env": {"LANGFUSE_PUBLIC_KEY": "pk-xxx"},
            "env_scopes": {"LANGFUSE_PUBLIC_KEY": "langfuse:project:abc"},
        }
        r = await client.put("/services/mill/env", json=put_body, headers=auth_headers)
        assert r.status_code == 204

        r = await client.get("/services/mill/env", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["env"] == {"LANGFUSE_PUBLIC_KEY": "pk-xxx"}
        assert data["env_scopes"] == {"LANGFUSE_PUBLIC_KEY": "langfuse:project:abc"}
        assert data["secret_scopes"] == {}

    async def test_put_then_get_returns_secret_scopes(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("mill")
        put_body = {
            "secrets": {"API_KEY": "secret-val"},
            "secret_scopes": {"API_KEY": "api:provider:openrouter"},
        }
        r = await client.put("/services/mill/env", json=put_body, headers=auth_headers)
        assert r.status_code == 204

        r = await client.get("/services/mill/env", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["secrets"] == {"API_KEY": "***"}
        assert data["secret_scopes"] == {"API_KEY": "api:provider:openrouter"}

    async def test_put_scopes_merge_not_replace(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("mill")
        await client.put(
            "/services/mill/env",
            json={
                "env": {"A": "1"},
                "env_scopes": {"A": "scope:a"},
            },
            headers=auth_headers,
        )
        await client.put(
            "/services/mill/env",
            json={
                "env": {"B": "2"},
                "env_scopes": {"B": "scope:b"},
            },
            headers=auth_headers,
        )
        r = await client.get("/services/mill/env", headers=auth_headers)
        data = r.json()
        assert data["env_scopes"] == {"A": "scope:a", "B": "scope:b"}

    async def test_delete_scoped_key_clears_scope(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("mill")
        await client.put(
            "/services/mill/env",
            json={
                "env": {"A": "1"},
                "env_scopes": {"A": "scope:a"},
            },
            headers=auth_headers,
        )
        r = await client.delete("/services/mill/env/A", headers=auth_headers)
        assert r.status_code == 204
        r = await client.get("/services/mill/env", headers=auth_headers)
        data = r.json()
        assert data["env"] == {}
        assert data["env_scopes"] == {}

    async def test_deploy_resolves_consumed_credentials(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """When chat has consumed_scopes, deploy injects scoped creds from mill."""
        await _seed_store("chat", image="ghcr.io/o/chat:main")
        await _seed_store("mill", image="ghcr.io/o/mill:main")

        # Register both components — chat consumes langfuse scopes
        from robotsix_central_deploy.registry.loader import ComponentRegistry

        cfg_chat = ComponentConfig(
            id="chat",
            image="ghcr.io/o/chat:main",
            container_name="chat",
            consumed_scopes=["langfuse:project:*"],
        )
        cfg_mill = ComponentConfig(
            id="mill",
            image="ghcr.io/o/mill:main",
            container_name="mill",
        )
        registry = ComponentRegistry([cfg_chat, cfg_mill])
        server_mod.app.state.registry = registry

        # Seed mill's env store with scoped credentials
        env_store = server_mod.app.state.env_store
        await env_store.upsert(
            "mill",
            {"LANGFUSE_PUBLIC_KEY": "pk-mill"},
            {"LANGFUSE_SECRET_KEY": "sk-mill"},
            env_scopes={"LANGFUSE_PUBLIC_KEY": "langfuse:project:abc"},
            secret_scopes={"LANGFUSE_SECRET_KEY": "langfuse:project:abc"},
        )

        # Also seed a config so that deploy can proceed
        config_store = server_mod.app.state.component_config_store
        await config_store.put(cfg_chat)
        await config_store.put(cfg_mill)

        # Monkeypatch backend.deploy to capture the config
        captured_configs: list = []
        original_deploy = server_mod.app.state.backend.deploy

        async def _fake_deploy(service, config, image_ref):
            captured_configs.append(config)
            return await original_deploy(service, config, image_ref)

        monkeypatch.setattr(server_mod.app.state.backend, "deploy", _fake_deploy)

        r = await client.post("/services/chat/deploy", headers=auth_headers)
        assert r.status_code == 202

        # Let the background task run to completion.
        await asyncio.sleep(0)

        assert len(captured_configs) == 1
        deployed_env = captured_configs[0].env
        assert deployed_env["LANGFUSE_PUBLIC_KEY"] == "pk-mill"
        assert deployed_env["LANGFUSE_SECRET_KEY"] == "sk-mill"

    async def test_deploy_without_consumed_scopes_does_not_inject(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """When chat has NO consumed_scopes, scoped creds are not injected."""
        await _seed_store("chat", image="ghcr.io/o/chat:main")
        await _seed_store("mill", image="ghcr.io/o/mill:main")

        from robotsix_central_deploy.registry.loader import ComponentRegistry

        cfg_chat = ComponentConfig(
            id="chat",
            image="ghcr.io/o/chat:main",
            container_name="chat",
            # NO consumed_scopes
        )
        cfg_mill = ComponentConfig(
            id="mill",
            image="ghcr.io/o/mill:main",
            container_name="mill",
        )
        registry = ComponentRegistry([cfg_chat, cfg_mill])
        server_mod.app.state.registry = registry

        env_store = server_mod.app.state.env_store
        await env_store.upsert(
            "mill",
            {"LANGFUSE_PUBLIC_KEY": "pk-mill"},
            {},
            env_scopes={"LANGFUSE_PUBLIC_KEY": "langfuse:project:abc"},
        )

        config_store = server_mod.app.state.component_config_store
        await config_store.put(cfg_chat)
        await config_store.put(cfg_mill)

        captured_configs: list = []
        original_deploy = server_mod.app.state.backend.deploy

        async def _fake_deploy(service, config, image_ref):
            captured_configs.append(config)
            return await original_deploy(service, config, image_ref)

        monkeypatch.setattr(server_mod.app.state.backend, "deploy", _fake_deploy)

        r = await client.post("/services/chat/deploy", headers=auth_headers)
        assert r.status_code == 202
        await asyncio.sleep(0)

        assert len(captured_configs) == 1
        deployed_env = captured_configs[0].env
        assert "LANGFUSE_PUBLIC_KEY" not in deployed_env

    async def test_deploy_unscoped_credentials_not_shared(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """Untagged secrets are never exposed to other services."""
        await _seed_store("chat", image="ghcr.io/o/chat:main")
        await _seed_store("mill", image="ghcr.io/o/mill:main")

        from robotsix_central_deploy.registry.loader import ComponentRegistry

        cfg_chat = ComponentConfig(
            id="chat",
            image="ghcr.io/o/chat:main",
            container_name="chat",
            consumed_scopes=["*:*:*"],
        )
        cfg_mill = ComponentConfig(
            id="mill",
            image="ghcr.io/o/mill:main",
            container_name="mill",
        )
        registry = ComponentRegistry([cfg_chat, cfg_mill])
        server_mod.app.state.registry = registry

        env_store = server_mod.app.state.env_store
        await env_store.upsert(
            "mill",
            {"PRIVATE_KEY": "do-not-share"},
            {"PRIVATE_SECRET": "also-private"},
            # NO scopes set — these keys are private
        )

        config_store = server_mod.app.state.component_config_store
        await config_store.put(cfg_chat)
        await config_store.put(cfg_mill)

        captured_configs: list = []
        original_deploy = server_mod.app.state.backend.deploy

        async def _fake_deploy(service, config, image_ref):
            captured_configs.append(config)
            return await original_deploy(service, config, image_ref)

        monkeypatch.setattr(server_mod.app.state.backend, "deploy", _fake_deploy)

        r = await client.post("/services/chat/deploy", headers=auth_headers)
        assert r.status_code == 202
        await asyncio.sleep(0)

        assert len(captured_configs) == 1
        deployed_env = captured_configs[0].env
        assert "PRIVATE_KEY" not in deployed_env
        assert "PRIVATE_SECRET" not in deployed_env


# ---------------------------------------------------------------------------
# DELETE /services/{name}
# ---------------------------------------------------------------------------


async def _seed_config(
    config_store: ComponentConfigStore, name: str, *, siblings: list = None
) -> ComponentConfig:
    """Create and persist a ComponentConfig in the config store, plus register it."""
    cfg = ComponentConfig(
        id=name,
        image=f"{name}:latest",
        container_name=name,
        siblings=siblings or [],
    )
    await config_store.put(cfg)
    server_mod.app.state.registry.register(cfg)
    return cfg
