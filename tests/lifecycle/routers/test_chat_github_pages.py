"""Integration tests for chat-agent GitHub Pages endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

pytest.importorskip("github")

import robotsix_central_deploy.lifecycle.app as server_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_client(requester: MagicMock) -> MagicMock:
    client = MagicMock(name="fake-github-client")
    client.requester = requester
    return client


@pytest.fixture
def enable_github_app():
    """Configure github_app_id/private_key/installation_id so endpoints don't 503."""
    from pydantic import SecretStr

    server_mod.app.state.config.github_app_id = "12345"
    server_mod.app.state.config.github_app_private_key = SecretStr("pem-data")
    server_mod.app.state.config.installation_id = SecretStr("999")
    yield
    server_mod.app.state.config.github_app_id = ""
    server_mod.app.state.config.github_app_private_key = SecretStr("")
    server_mod.app.state.config.installation_id = SecretStr("")


# ---------------------------------------------------------------------------
# PUT /chat/github/repos/{owner}/{repo}/pages
# ---------------------------------------------------------------------------


class TestUpdatePages:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.put("/chat/github/repos/acme/widget/pages")
        assert resp.status_code == 401

    async def test_503_when_neither_credential_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.put(
            "/chat/github/repos/acme/widget/pages",
            json={"enabled": "enabled"},
            headers=auth_headers,
        )
        assert resp.status_code == 503

    async def test_empty_body_returns_422(
        self,
        client: AsyncClient,
        auth_headers: dict,
        enable_github_app,
    ):
        resp = await client.put(
            "/chat/github/repos/acme/widget/pages",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_invalid_enabled_value_returns_422(
        self,
        client: AsyncClient,
        auth_headers: dict,
        enable_github_app,
    ):
        resp = await client.put(
            "/chat/github/repos/acme/widget/pages",
            json={"enabled": "maybe"},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_enables_pages_workflow(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        requester = MagicMock(name="fake-requester")
        # POST creates the pages site; GET returns state (2 calls)
        requester.requestJsonAndCheck.side_effect = [
            ({}, {}),  # POST
            (
                {},
                {"html_url": "https://acme.github.io/widget", "build_type": "workflow"},
            ),  # GET
        ]
        fake_gh_client = _fake_client(requester)

        async def _fake_get_client(config, owner, repo):
            return fake_gh_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/pages",
            json={"enabled": "enabled"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["pages_enabled"] is True
        assert data["full_name"] == "acme/widget"
        assert data["build_type"] == "workflow"

        # Check that POST was called (not PUT)
        calls = requester.requestJsonAndCheck.call_args_list
        assert len(calls) == 2  # POST + GET
        assert calls[0][0][0] == "POST"
        assert "/repos/acme/widget/pages" in calls[0][0][1]
        assert calls[0][1]["input"] == {"build_type": "workflow"}

    async def test_enables_pages_idempotent_when_already_exists(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        """When Pages already exists (409), fall back to PUT."""
        from github import GithubException

        requester = MagicMock(name="fake-requester")
        # First call (POST) raises 409; second (PUT) succeeds; third (GET) returns state
        requester.requestJsonAndCheck.side_effect = [
            GithubException(409, data={"message": "Pages already exists"}),
            ({}, {}),
            (
                {},
                {"html_url": "https://acme.github.io/widget", "build_type": "workflow"},
            ),
        ]
        fake_gh_client = _fake_client(requester)

        async def _fake_get_client(config, owner, repo):
            return fake_gh_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/pages",
            json={"enabled": "enabled"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["pages_enabled"] is True

        # POST called first, then PUT on 409
        calls = requester.requestJsonAndCheck.call_args_list
        assert len(calls) == 3  # POST (409) + PUT + GET
        assert calls[0][0][0] == "POST"
        assert calls[1][0][0] == "PUT"

    async def test_disables_pages(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        requester = MagicMock(name="fake-requester")
        requester.requestJsonAndCheck.return_value = ({}, {})
        fake_gh_client = _fake_client(requester)

        async def _fake_get_client(config, owner, repo):
            return fake_gh_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/pages",
            json={"enabled": "disabled"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["pages_enabled"] is False
        assert data["full_name"] == "acme/widget"

        calls = requester.requestJsonAndCheck.call_args_list
        assert len(calls) == 1
        assert calls[0][0][0] == "DELETE"
        assert "/repos/acme/widget/pages" in calls[0][0][1]

    async def test_disables_pages_idempotent_when_not_enabled(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        """When Pages is already disabled, DELETE returns 404 — treated as success."""
        from github import GithubException

        requester = MagicMock(name="fake-requester")
        requester.requestJsonAndCheck.side_effect = GithubException(
            404, data={"message": "Not Found"}
        )
        fake_gh_client = _fake_client(requester)

        async def _fake_get_client(config, owner, repo):
            return fake_gh_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/pages",
            json={"enabled": "disabled"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["pages_enabled"] is False

    async def test_enables_pages_legacy_with_branch(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        requester = MagicMock(name="fake-requester")
        # POST + GET (2 calls)
        requester.requestJsonAndCheck.side_effect = [
            ({}, {}),  # POST
            (
                {},
                {"html_url": "https://acme.github.io/widget", "build_type": "legacy"},
            ),  # GET
        ]
        fake_gh_client = _fake_client(requester)

        async def _fake_get_client(config, owner, repo):
            return fake_gh_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/pages",
            json={
                "enabled": "enabled",
                "build_type": "legacy",
                "source_branch": "gh-pages",
                "source_path": "/docs",
            },
            headers=auth_headers,
        )

        assert resp.status_code == 200
        calls = requester.requestJsonAndCheck.call_args_list
        assert calls[0][0][0] == "POST"
        assert calls[0][1]["input"] == {
            "source": {"branch": "gh-pages", "path": "/docs"},
        }

    async def test_reconfigures_existing_pages(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        """Omitting 'enabled' but providing 'build_type' should reconfigure."""
        from github import GithubException

        requester = MagicMock(name="fake-requester")
        # POST → 409 (already exists); PUT succeeds; GET returns new state
        requester.requestJsonAndCheck.side_effect = [
            GithubException(409, data={"message": "Pages already exists"}),
            ({}, {}),
            ({}, {"html_url": "https://acme.github.io/widget", "build_type": "legacy"}),
        ]
        fake_gh_client = _fake_client(requester)

        async def _fake_get_client(config, owner, repo):
            return fake_gh_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/pages",
            json={"build_type": "legacy", "source_branch": "gh-pages"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["pages_enabled"] is True

    async def test_records_audit_entry(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        requester = MagicMock(name="fake-requester")
        # POST + GET (2 calls)
        requester.requestJsonAndCheck.side_effect = [
            ({}, {}),  # POST
            (
                {},
                {"html_url": "https://acme.github.io/widget", "build_type": "workflow"},
            ),  # GET
        ]
        fake_gh_client = _fake_client(requester)

        async def _fake_get_client(config, owner, repo):
            return fake_gh_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/pages",
            json={"enabled": "enabled"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert len(entries) == 1
        assert entries[0].component == "github"
        assert entries[0].action == "update_pages"
        assert entries[0].key == "acme/widget"

    async def test_unknown_repo_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        from github import UnknownObjectException

        async def _fake_get_client(config, owner, repo):
            raise UnknownObjectException(404, data={"message": "Not Found"})

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/ghost/pages",
            json={"enabled": "enabled"},
            headers=auth_headers,
        )
        assert resp.status_code == 404
