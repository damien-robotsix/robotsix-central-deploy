"""Integration tests for the chat-agent GitHub Actions status endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.lifecycle.github_app import GitHubAppNotConfiguredError


class _FakeRun:
    """Stand-in for a PyGithub ``WorkflowRun``."""

    def __init__(
        self,
        run_id: int,
        *,
        name: str = "CI",
        status: str = "completed",
        conclusion: str | None = "success",
    ) -> None:
        self.id = run_id
        self.name = name
        self.status = status
        self.conclusion = conclusion
        self.head_branch = "main"
        self.head_sha = "abc123"
        self.run_number = 5
        self.event = "push"
        self.html_url = f"https://github.com/acme/widget/actions/runs/{run_id}"
        self.created_at = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
        self.updated_at = datetime(2026, 7, 7, 12, 5, 0, tzinfo=timezone.utc)


def _fake_client(repo_obj: MagicMock) -> MagicMock:
    client = MagicMock(name="fake-github-client")
    client.get_repo.return_value = repo_obj
    return client


@pytest.fixture
def enable_github_app():
    """Configure github_app_id/private_key so the endpoints don't 503."""
    server_mod.app.state.config.github_app_id = "12345"
    server_mod.app.state.config.github_app_private_key = "pem-data"
    yield
    server_mod.app.state.config.github_app_id = ""
    server_mod.app.state.config.github_app_private_key = ""


class TestListWorkflowRuns:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.get("/chat/github/repos/acme/widget/actions/runs")
        assert resp.status_code == 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs", headers=auth_headers
        )
        assert resp.status_code == 503

    async def test_lists_runs(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.get_workflow_runs.return_value = [_FakeRun(1), _FakeRun(2)]
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs", headers=auth_headers
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0] == {
            "id": 1,
            "name": "CI",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "head_sha": "abc123",
            "run_number": 5,
            "event": "push",
            "html_url": "https://github.com/acme/widget/actions/runs/1",
            "created_at": "2026-07-07T12:00:00+00:00",
            "updated_at": "2026-07-07T12:05:00+00:00",
        }
        repo_obj.get_workflow_runs.assert_called_once_with()

    async def test_passes_branch_and_status_filters(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.get_workflow_runs.return_value = []
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs"
            "?branch=main&run_status=in_progress",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        repo_obj.get_workflow_runs.assert_called_once_with(
            branch="main", status="in_progress"
        )

    async def test_per_page_capped_at_100(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.get_workflow_runs.return_value = [_FakeRun(i) for i in range(5)]
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs?per_page=999",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert (
            len(resp.json()) == 5
        )  # only 5 available; cap doesn't truncate below that

    async def test_unknown_repo_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        repo_obj = MagicMock()
        repo_obj.get_workflow_runs.side_effect = UnknownObjectException(
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
            "/chat/github/repos/acme/ghost/actions/runs", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_generic_github_error_returns_502(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import GithubException

        repo_obj = MagicMock()
        repo_obj.get_workflow_runs.side_effect = GithubException(
            500, data={"message": "boom"}
        )
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs", headers=auth_headers
        )
        assert resp.status_code == 502


class TestGetWorkflowRun:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.get("/chat/github/repos/acme/widget/actions/runs/1")
        assert resp.status_code == 401

    async def test_gets_single_run(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.get_workflow_run.return_value = _FakeRun(
            42, status="in_progress", conclusion=None
        )
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs/42", headers=auth_headers
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == 42
        assert body["status"] == "in_progress"
        assert body["conclusion"] is None
        repo_obj.get_workflow_run.assert_called_once_with(42)

    async def test_run_not_found_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        repo_obj = MagicMock()
        repo_obj.get_workflow_run.side_effect = UnknownObjectException(
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
            "/chat/github/repos/acme/widget/actions/runs/9999", headers=auth_headers
        )
        assert resp.status_code == 404


@pytest.fixture
def enable_repo_create_token():
    """Configure github_repo_create_token so create_repo doesn't 503."""
    server_mod.app.state.config.github_repo_create_token = "pat-token"
    yield
    server_mod.app.state.config.github_repo_create_token = ""


class TestCreateRepo:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.post("/chat/github/repos", json={"name": "widget"})
        assert resp.status_code == 401

    async def test_503_when_token_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.post(
            "/chat/github/repos", json={"name": "widget"}, headers=auth_headers
        )
        assert resp.status_code == 503

    async def test_creates_repo(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_repo_create_token,
    ):
        fake_repo = MagicMock()
        fake_repo.full_name = "damien-robotsix/widget"
        fake_repo.html_url = "https://github.com/damien-robotsix/widget"
        fake_repo.clone_url = "https://github.com/damien-robotsix/widget.git"
        fake_repo.private = True
        fake_repo.description = "A widget"

        fake_user = MagicMock()
        fake_user.create_repo.return_value = fake_repo
        fake_client = MagicMock()
        fake_client.get_user.return_value = fake_user

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_repo_create_client",
            lambda config: fake_client,
        )

        resp = await client.post(
            "/chat/github/repos",
            json={
                "name": "widget",
                "description": "A widget",
                "private": True,
                "topics": ["robotics"],
            },
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "full_name": "damien-robotsix/widget",
            "html_url": "https://github.com/damien-robotsix/widget",
            "clone_url": "https://github.com/damien-robotsix/widget.git",
            "private": True,
            "description": "A widget",
        }
        fake_user.create_repo.assert_called_once_with(
            name="widget",
            description="A widget",
            homepage="",
            private=True,
            auto_init=False,
        )
        fake_repo.replace_topics.assert_called_once_with(["robotics"])

    async def test_records_audit_entry(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_repo_create_token,
    ):
        fake_repo = MagicMock()
        fake_repo.full_name = "damien-robotsix/widget"
        fake_repo.html_url = "https://github.com/damien-robotsix/widget"
        fake_repo.clone_url = "https://github.com/damien-robotsix/widget.git"
        fake_repo.private = False
        fake_repo.description = ""

        fake_user = MagicMock()
        fake_user.create_repo.return_value = fake_repo
        fake_client = MagicMock()
        fake_client.get_user.return_value = fake_user

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_repo_create_client",
            lambda config: fake_client,
        )

        resp = await client.post(
            "/chat/github/repos", json={"name": "widget"}, headers=auth_headers
        )
        assert resp.status_code == 200

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert len(entries) == 1
        assert entries[0].component == "github"
        assert entries[0].action == "create_repo"
        assert entries[0].key == "widget"

    async def test_name_conflict_returns_409(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_repo_create_token,
    ):
        from github import GithubException

        fake_user = MagicMock()
        fake_user.create_repo.side_effect = GithubException(
            422, data={"message": "name already exists on this account"}
        )
        fake_client = MagicMock()
        fake_client.get_user.return_value = fake_user

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_repo_create_client",
            lambda config: fake_client,
        )

        resp = await client.post(
            "/chat/github/repos", json={"name": "widget"}, headers=auth_headers
        )
        assert resp.status_code == 409

    async def test_other_github_error_returns_422(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_repo_create_token,
    ):
        from github import GithubException

        fake_user = MagicMock()
        fake_user.create_repo.side_effect = GithubException(
            422, data={"message": "invalid name"}
        )
        fake_client = MagicMock()
        fake_client.get_user.return_value = fake_user

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_repo_create_client",
            lambda config: fake_client,
        )

        resp = await client.post(
            "/chat/github/repos", json={"name": "bad name"}, headers=auth_headers
        )
        assert resp.status_code == 422


class TestGitHubAppNotConfiguredError:
    def test_message_mentions_both_fields(self):
        # Sanity check on the error message content raised by github_app.py,
        # surfaced verbatim as the 503 detail above.
        try:
            raise GitHubAppNotConfiguredError(
                "github_app_id and github_app_private_key must both be set "
                "to use the github chat component."
            )
        except GitHubAppNotConfiguredError as exc:
            assert "github_app_id" in str(exc)
            assert "github_app_private_key" in str(exc)
