"""Integration tests for the chat components endpoint."""

from __future__ import annotations

import time

from httpx import AsyncClient

from unittest.mock import AsyncMock, MagicMock

from robotsix_central_deploy.lifecycle.models import (
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    PortMapping,
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


class TestChatComponents:
    async def test_empty_when_no_components(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.get("/chat/components")
        assert resp.status_code == 401

    async def test_skips_components_without_allow_chat_access(
        self, client: AsyncClient, auth_headers: dict
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="no-chat",
            image="no-chat:latest",
            container_name="no-chat",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = False
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_component_with_skill(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="chatty",
            image="chatty:latest",
            container_name="chatty",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = True
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        # Mock the httpx.AsyncClient so the skill probe succeeds.
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "# Chatty Skill\nDo the thing."
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        # Clear the cache so we get a fresh probe.
        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "chatty"
        assert data[0]["base_url"] == "http://chatty:8080"
        assert data[0]["skill"] == "# Chatty Skill\nDo the thing."

    async def test_skips_component_with_failed_probe(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="flaky",
            image="flaky:latest",
            container_name="flaky",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = True
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        # Mock httpx.AsyncClient to raise an exception.
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=Exception("boom"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_serves_stale_skill_when_probe_fails(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="stale-ok",
            image="stale-ok:latest",
            container_name="stale-ok",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = True
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        # Probe raises, but an expired cache entry holds a last-known-good
        # skill — the component must stay in the roster with that body.
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=Exception("boom"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()
        expired_at = time.monotonic() - chat_mod._SKILL_CACHE_TTL - 1
        chat_mod._skill_cache["stale-ok"] = (expired_at, "# Stale Skill")

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "stale-ok"
        assert data[0]["skill"] == "# Stale Skill"
        # The stale timestamp is preserved so the next request re-probes.
        assert chat_mod._skill_cache["stale-ok"][0] == expired_at

    async def test_skips_component_with_non_200_probe(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="bad-status",
            image="bad-status:latest",
            container_name="bad-status",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = True
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Error"
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_skips_empty_skill_body(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="empty-skill",
            image="empty-skill:latest",
            container_name="empty-skill",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = True
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "   "  # whitespace-only
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_caches_skill_bodies(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="cached",
            image="cached:latest",
            container_name="cached",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = True
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        call_count = 0

        async def mock_get(url):
            nonlocal call_count
            call_count += 1
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = f"Skill v{call_count}"
            return mock_resp

        mock_client = MagicMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        # First call: probes and caches.
        resp1 = await client.get("/chat/components", headers=auth_headers)
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert len(data1) == 1
        assert data1[0]["skill"] == "Skill v1"

        # Second call: should use cache (no additional probe).
        resp2 = await client.get("/chat/components", headers=auth_headers)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2[0]["skill"] == "Skill v1"
        assert call_count == 1  # still 1 — cache hit

    async def test_skips_component_without_ports(
        self, client: AsyncClient, auth_headers: dict
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="noport",
            image="noport:latest",
            container_name="noport",
            ports=[],
        )
        cfg.allow_chat_access = True
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_multiple_components_mixed(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store

        # Component with chat access enabled that returns a skill.
        cfg1 = ComponentConfig(
            id="alpha",
            image="alpha:latest",
            container_name="alpha",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg1.allow_chat_access = True
        await config_store.put(cfg1)
        server_mod.app.state.registry.register(cfg1)

        # Component without chat access — should be skipped.
        cfg2 = ComponentConfig(
            id="beta",
            image="beta:latest",
            container_name="beta",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg2.allow_chat_access = False
        await config_store.put(cfg2)
        server_mod.app.state.registry.register(cfg2)

        # Component with chat access but probe fails — should be skipped.
        cfg3 = ComponentConfig(
            id="gamma",
            image="gamma:latest",
            container_name="gamma",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg3.allow_chat_access = True
        await config_store.put(cfg3)
        server_mod.app.state.registry.register(cfg3)

        # Mock: alpha returns 200, gamma returns 500.
        async def mock_get(url):
            mock_resp = MagicMock()
            if "alpha" in url:
                mock_resp.status_code = 200
                mock_resp.text = "# Alpha Skill"
            else:
                mock_resp.status_code = 500
                mock_resp.text = "Error"
            return mock_resp

        mock_client = MagicMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "alpha"

    async def test_virtual_component_with_static_skill(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """A virtual (non-Docker) component with chat_base_url + chat_skill
        appears in the roster without probing."""
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="langfuse",
            image="",
            container_name="langfuse",
            ports=[],
        )
        cfg.allow_chat_access = True
        cfg.chat_base_url = "https://langfuse.robotsix.net"
        cfg.chat_skill = "# Langfuse\n\nObservability platform."
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        # Mock httpx so we can assert it is NOT called for the virtual component.
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=MagicMock())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "langfuse"
        assert data[0]["base_url"] == "https://langfuse.robotsix.net"
        assert data[0]["skill"] == "# Langfuse\n\nObservability platform."
        # The static-skill path must NOT call httpx at all.
        mock_client.get.assert_not_called()

    async def test_virtual_component_with_basic_auth(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """A virtual component with auth_type='basic' includes auth metadata
        referencing environment variable names — never the actual values."""
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="langfuse",
            image="",
            container_name="langfuse",
            ports=[],
        )
        cfg.allow_chat_access = True
        cfg.chat_base_url = "https://langfuse.robotsix.net"
        cfg.chat_skill = "# Langfuse\n\nObservability platform."
        cfg.auth_type = "basic"
        cfg.auth_username_env = "LF_PUBLIC"
        cfg.auth_password_env = "LF_SECRET"
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=MagicMock())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        entry = data[0]
        assert entry["id"] == "langfuse"
        assert "auth" in entry
        assert entry["auth"]["type"] == "basic"
        assert entry["auth"]["username_env"] == "LF_PUBLIC"
        assert entry["auth"]["password_env"] == "LF_SECRET"
        # No actual credential values in the response.
        assert "LF_PUBLIC" not in str(entry["auth"]).replace("LF_PUBLIC", "")
        # httpx must NOT be called — static skill path.
        mock_client.get.assert_not_called()

    async def test_virtual_component_with_header_auth(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """A virtual component with auth_type='header' includes auth metadata
        with the header name and token env var."""
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="deploy",
            image="",
            container_name="deploy",
            ports=[],
        )
        cfg.allow_chat_access = True
        cfg.chat_base_url = "http://localhost:8100"
        cfg.chat_skill = "# Deploy\n\nLifecycle server."
        cfg.auth_type = "header"
        cfg.auth_header_name = "X-API-Key"
        cfg.auth_token_env = "DEPLOY_API_KEY"
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=MagicMock())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        entry = data[0]
        assert entry["id"] == "deploy"
        assert "auth" in entry
        assert entry["auth"]["type"] == "header"
        assert entry["auth"]["header_name"] == "X-API-Key"
        assert entry["auth"]["token_env"] == "DEPLOY_API_KEY"
        mock_client.get.assert_not_called()

    async def test_component_without_auth_omits_auth_key(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """A component with empty auth_type does not include an auth key."""
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="noauth",
            image="",
            container_name="noauth",
            ports=[],
        )
        cfg.allow_chat_access = True
        cfg.chat_base_url = "http://noauth.local"
        cfg.chat_skill = "# NoAuth\n\nPublic component."
        # auth_type left as empty string (default)
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=MagicMock())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert "auth" not in data[0]

    async def test_roster_includes_both_langfuse_and_deploy_with_auth(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """The complete roster includes both langfuse (basic auth) and deploy
        (header auth) with their respective auth metadata, matching the
        ticket's verification criterion shape."""
        config_store = server_mod.app.state.component_config_store

        # langfuse — basic auth
        lf = ComponentConfig(
            id="langfuse",
            image="",
            container_name="langfuse",
            ports=[],
        )
        lf.allow_chat_access = True
        lf.chat_base_url = "https://langfuse.robotsix.net"
        lf.chat_skill = "# Langfuse\n\nObservability platform."
        lf.auth_type = "basic"
        lf.auth_username_env = "LANGFUSE_PUBLIC_KEY"
        lf.auth_password_env = "LANGFUSE_SECRET_KEY"
        await config_store.put(lf)
        server_mod.app.state.registry.register(lf)

        # deploy — header auth
        dp = ComponentConfig(
            id="deploy",
            image="",
            container_name="deploy",
            ports=[],
        )
        dp.allow_chat_access = True
        dp.chat_base_url = "http://localhost:8100"
        dp.chat_skill = "# Deploy\n\nLifecycle server."
        dp.auth_type = "header"
        dp.auth_header_name = "X-API-Key"
        dp.auth_token_env = "DEPLOY_API_KEY"
        await config_store.put(dp)
        server_mod.app.state.registry.register(dp)

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=MagicMock())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

        by_id = {e["id"]: e for e in data}

        # langfuse entry
        assert "langfuse" in by_id
        lf_entry = by_id["langfuse"]
        assert lf_entry["base_url"] == "https://langfuse.robotsix.net"
        assert lf_entry["auth"]["type"] == "basic"
        assert lf_entry["auth"]["username_env"] == "LANGFUSE_PUBLIC_KEY"
        assert lf_entry["auth"]["password_env"] == "LANGFUSE_SECRET_KEY"

        # deploy entry
        assert "deploy" in by_id
        dp_entry = by_id["deploy"]
        assert dp_entry["base_url"] == "http://localhost:8100"
        assert dp_entry["auth"]["type"] == "header"
        assert dp_entry["auth"]["header_name"] == "X-API-Key"
        assert dp_entry["auth"]["token_env"] == "DEPLOY_API_KEY"

        mock_client.get.assert_not_called()
