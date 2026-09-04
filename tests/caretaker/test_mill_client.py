"""Tests for caretaker/mill_client.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from robotsix_http import RetryClient

from robotsix_central_deploy.caretaker.mill_client import MillClient
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.models import ComponentConfig, PortMapping


class TestMillClient:
    """Tests for MillClient HTTP methods."""

    @pytest.mark.asyncio
    async def test_active_stage_summary_reports_heavy_stages(self):
        # Real GET /active shape: list of {ticket_id, stage, started_at}.
        http = MagicMock(spec=RetryClient)
        http.get = AsyncMock(
            return_value=MagicMock(
                json=MagicMock(
                    return_value=[
                        {
                            "ticket_id": "20260901T154756Z-scaffold-44e2",
                            "stage": "implement",
                            "started_at": "2026-09-02T03:14:48.426836+00:00",
                        },
                        {
                            "ticket_id": "20260901T141144Z-inject-c4d3",
                            "stage": "implement",
                            "started_at": "2026-09-02T03:23:54.225249+00:00",
                        },
                        {
                            "ticket_id": "20260902T032527Z-stop-0a7d",
                            "stage": "classify",
                            "started_at": "2026-09-02T03:36:47.358219+00:00",
                        },
                    ]
                )
            )
        )
        client = MillClient("http://mill:8077", http)
        summary = await client.active_stage_summary()
        assert summary == "2 heavy stage(s) in flight: implement×2"
        http.get.assert_called_once_with("http://mill:8077/active")

    @pytest.mark.asyncio
    async def test_active_stage_summary_none_for_cheap_stages_only(self):
        http = MagicMock(spec=RetryClient)
        http.get = AsyncMock(
            return_value=MagicMock(
                json=MagicMock(return_value=[{"ticket_id": "t", "stage": "classify"}])
            )
        )
        client = MillClient("http://mill:8077", http)
        assert await client.active_stage_summary() is None

    @pytest.mark.asyncio
    async def test_active_stage_summary_none_when_idle(self):
        http = MagicMock(spec=RetryClient)
        http.get = AsyncMock(return_value=MagicMock(json=MagicMock(return_value=[])))
        client = MillClient("http://mill:8077", http)
        assert await client.active_stage_summary() is None

    @pytest.mark.asyncio
    async def test_active_stage_summary_fails_open_on_error(self):
        # An unreachable mill must NOT read as busy — deploying is then the
        # likely remedy, and treating errors as busy would pin a broken mill
        # on its broken image forever.
        http = MagicMock(spec=RetryClient)
        http.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        client = MillClient("http://mill:8077", http)
        assert await client.active_stage_summary() is None

    @pytest.mark.asyncio
    async def test_register_repo_201_returns_true(self):
        """Real POST /repos shape: 201 with registered=true on first registration."""
        http = MagicMock(spec=RetryClient)
        http.post = AsyncMock(return_value=MagicMock(is_success=True, status_code=201))
        client = MillClient("http://localhost:8080", http)
        assert (
            await client.register_repo("my-repo", "https://github.com/org/my-repo.git")
            is True
        )
        http.post.assert_called_once_with(
            "http://localhost:8080/repos",
            json={
                "repo_id": "my-repo",
                "forge_remote_url": "https://github.com/org/my-repo.git",
            },
        )

    @pytest.mark.asyncio
    async def test_register_repo_200_registered_false_returns_true(self):
        """Idempotent: an already-registered repo returns 200 registered=false."""
        http = MagicMock(spec=RetryClient)
        http.post = AsyncMock(return_value=MagicMock(is_success=True, status_code=200))
        client = MillClient("http://localhost:8080", http)
        assert (
            await client.register_repo("my-repo", "https://github.com/org/my-repo.git")
            is True
        )

    @pytest.mark.asyncio
    async def test_register_repo_403_flag_off_returns_false(self):
        """allow_runtime_repo_registration disabled → 403, best-effort failure."""
        http = MagicMock(spec=RetryClient)
        http.post = AsyncMock(
            side_effect=ExternalHTTPError(
                "repo registration disabled",
                status_code=403,
                response=MagicMock(text="repo registration disabled"),
            )
        )
        client = MillClient("http://localhost:8080", http)
        assert (
            await client.register_repo("my-repo", "https://github.com/org/my-repo.git")
            is False
        )

    @pytest.mark.asyncio
    async def test_register_repo_error_returns_false(self):
        http = MagicMock(spec=RetryClient)
        http.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        client = MillClient("http://localhost:8080", http)
        assert (
            await client.register_repo("my-repo", "https://github.com/org/my-repo.git")
            is False
        )

    def test_derive_url_finds_mill_component(self):
        registry = ComponentRegistry([])
        config_store = MagicMock(spec=ComponentConfigStore)
        mill_cfg = ComponentConfig(
            id="mill",
            image="mill:latest",
            container_name="mill",
            ports=[PortMapping(host=8080, container=8077)],
        )
        config_store.get = MagicMock(return_value=mill_cfg)
        url = MillClient.derive_url_from_registry(registry, config_store)
        # Container name + container port: managed components publish no
        # host ports, so the caretaker must go over the proxy network.
        assert url == "http://mill:8077"

    def test_derive_url_returns_none_when_absent(self):
        registry = ComponentRegistry([])
        config_store = MagicMock(spec=ComponentConfigStore)
        config_store.get = MagicMock(return_value=None)
        url = MillClient.derive_url_from_registry(registry, config_store)
        assert url is None

    def test_derive_url_uses_custom_component_id(self):
        registry = ComponentRegistry([])
        config_store = MagicMock(spec=ComponentConfigStore)
        mill_cfg = ComponentConfig(
            id="my-mill",
            image="mill:latest",
            container_name="my-mill",
            ports=[PortMapping(host=9090, container=8080)],
        )
        config_store.get = MagicMock(
            side_effect=lambda cid: mill_cfg if cid == "my-mill" else None
        )
        assert MillClient.derive_url_from_registry(registry, config_store) is None
        url = MillClient.derive_url_from_registry(registry, config_store, "my-mill")
        assert url == "http://my-mill:8080"
