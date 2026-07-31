"""Integration tests for the chat-agent GitHub pull-request, review, and merge-gate endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

pytest.importorskip("github")

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.github_app import (
    GitHubAppNotConfiguredError,
    GitHubRepoCreateNotConfiguredError,
)


def _fake_client(repo_obj: MagicMock) -> MagicMock:
    client = MagicMock(name="fake-github-client")
    client.get_repo.return_value = repo_obj
    return client


@pytest.fixture
def enable_github_app():
    """Configure github_app_id/private_key/installation_id so the endpoints don't 503."""
    from pydantic import SecretStr

    server_mod.app.state.config.github_app_id = "12345"
    server_mod.app.state.config.github_app_private_key = SecretStr("pem-data")
    server_mod.app.state.config.installation_id = SecretStr("999")
    yield
    server_mod.app.state.config.github_app_id = ""
    server_mod.app.state.config.github_app_private_key = SecretStr("")
    server_mod.app.state.config.installation_id = SecretStr("")


@pytest.fixture
def enable_repo_create_token():
    """Configure github_repo_create_token so create_repo doesn't 503."""
    server_mod.app.state.config.github_repo_create_token = "pat-token"
    yield
    server_mod.app.state.config.github_repo_create_token = ""


class _FakeRepo:
    """Stand-in for a PyGithub ``Repository``."""

    def __init__(
        self,
        *,
        full_name: str = "acme/widget",
        private: bool = False,
        description: str = "A widget",
        homepage: str = "",
        has_issues: bool = True,
        has_wiki: bool = True,
        default_branch: str = "main",
        archived: bool = False,
    ) -> None:
        self.full_name = full_name
        self.html_url = f"https://github.com/{full_name}"
        self.clone_url = f"https://github.com/{full_name}.git"
        self.private = private
        self.description = description
        self.homepage = homepage
        self.has_issues = has_issues
        self.has_wiki = has_wiki
        self.default_branch = default_branch
        self.archived = archived
        self.edit = MagicMock()


# ---------------------------------------------------------------------------
# Fake classes for pull requests
# ---------------------------------------------------------------------------


class _FakeUser:
    def __init__(self, login: str) -> None:
        self.login = login


class _FakeRef:
    def __init__(self, ref: str, sha: str = "abc123") -> None:
        self.ref = ref
        self.sha = sha


class _FakePull:
    """Stand-in for a PyGithub ``PullRequest``."""

    def __init__(
        self,
        number: int,
        *,
        title: str = "Fix the thing",
        state: str = "open",
        mergeable_state: str = "clean",
        draft: bool = False,
        user_login: str | None = "octocat",
        head_ref: str = "feature-branch",
        head_sha: str = "abc123",
        base_ref: str = "main",
        mergeable: bool | None = True,
        merged: bool = False,
        merged_at: datetime | None = None,
        body: str | None = "Fixes the thing.",
    ) -> None:
        self.number = number
        self.title = title
        self.state = state
        self.mergeable_state = mergeable_state
        self.draft = draft
        self.user = _FakeUser(user_login) if user_login else None
        self.html_url = f"https://github.com/acme/widget/pull/{number}"
        self.head = _FakeRef(head_ref, sha=head_sha)
        self.base = _FakeRef(base_ref)
        self.mergeable = mergeable
        self.merged = merged
        self.merged_at = merged_at
        self.created_at = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
        self.updated_at = datetime(2026, 7, 7, 12, 5, 0, tzinfo=timezone.utc)
        self.body = body


class TestListPulls:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.get("/chat/github/repos/acme/widget/pulls")
        assert resp.status_code == 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get(
            "/chat/github/repos/acme/widget/pulls", headers=auth_headers
        )
        assert resp.status_code == 503

    async def test_lists_pulls(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.get_pulls.return_value = [_FakePull(1), _FakePull(2)]
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/pulls", headers=auth_headers
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0] == {
            "number": 1,
            "title": "Fix the thing",
            "state": "open",
            "mergeable_state": "clean",
            "draft": False,
            "user": "octocat",
            "html_url": "https://github.com/acme/widget/pull/1",
            "head_ref": "feature-branch",
            "head_sha": "abc123",
            "base_ref": "main",
            "mergeable": True,
            "merged": False,
            "merged_at": None,
            "created_at": "2026-07-07T12:00:00+00:00",
            "updated_at": "2026-07-07T12:05:00+00:00",
            "body": "Fixes the thing.",
        }
        repo_obj.get_pulls.assert_called_once_with(state="open")

    async def test_passes_state_filter(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.get_pulls.return_value = []
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/pulls?state=all", headers=auth_headers
        )

        assert resp.status_code == 200
        repo_obj.get_pulls.assert_called_once_with(state="all")

    async def test_per_page_capped_at_100(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.get_pulls.return_value = [_FakePull(i) for i in range(5)]
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/pulls?per_page=999", headers=auth_headers
        )

        assert resp.status_code == 200
        assert len(resp.json()) == 5

    async def test_unknown_repo_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        repo_obj = MagicMock()
        repo_obj.get_pulls.side_effect = UnknownObjectException(
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
            "/chat/github/repos/acme/ghost/pulls", headers=auth_headers
        )
        assert resp.status_code == 404


class TestGetPull:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.get("/chat/github/repos/acme/widget/pulls/1")
        assert resp.status_code == 401

    async def test_gets_single_pull(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = _FakePull(
            42, state="closed", merged=True, mergeable=None
        )
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/pulls/42", headers=auth_headers
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["number"] == 42
        assert body["state"] == "closed"
        assert body["merged"] is True
        assert body["mergeable"] is None
        repo_obj.get_pull.assert_called_once_with(42)

    async def test_pull_not_found_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        repo_obj = MagicMock()
        repo_obj.get_pull.side_effect = UnknownObjectException(
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
            "/chat/github/repos/acme/widget/pulls/9999", headers=auth_headers
        )
        assert resp.status_code == 404


class _FakeMergeStatus:
    """Stand-in for a PyGithub ``PullRequestMergeStatus``."""

    def __init__(
        self,
        *,
        merged: bool = True,
        message: str = "Pull Request successfully merged",
        sha: str = "abc123def456",
    ) -> None:
        self.merged = merged
        self.message = message
        self.sha = sha


# ---------------------------------------------------------------------------
# POST /chat/github/repos/{owner}/{repo}/pulls/{number}/merge
# ---------------------------------------------------------------------------


class TestMergePull:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/42/merge",
            json={},
        )
        assert resp.status_code == 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/42/merge",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 503

    async def test_merges_pull(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_pr = _FakePull(42)
        fake_pr.merge = MagicMock(return_value=_FakeMergeStatus(sha="sha-merged"))
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/42/merge",
            json={"merge_method": "squash", "sha": "abc123"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["merged"] is True
        assert body["sha"] == "sha-merged"
        assert "merged" in body["message"].lower()
        repo_obj.get_pull.assert_called_once_with(42)
        fake_pr.merge.assert_called_once_with(merge_method="squash", sha="abc123")

    async def test_merges_without_body_fields(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        """Empty body — merge with squash default, sha=NotSet."""
        fake_pr = _FakePull(42)
        fake_pr.merge = MagicMock(return_value=_FakeMergeStatus())
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/42/merge",
            json={},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        fake_pr.merge.assert_called_once()
        # Default merge_method is "squash"
        assert fake_pr.merge.call_args.kwargs["merge_method"] == "squash"

    async def test_merges_with_commit_title(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        """commit_title is passed through to pr.merge()."""
        fake_pr = _FakePull(42)
        fake_pr.merge = MagicMock(return_value=_FakeMergeStatus(sha="sha-commit"))
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/42/merge",
            json={"merge_method": "squash", "commit_title": "My merge title"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        fake_pr.merge.assert_called_once()
        kwargs = fake_pr.merge.call_args.kwargs
        assert kwargs["merge_method"] == "squash"
        assert kwargs["commit_title"] == "My merge title"

    async def test_records_audit_entry(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_pr = _FakePull(42)
        fake_pr.merge = MagicMock(
            return_value=_FakeMergeStatus(
                message="Pull Request successfully merged", sha="abc"
            )
        )
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/42/merge",
            json={"merge_method": "merge"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert len(entries) == 1
        assert entries[0].component == "github"
        assert entries[0].action == "merge_pull"
        assert entries[0].key == "acme/widget#42"
        assert entries[0].new_value["merge_method"] == "merge"

    async def test_pull_not_found_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        repo_obj = MagicMock()
        repo_obj.get_pull.side_effect = UnknownObjectException(
            404, data={"message": "Not Found"}
        )
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/9999/merge",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_merge_not_allowed_returns_405(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import GithubException

        fake_pr = _FakePull(42)
        fake_pr.merge = MagicMock(
            side_effect=GithubException(
                405, data={"message": "Merge queue is required for this repository"}
            )
        )
        # Also make the raw requester path fail so we surface the 405
        fake_pr.requester = MagicMock()
        fake_pr.requester.requestJsonAndCheck = MagicMock(
            side_effect=GithubException(
                405, data={"message": "Merge queue is required for this repository"}
            )
        )
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)
        # Attach requester for the raw fallback path
        fake_client.requester = fake_pr.requester

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/42/merge",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 405

    async def test_merge_conflict_returns_409(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import GithubException

        fake_pr = _FakePull(42)
        fake_pr.merge = MagicMock(
            side_effect=GithubException(409, data={"message": "Merge conflict"})
        )
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/42/merge",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    async def test_unprocessable_returns_422(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import GithubException

        fake_pr = _FakePull(42)
        fake_pr.merge = MagicMock(
            side_effect=GithubException(
                422, data={"message": "Pull Request is not mergeable"}
            )
        )
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/42/merge",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_github_404_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import GithubException

        fake_pr = _FakePull(42)
        fake_pr.merge = MagicMock(
            side_effect=GithubException(404, data={"message": "Not Found"})
        )
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/42/merge",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_raw_requester_fallback_on_405(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        """When pr.merge() returns 405, fall back to raw requester."""
        from github import GithubException

        fake_pr = _FakePull(42)
        fake_pr.merge = MagicMock(
            side_effect=GithubException(
                405, data={"message": "Merge queue is required for this repository"}
            )
        )
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr

        fake_client = _fake_client(repo_obj)
        fake_client.requester = MagicMock()
        fake_client.requester.requestJsonAndCheck.return_value = (
            {"Content-Type": "application/json"},
            {
                "merged": True,
                "message": "Enqueued in merge queue",
                "sha": "sha-enqueued",
            },
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/42/merge",
            json={"merge_method": "squash", "sha": "abc123"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["merged"] is True
        assert body["sha"] == "sha-enqueued"
        # Raw requester should have been called with the right params
        fake_client.requester.requestJsonAndCheck.assert_called_once_with(
            "PUT",
            "/repos/acme/widget/pulls/42/merge",
            input={"merge_method": "squash", "sha": "abc123"},
        )


class TestGitHubAppNotConfiguredError:
    def test_message_mentions_all_fields(self):
        # Sanity check on the error message content raised by github_app.py,
        # surfaced verbatim as the 503 detail above.
        try:
            raise GitHubAppNotConfiguredError(
                "github_app_id, github_app_private_key, and installation_id "
                "must all be set to use the github chat component."
            )
        except GitHubAppNotConfiguredError as exc:
            assert "github_app_id" in str(exc)
            assert "github_app_private_key" in str(exc)
            assert "installation_id" in str(exc)


# ---------------------------------------------------------------------------
# GET /chat/github/repos/{owner}/{repo}/actions/permissions/workflow
# ---------------------------------------------------------------------------


class TestGetWorkflowPermissions:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/permissions/workflow"
        )
        assert resp.status_code == 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/permissions/workflow",
            headers=auth_headers,
        )
        assert resp.status_code == 503

    async def test_gets_permissions(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_client = MagicMock(name="fake-github-client")
        fake_client.requester = MagicMock()
        fake_client.requester.requestJsonAndCheck.return_value = (
            {"Content-Type": "application/json"},
            {
                "default_workflow_permissions": "read",
                "can_approve_pull_request_reviews": False,
            },
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/permissions/workflow",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        }
        fake_client.requester.requestJsonAndCheck.assert_called_once_with(
            "GET", "/repos/acme/widget/actions/permissions/workflow"
        )

    async def test_unknown_repo_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        fake_client = MagicMock(name="fake-github-client")
        fake_client.requester = MagicMock()
        fake_client.requester.requestJsonAndCheck.side_effect = UnknownObjectException(
            404, data={"message": "Not Found"}
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/ghost/actions/permissions/workflow",
            headers=auth_headers,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /chat/github/repos/{owner}/{repo}/actions/permissions/workflow
# ---------------------------------------------------------------------------


class TestSetWorkflowPermissions:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.put(
            "/chat/github/repos/acme/widget/actions/permissions/workflow",
            json={
                "default_workflow_permissions": "write",
                "can_approve_pull_request_reviews": True,
            },
        )
        assert resp.status_code == 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.put(
            "/chat/github/repos/acme/widget/actions/permissions/workflow",
            json={
                "default_workflow_permissions": "write",
                "can_approve_pull_request_reviews": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 503

    async def test_sets_permissions(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_client = MagicMock(name="fake-github-client")
        fake_client.requester = MagicMock()
        fake_client.requester.requestJsonAndCheck.return_value = (
            {"Content-Type": "application/json"},
            {
                "default_workflow_permissions": "write",
                "can_approve_pull_request_reviews": True,
            },
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/actions/permissions/workflow",
            json={
                "default_workflow_permissions": "write",
                "can_approve_pull_request_reviews": True,
            },
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "default_workflow_permissions": "write",
            "can_approve_pull_request_reviews": True,
        }
        fake_client.requester.requestJsonAndCheck.assert_called_once_with(
            "PUT",
            "/repos/acme/widget/actions/permissions/workflow",
            input={
                "default_workflow_permissions": "write",
                "can_approve_pull_request_reviews": True,
            },
        )

    async def test_records_audit_entry(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_client = MagicMock(name="fake-github-client")
        fake_client.requester = MagicMock()
        fake_client.requester.requestJsonAndCheck.return_value = (
            {"Content-Type": "application/json"},
            {
                "default_workflow_permissions": "write",
                "can_approve_pull_request_reviews": True,
            },
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/actions/permissions/workflow",
            json={
                "default_workflow_permissions": "write",
                "can_approve_pull_request_reviews": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert len(entries) == 1
        assert entries[0].component == "github"
        assert entries[0].action == "set_workflow_permissions"
        assert entries[0].key == "acme/widget"
        assert entries[0].new_value == {
            "default_workflow_permissions": "write",
            "can_approve_pull_request_reviews": True,
        }

    async def test_unknown_repo_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        fake_client = MagicMock(name="fake-github-client")
        fake_client.requester = MagicMock()
        fake_client.requester.requestJsonAndCheck.side_effect = UnknownObjectException(
            404, data={"message": "Not Found"}
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/ghost/actions/permissions/workflow",
            json={
                "default_workflow_permissions": "write",
                "can_approve_pull_request_reviews": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_invalid_permissions_value_returns_422(
        self, client: AsyncClient, auth_headers: dict, enable_github_app
    ):
        resp = await client.put(
            "/chat/github/repos/acme/widget/actions/permissions/workflow",
            json={
                "default_workflow_permissions": "admin",
                "can_approve_pull_request_reviews": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_missing_required_fields_returns_422(
        self, client: AsyncClient, auth_headers: dict, enable_github_app
    ):
        resp = await client.put(
            "/chat/github/repos/acme/widget/actions/permissions/workflow",
            json={"default_workflow_permissions": "read"},
            headers=auth_headers,
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Fake classes for reviews and review comments
# ---------------------------------------------------------------------------


class _FakeReview:
    """Stand-in for a PyGithub ``PullRequestReview``."""

    def __init__(
        self,
        review_id: int,
        *,
        user_login: str = "reviewer",
        state: str = "APPROVED",
        submitted_at: datetime | None = None,
        commit_id: str = "abc123",
        body: str = "LGTM",
    ) -> None:
        self.id = review_id
        self.user = _FakeUser(user_login)
        self.state = state
        self.submitted_at = submitted_at or datetime(
            2026, 7, 7, 12, 10, 0, tzinfo=timezone.utc
        )
        self.commit_id = commit_id
        self.body = body


class _FakeReviewComment:
    """Stand-in for a PyGithub ``PullRequestComment`` (inline review comment)."""

    def __init__(
        self,
        comment_id: int,
        *,
        path: str = "src/app.py",
        line: int | None = 42,
        body: str = "Consider using a constant here.",
        user_login: str = "reviewer",
        in_reply_to_id: int | None = None,
        commit_id: str = "abc123",
        created_at: datetime | None = None,
    ) -> None:
        self.id = comment_id
        self.path = path
        self.line = line
        self.body = body
        self.user = _FakeUser(user_login)
        self.in_reply_to_id = in_reply_to_id
        self.commit_id = commit_id
        self.created_at = created_at or datetime(
            2026, 7, 7, 12, 10, 0, tzinfo=timezone.utc
        )


# ---------------------------------------------------------------------------
# GET /chat/github/repos/{owner}/{repo}/pulls/{number}/reviews
# ---------------------------------------------------------------------------


class TestListReviews:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.get("/chat/github/repos/acme/widget/pulls/1/reviews")
        assert resp.status_code == 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get(
            "/chat/github/repos/acme/widget/pulls/1/reviews", headers=auth_headers
        )
        assert resp.status_code == 503

    async def test_lists_reviews(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        fake_pr = MagicMock()
        fake_pr.get_reviews.return_value = [
            _FakeReview(1, state="APPROVED"),
            _FakeReview(2, state="CHANGES_REQUESTED", body="Needs work"),
        ]
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/pulls/1/reviews", headers=auth_headers
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0] == {
            "id": 1,
            "user": "reviewer",
            "state": "APPROVED",
            "submitted_at": "2026-07-07T12:10:00+00:00",
            "commit_id": "abc123",
            "body": "LGTM",
        }
        assert body[1]["state"] == "CHANGES_REQUESTED"
        assert body[1]["body"] == "Needs work"

    async def test_per_page_capped_at_100(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        fake_pr = MagicMock()
        fake_pr.get_reviews.return_value = [_FakeReview(i) for i in range(5)]
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/pulls/1/reviews?per_page=999",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 5

    async def test_unknown_repo_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        fake_client = MagicMock(name="fake-github-client")
        fake_client.get_repo.side_effect = UnknownObjectException(
            404, data={"message": "Not Found"}
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/ghost/pulls/1/reviews", headers=auth_headers
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /chat/github/repos/{owner}/{repo}/pulls/{number}/comments
# ---------------------------------------------------------------------------


class TestListReviewComments:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.get("/chat/github/repos/acme/widget/pulls/1/comments")
        assert resp.status_code == 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get(
            "/chat/github/repos/acme/widget/pulls/1/comments", headers=auth_headers
        )
        assert resp.status_code == 503

    async def test_lists_comments(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        fake_pr = MagicMock()
        fake_pr.get_review_comments.return_value = [
            _FakeReviewComment(1, path="src/app.py", line=42),
            _FakeReviewComment(2, path="src/util.py", line=10, in_reply_to_id=1),
        ]
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/pulls/1/comments", headers=auth_headers
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0] == {
            "id": 1,
            "path": "src/app.py",
            "line": 42,
            "body": "Consider using a constant here.",
            "user": "reviewer",
            "in_reply_to_id": None,
            "commit_id": "abc123",
            "created_at": "2026-07-07T12:10:00+00:00",
        }
        assert body[1]["in_reply_to_id"] == 1

    async def test_unknown_repo_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        fake_client = MagicMock(name="fake-github-client")
        fake_client.get_repo.side_effect = UnknownObjectException(
            404, data={"message": "Not Found"}
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/ghost/pulls/1/comments", headers=auth_headers
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /chat/github/repos/{owner}/{repo}/pulls/{number}/reviews
# ---------------------------------------------------------------------------


class TestCreateReview:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/1/reviews",
            json={"event": "APPROVE"},
        )
        assert resp.status_code == 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/1/reviews",
            json={"event": "APPROVE"},
            headers=auth_headers,
        )
        assert resp.status_code == 503

    async def test_invalid_event_returns_422(
        self, client: AsyncClient, auth_headers: dict, enable_github_app
    ):
        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/1/reviews",
            json={"event": "INVALID"},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_creates_review(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_pr = MagicMock()
        fake_pr.create_review.return_value = _FakeReview(
            1, state="APPROVED", body="Looks good!"
        )
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/1/reviews",
            json={"event": "APPROVE", "body": "Looks good!"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == 1
        assert body["state"] == "APPROVED"
        assert body["body"] == "Looks good!"
        fake_pr.create_review.assert_called_once_with(
            event="APPROVE", body="Looks good!"
        )

    async def test_creates_comment_review(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_pr = MagicMock()
        fake_pr.create_review.return_value = _FakeReview(
            2, state="COMMENTED", body="Just a note."
        )
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/1/reviews",
            json={"event": "COMMENT", "body": "Just a note."},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "COMMENTED"

    async def test_records_audit_entry(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_pr = MagicMock()
        fake_pr.create_review.return_value = _FakeReview(1, state="APPROVED")
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/1/reviews",
            json={"event": "APPROVE"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert len(entries) == 1
        assert entries[0].component == "github"
        assert entries[0].action == "create_review"
        assert entries[0].key == "acme/widget#1"

    async def test_self_approval_falls_back_to_pat(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
        enable_repo_create_token,
    ):
        """When the App token fails with a self-approval 422, fall back to PAT."""
        from github import GithubException

        fake_pr = MagicMock()
        fake_pr.create_review.side_effect = GithubException(
            422,
            data={
                "message": "You cannot approve your own pull request",
                "errors": [{"message": "self-approval is not allowed"}],
            },
        )
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_app_client = _fake_client(repo_obj)

        # PAT client — used for fallback via raw requester
        fake_pat_client = MagicMock(name="fake-pat-client")
        fake_pat_client.requester = MagicMock()
        fake_pat_client.requester.requestJsonAndCheck.return_value = (
            {"Content-Type": "application/json"},
            {
                "id": 99,
                "user": {"login": "pat-user"},
                "state": "APPROVED",
                "submitted_at": "2026-07-07T12:30:00Z",
                "commit_id": "def456",
                "body": "",
            },
        )

        async def _fake_get_app_client(config, owner, repo):
            return fake_app_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_app_client,
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_repo_create_client",
            lambda config: fake_pat_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/1/reviews",
            json={"event": "APPROVE"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == 99
        assert body["user"] == "pat-user"
        assert body["state"] == "APPROVED"

    async def test_self_approval_no_pat_fallback_returns_422(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        """Self-approval 422 without a PAT configured returns 422."""
        from github import GithubException

        fake_pr = MagicMock()
        fake_pr.create_review.side_effect = GithubException(
            422,
            data={
                "message": "You cannot approve your own pull request",
            },
        )
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )
        # Ensure no PAT is configured
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_repo_create_client",
            lambda config: (_ for _ in ()).throw(
                GitHubRepoCreateNotConfiguredError("No PAT")
            ),
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/pulls/1/reviews",
            json={"event": "APPROVE"},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_unknown_repo_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        fake_client = MagicMock(name="fake-github-client")
        fake_client.get_repo.side_effect = UnknownObjectException(
            404, data={"message": "Not Found"}
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/ghost/pulls/1/reviews",
            json={"event": "APPROVE"},
            headers=auth_headers,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /chat/github/repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}/
#     dismissals
# ---------------------------------------------------------------------------


class TestDismissReview:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.put(
            "/chat/github/repos/acme/widget/pulls/1/reviews/1/dismissals",
            json={"message": "Stale review"},
        )
        assert resp.status_code == 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.put(
            "/chat/github/repos/acme/widget/pulls/1/reviews/1/dismissals",
            json={"message": "Stale review"},
            headers=auth_headers,
        )
        assert resp.status_code == 503

    async def test_missing_message_returns_422(
        self, client: AsyncClient, auth_headers: dict, enable_github_app
    ):
        resp = await client.put(
            "/chat/github/repos/acme/widget/pulls/1/reviews/1/dismissals",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_dismisses_review(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_pr = MagicMock()
        fake_review = _FakeReview(1, state="DISMISSED", body="Stale review")
        fake_review.dismiss = MagicMock()
        fake_pr.get_review.return_value = fake_review
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/pulls/1/reviews/1/dismissals",
            json={"message": "Stale review"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "DISMISSED"
        fake_review.dismiss.assert_called_once_with("Stale review")

    async def test_records_audit_entry(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_pr = MagicMock()
        fake_review = _FakeReview(1, state="DISMISSED")
        fake_review.dismiss = MagicMock()
        fake_pr.get_review.return_value = fake_review
        repo_obj = MagicMock()
        repo_obj.get_pull.return_value = fake_pr
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/pulls/1/reviews/1/dismissals",
            json={"message": "Stale review"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert len(entries) == 1
        assert entries[0].component == "github"
        assert entries[0].action == "dismiss_review"
        assert entries[0].key == "acme/widget#1/reviews/1"

    async def test_unknown_repo_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        fake_client = MagicMock(name="fake-github-client")
        fake_client.get_repo.side_effect = UnknownObjectException(
            404, data={"message": "Not Found"}
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/ghost/pulls/1/reviews/1/dismissals",
            json={"message": "Stale review"},
            headers=auth_headers,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /chat/github/repos/{owner}/{repo}/relax-merge-gate
# ---------------------------------------------------------------------------


class _FakeBranch:
    """Stand-in for a PyGithub ``Branch`` with protection support."""

    def __init__(
        self,
        *,
        required_approving_review_count: int = 1,
        required_status_checks: list[str] | None = None,
    ) -> None:
        self._required_approving_review_count = required_approving_review_count
        self._required_status_checks = required_status_checks or []
        self.edit_required_pull_request_reviews = MagicMock()
        self.get_protection = MagicMock()

    @property
    def raw_data(self) -> dict:
        """Simulate the protection data returned after a mutation."""
        return self.get_protection.return_value.raw_data


class TestRelaxMergeGate:
    @pytest.fixture(autouse=True)
    def _clear_audit(self):
        """Ensure audit store is clean before each test."""
        server_mod.app.state.chat_agent_audit_store._entries = []
        yield

    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.post("/chat/github/repos/acme/widget/relax-merge-gate")
        assert resp.status_code == 401

    async def test_503_when_neither_credential_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.post(
            "/chat/github/repos/acme/widget/relax-merge-gate",
            headers=auth_headers,
        )
        assert resp.status_code == 503

    async def test_relaxes_default_branch(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        protection_data: dict[str, object] = {
            "required_status_checks": {
                "strict": True,
                "contexts": ["CI"],
            },
            "required_pull_request_reviews": {
                "required_approving_review_count": 0,
            },
            "enforce_admins": False,
        }

        fake_protection = MagicMock()
        fake_protection.raw_data = protection_data

        fake_branch = _FakeBranch()
        fake_branch.get_protection.return_value = fake_protection

        fake_repo = _FakeRepo(default_branch="main")
        fake_repo.get_branch = MagicMock(return_value=fake_branch)

        fake_client = MagicMock(name="fake-github-client")
        fake_client.get_repo.return_value = fake_repo

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/relax-merge-gate",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body == protection_data
        fake_repo.get_branch.assert_called_once_with("main")
        fake_branch.edit_required_pull_request_reviews.assert_called_once_with(
            required_approving_review_count=0
        )

    async def test_relaxes_custom_branch(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        protection_data: dict[str, object] = {
            "required_status_checks": {"contexts": ["CI"]},
            "required_pull_request_reviews": {
                "required_approving_review_count": 0,
            },
        }

        fake_protection = MagicMock()
        fake_protection.raw_data = protection_data

        fake_branch = _FakeBranch()
        fake_branch.get_protection.return_value = fake_protection

        fake_repo = _FakeRepo(default_branch="main")
        fake_repo.get_branch = MagicMock(return_value=fake_branch)

        fake_client = MagicMock(name="fake-github-client")
        fake_client.get_repo.return_value = fake_repo

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/relax-merge-gate",
            json={"branch": "develop"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        fake_repo.get_branch.assert_called_once_with("develop")

    async def test_records_audit_entry(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        protection_data: dict[str, object] = {
            "required_pull_request_reviews": {
                "required_approving_review_count": 0,
            },
        }

        fake_protection = MagicMock()
        fake_protection.raw_data = protection_data

        fake_branch = _FakeBranch()
        fake_branch.get_protection.return_value = fake_protection

        fake_repo = _FakeRepo(default_branch="main")
        fake_repo.get_branch = MagicMock(return_value=fake_branch)

        fake_client = MagicMock(name="fake-github-client")
        fake_client.get_repo.return_value = fake_repo

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/relax-merge-gate",
            headers=auth_headers,
        )
        assert resp.status_code == 200

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert len(entries) == 1
        assert entries[0].component == "github"
        assert entries[0].action == "relax_merge_gate"
        assert entries[0].key == "acme/widget"

    async def test_records_audit_entry_with_branch(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        protection_data: dict[str, object] = {
            "required_pull_request_reviews": {
                "required_approving_review_count": 0,
            },
        }

        fake_protection = MagicMock()
        fake_protection.raw_data = protection_data

        fake_branch = _FakeBranch()
        fake_branch.get_protection.return_value = fake_protection

        fake_repo = _FakeRepo(default_branch="main")
        fake_repo.get_branch = MagicMock(return_value=fake_branch)

        fake_client = MagicMock(name="fake-github-client")
        fake_client.get_repo.return_value = fake_repo

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/relax-merge-gate",
            json={"branch": "staging"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert len(entries) == 1
        assert entries[0].new_value == {"branch": "staging"}

    async def test_unknown_repo_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        from github import UnknownObjectException

        fake_client = MagicMock(name="fake-github-client")
        fake_client.get_repo.side_effect = UnknownObjectException(
            404, data={"message": "Not Found"}
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/ghost/relax-merge-gate",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_pat_fallback_when_app_not_configured(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_repo_create_token,
    ):
        """When the App is not configured, the PAT should be used instead."""
        protection_data: dict[str, object] = {
            "required_pull_request_reviews": {
                "required_approving_review_count": 0,
            },
        }

        fake_protection = MagicMock()
        fake_protection.raw_data = protection_data

        fake_branch = _FakeBranch()
        fake_branch.get_protection.return_value = fake_protection

        fake_repo = _FakeRepo(default_branch="main")
        fake_repo.get_branch = MagicMock(return_value=fake_branch)

        fake_client = _fake_client(fake_repo)

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_repo_create_client",
            lambda config: fake_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/relax-merge-gate",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        fake_branch.edit_required_pull_request_reviews.assert_called_once_with(
            required_approving_review_count=0
        )
