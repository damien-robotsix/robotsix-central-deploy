"""Integration tests for the chat-agent GitHub branch endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

pytest.importorskip("github")

import robotsix_central_deploy.lifecycle.app as server_mod


def _fake_client(repo_obj: MagicMock) -> MagicMock:
    client = MagicMock(name="fake-github-client")
    client.get_repo.return_value = repo_obj
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


def _fake_branch(
    name: str, *, protected: bool = False, sha: str = "sha123"
) -> MagicMock:
    branch = MagicMock(name=f"branch-{name}")
    branch.name = name
    branch.protected = protected
    branch.commit.sha = sha
    return branch


# ---------------------------------------------------------------------------
# GET /chat/github/repos/{owner}/{repo}/branches
# ---------------------------------------------------------------------------


class TestListBranches:
    async def test_unauthorized_no_longer_401(self, client: AsyncClient):
        resp = await client.get("/chat/github/repos/acme/widget/branches")
        assert resp.status_code != 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get(
            "/chat/github/repos/acme/widget/branches", headers=auth_headers
        )
        assert resp.status_code == 503

    async def test_lists_branches(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.get_branches.return_value = [
            _fake_branch("main", protected=True, sha="mainsha"),
            _fake_branch("feature/foo", protected=False, sha="featsha"),
        ]
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/branches", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body == [
            {"name": "main", "protected": True, "commit_sha": "mainsha"},
            {"name": "feature/foo", "protected": False, "commit_sha": "featsha"},
        ]

    async def test_per_page_capped_at_100(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.get_branches.return_value = [_fake_branch(f"b{i}") for i in range(5)]
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/branches?per_page=999",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 5

    async def test_unknown_repo_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        repo_obj = MagicMock()
        repo_obj.get_branches.side_effect = UnknownObjectException(
            404, data={"message": "Not Found"}
        )
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/branches", headers=auth_headers
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /chat/github/repos/{owner}/{repo}/branches/{branch:path}
# ---------------------------------------------------------------------------


class TestDeleteBranch:
    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.delete(
            "/chat/github/repos/acme/widget/branches/stale", headers=auth_headers
        )
        assert resp.status_code == 503

    async def test_delete_fires(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.default_branch = "main"
        repo_obj.get_branch.return_value = _fake_branch("stale", protected=False)
        git_ref = MagicMock()
        repo_obj.get_git_ref.return_value = git_ref
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.delete(
            "/chat/github/repos/acme/widget/branches/stale", headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json() == {"deleted": True, "branch": "stale"}
        repo_obj.get_git_ref.assert_called_once_with("heads/stale")
        git_ref.delete.assert_called_once_with()

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert len(entries) == 1
        assert entries[0].action == "delete_branch"
        assert entries[0].key == "acme/widget#stale"

    async def test_delete_handles_slash_in_name(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.default_branch = "main"
        repo_obj.get_branch.return_value = _fake_branch("feature/foo", protected=False)
        git_ref = MagicMock()
        repo_obj.get_git_ref.return_value = git_ref
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.delete(
            "/chat/github/repos/acme/widget/branches/feature/foo",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == {"deleted": True, "branch": "feature/foo"}
        repo_obj.get_branch.assert_called_once_with("feature/foo")
        repo_obj.get_git_ref.assert_called_once_with("heads/feature/foo")

    async def test_delete_skips_default(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.default_branch = "main"
        git_ref = MagicMock()
        repo_obj.get_git_ref.return_value = git_ref
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.delete(
            "/chat/github/repos/acme/widget/branches/main", headers=auth_headers
        )
        assert resp.status_code == 409
        repo_obj.get_git_ref.assert_not_called()
        git_ref.delete.assert_not_called()

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert entries == []

    async def test_delete_skips_protected(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.default_branch = "main"
        repo_obj.get_branch.return_value = _fake_branch("release", protected=True)
        git_ref = MagicMock()
        repo_obj.get_git_ref.return_value = git_ref
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.delete(
            "/chat/github/repos/acme/widget/branches/release", headers=auth_headers
        )
        assert resp.status_code == 409
        repo_obj.get_git_ref.assert_not_called()
        git_ref.delete.assert_not_called()

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert entries == []

    async def test_delete_not_found(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        repo_obj = MagicMock()
        repo_obj.default_branch = "main"
        repo_obj.get_branch.side_effect = UnknownObjectException(
            404, data={"message": "Branch not found"}
        )
        git_ref = MagicMock()
        repo_obj.get_git_ref.return_value = git_ref
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.delete(
            "/chat/github/repos/acme/widget/branches/ghost", headers=auth_headers
        )
        assert resp.status_code == 404
        repo_obj.get_git_ref.assert_not_called()

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert entries == []
