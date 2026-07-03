"""Tests for caretaker/mill_client.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from robotsix_central_deploy.caretaker.mill_client import MillClient
from robotsix_central_deploy.caretaker.models import CaretakerFinding, FindingKind
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.models import ComponentConfig, PortMapping


class TestMillClient:
    """Tests for MillClient HTTP methods."""

    @pytest.mark.asyncio
    async def test_ingest_2xx_returns_true(self):
        http = MagicMock(spec=httpx.AsyncClient)
        http.post = AsyncMock(return_value=MagicMock(is_success=True))
        client = MillClient("http://localhost:8080", http)
        finding = CaretakerFinding(
            component_id="svc",
            repo_id="my-repo",
            kind=FindingKind.HEALTH,
            title="test",
            detail="detail",
        )
        assert await client.ingest_finding(finding) is True
        http.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingest_4xx_returns_false(self):
        http = MagicMock(spec=httpx.AsyncClient)
        http.post = AsyncMock(return_value=MagicMock(is_success=False, status_code=422))
        client = MillClient("http://localhost:8080", http)
        finding = CaretakerFinding(
            component_id="svc",
            repo_id="my-repo",
            kind=FindingKind.HEALTH,
            title="test",
            detail="detail",
        )
        assert await client.ingest_finding(finding) is False

    @pytest.mark.asyncio
    async def test_ingest_network_error_returns_false(self):
        http = MagicMock(spec=httpx.AsyncClient)
        http.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        client = MillClient("http://localhost:8080", http)
        finding = CaretakerFinding(
            component_id="svc",
            repo_id="my-repo",
            kind=FindingKind.HEALTH,
            title="test",
            detail="detail",
        )
        assert await client.ingest_finding(finding) is False

    @pytest.mark.asyncio
    async def test_register_repo_201_returns_true(self):
        http = MagicMock(spec=httpx.AsyncClient)
        http.post = AsyncMock(return_value=MagicMock(is_success=True))
        client = MillClient("http://localhost:8080", http)
        assert (
            await client.register_repo("my-repo", "https://github.com/org/my-repo.git")
            is True
        )

    @pytest.mark.asyncio
    async def test_register_repo_error_returns_false(self):
        http = MagicMock(spec=httpx.AsyncClient)
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
