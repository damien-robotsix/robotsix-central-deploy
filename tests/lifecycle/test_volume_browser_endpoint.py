"""Integration tests for GET /volumes/{name}/ls and /volumes/{name}/cat."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from httpx import AsyncClient

from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.models import ComponentConfig

import robotsix_central_deploy.lifecycle.server as server_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register_component_with_volume(
    store: ComponentConfigStore, component_id: str, volume_name: str
) -> None:
    """Persist a minimal ComponentConfig that declares one named volume."""
    cfg = ComponentConfig(
        id=component_id,
        image="test:latest",
        container_name=component_id,
        named_volumes=[volume_name],
    )
    await store.put(cfg)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestVolumeBrowserAuth:
    async def test_ls_requires_auth(self, client: AsyncClient):
        resp = await client.get("/volumes/myvol/ls?path=")
        assert resp.status_code == 401

    async def test_cat_requires_auth(self, client: AsyncClient):
        resp = await client.get("/volumes/myvol/cat?path=foo")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Volume allow-list (404)
# ---------------------------------------------------------------------------


class TestVolumeBrowserAllowList:
    async def test_ls_unknown_volume_returns_404(self, client: AsyncClient):
        resp = await client.get(
            "/volumes/nosuchvol/ls", headers={"X-API-Key": "test-key"}
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"].lower()

    async def test_cat_unknown_volume_returns_404(self, client: AsyncClient):
        resp = await client.get(
            "/volumes/nosuchvol/cat",
            params={"path": "x"},
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 404

    async def test_ls_known_volume_does_not_404(self, client: AsyncClient):
        store: ComponentConfigStore = server_mod.app.state.component_config_store
        await _register_component_with_volume(store, "svc1", "my-data")
        resp = await client.get(
            "/volumes/my-data/ls", headers={"X-API-Key": "test-key"}
        )
        # NoopBackend raises 501, not 404
        assert resp.status_code == 501


# ---------------------------------------------------------------------------
# Path validation (400)
# ---------------------------------------------------------------------------


class TestVolumeBrowserPathValidation:
    async def test_ls_dotdot_rejected(self, client: AsyncClient):
        store: ComponentConfigStore = server_mod.app.state.component_config_store
        await _register_component_with_volume(store, "svc2", "vol2")
        resp = await client.get(
            "/volumes/vol2/ls",
            params={"path": "../etc"},
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 400
        assert "traversal" in resp.json()["error"].lower()

    async def test_cat_dotdot_rejected(self, client: AsyncClient):
        store: ComponentConfigStore = server_mod.app.state.component_config_store
        await _register_component_with_volume(store, "svc3", "vol3")
        resp = await client.get(
            "/volumes/vol3/cat",
            params={"path": "../../secret"},
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 400

    async def test_ls_nul_byte_rejected(self, client: AsyncClient):
        store: ComponentConfigStore = server_mod.app.state.component_config_store
        await _register_component_with_volume(store, "svc4", "vol4")
        resp = await client.get(
            "/volumes/vol4/ls",
            params={"path": "foo\x00bar"},
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 400
        assert "nul" in resp.json()["error"].lower()

    async def test_cat_empty_path_ok(self, client: AsyncClient):
        store: ComponentConfigStore = server_mod.app.state.component_config_store
        await _register_component_with_volume(store, "svc5", "vol5")
        resp = await client.get(
            "/volumes/vol5/cat",
            params={"path": ""},
            headers={"X-API-Key": "test-key"},
        )
        # NoopBackend raises 501 for unsupported, not 400
        assert resp.status_code == 501

    async def test_ls_leading_slash_path_ok(self, client: AsyncClient):
        store: ComponentConfigStore = server_mod.app.state.component_config_store
        await _register_component_with_volume(store, "svc6", "vol6")
        resp = await client.get(
            "/volumes/vol6/ls",
            params={"path": "/subdir"},
            headers={"X-API-Key": "test-key"},
        )
        # Should be normalised to "subdir", no 400
        assert resp.status_code == 501


# ---------------------------------------------------------------------------
# Noop / unsupported backend (501)
# ---------------------------------------------------------------------------


class TestVolumeBrowserUnsupportedBackend:
    async def test_ls_noop_returns_501(self, client: AsyncClient):
        store: ComponentConfigStore = server_mod.app.state.component_config_store
        await _register_component_with_volume(store, "svc7", "vol7")
        resp = await client.get("/volumes/vol7/ls", headers={"X-API-Key": "test-key"})
        assert resp.status_code == 501
        assert "not supported" in resp.json()["error"].lower()

    async def test_cat_noop_returns_501(self, client: AsyncClient):
        store: ComponentConfigStore = server_mod.app.state.component_config_store
        await _register_component_with_volume(store, "svc8", "vol8")
        resp = await client.get(
            "/volumes/vol8/cat",
            params={"path": "readme.txt"},
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 501


# ---------------------------------------------------------------------------
# Happy path — mock backend (ls / cat)
# ---------------------------------------------------------------------------


class TestVolumeBrowserHappyPath:
    async def test_ls_returns_entries(self, client: AsyncClient):
        """When backend supports browsing, ls returns parsed entries."""
        store: ComponentConfigStore = server_mod.app.state.component_config_store
        await _register_component_with_volume(store, "svc9", "vol9")

        mock_backend = MagicMock()
        mock_backend.list_volume_dir = AsyncMock(
            return_value=[
                {"name": "config.yaml", "type": "file", "size_bytes": 2048},
                {"name": "data", "type": "dir", "size_bytes": 0},
            ]
        )
        mock_backend.read_volume_file = AsyncMock()
        server_mod.app.state.__setattr__("backend", mock_backend)

        resp = await client.get(
            "/volumes/vol9/ls",
            params={"path": ""},
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["entries"]) == 2
        assert data["entries"][0]["name"] == "config.yaml"
        assert data["entries"][0]["type"] == "file"
        assert data["entries"][0]["size_bytes"] == 2048
        assert data["entries"][1]["name"] == "data"
        assert data["entries"][1]["type"] == "dir"
        assert data["entries"][1]["size_bytes"] == 0
        mock_backend.list_volume_dir.assert_called_once_with("vol9", "")

    async def test_cat_returns_file_content(self, client: AsyncClient):
        """When backend supports browsing, cat returns file content info."""
        store: ComponentConfigStore = server_mod.app.state.component_config_store
        await _register_component_with_volume(store, "svc10", "vol10")

        mock_backend = MagicMock()
        mock_backend.list_volume_dir = AsyncMock()
        mock_backend.read_volume_file = AsyncMock(
            return_value={
                "size_bytes": 100,
                "content": "hello from volume",
                "binary": False,
                "truncated": False,
            }
        )
        server_mod.app.state.__setattr__("backend", mock_backend)

        resp = await client.get(
            "/volumes/vol10/cat",
            params={"path": "readme.txt"},
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["size_bytes"] == 100
        assert data["content"] == "hello from volume"
        assert data["binary"] is False
        assert data["truncated"] is False
        mock_backend.read_volume_file.assert_called_once_with(
            "vol10", "readme.txt", server_mod.VOLUME_CAT_MAX_BYTES
        )
