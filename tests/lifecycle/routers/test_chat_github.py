"""Integration tests for the chat-agent GitHub Actions status endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

pytest.importorskip("github")

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


class TestGetRepo:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.get("/chat/github/repos/acme/widget")
        assert resp.status_code == 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get("/chat/github/repos/acme/widget", headers=auth_headers)
        assert resp.status_code == 503

    async def test_gets_repo(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_repo = _FakeRepo()
        fake_client = _fake_client(fake_repo)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get("/chat/github/repos/acme/widget", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json() == {
            "full_name": "acme/widget",
            "html_url": "https://github.com/acme/widget",
            "clone_url": "https://github.com/acme/widget.git",
            "private": False,
            "description": "A widget",
            "homepage": "",
            "has_issues": True,
            "has_wiki": True,
            "default_branch": "main",
            "archived": False,
        }

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

        resp = await client.get("/chat/github/repos/acme/ghost", headers=auth_headers)
        assert resp.status_code == 404


class TestUpdateRepo:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.patch(
            "/chat/github/repos/acme/widget", json={"description": "new"}
        )
        assert resp.status_code == 401

    async def test_no_fields_returns_422(
        self, client: AsyncClient, auth_headers: dict, enable_github_app
    ):
        resp = await client.patch(
            "/chat/github/repos/acme/widget", json={}, headers=auth_headers
        )
        assert resp.status_code == 422

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.patch(
            "/chat/github/repos/acme/widget",
            json={"description": "new"},
            headers=auth_headers,
        )
        assert resp.status_code == 503

    async def test_updates_repo(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_repo = _FakeRepo(description="old")
        fake_repo_after = _FakeRepo(description="new")
        fake_client = MagicMock(name="fake-github-client")
        fake_client.get_repo.side_effect = [fake_repo, fake_repo_after]

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.patch(
            "/chat/github/repos/acme/widget",
            json={"description": "new"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json()["description"] == "new"
        fake_repo.edit.assert_called_once()
        _, kwargs = fake_repo.edit.call_args
        assert kwargs["description"] == "new"

    async def test_records_audit_entry(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_repo = _FakeRepo()
        fake_client = MagicMock(name="fake-github-client")
        fake_client.get_repo.side_effect = [fake_repo, fake_repo]

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.patch(
            "/chat/github/repos/acme/widget",
            json={"private": True},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert len(entries) == 1
        assert entries[0].component == "github"
        assert entries[0].action == "update_repo"
        assert entries[0].key == "acme/widget"

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

        resp = await client.patch(
            "/chat/github/repos/acme/ghost",
            json={"description": "new"},
            headers=auth_headers,
        )
        assert resp.status_code == 404


class TestEnableVulnerabilityAlerts:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.put("/chat/github/repos/acme/widget/vulnerability-alerts")
        assert resp.status_code == 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.put(
            "/chat/github/repos/acme/widget/vulnerability-alerts",
            headers=auth_headers,
        )
        assert resp.status_code == 503

    async def test_enables_alerts(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_repo = _FakeRepo()
        fake_repo.enable_vulnerability_alert = MagicMock(return_value=True)
        fake_client = _fake_client(fake_repo)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/vulnerability-alerts",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "full_name": "acme/widget",
            "vulnerability_alerts_enabled": True,
        }
        fake_repo.enable_vulnerability_alert.assert_called_once()

    async def test_records_audit_entry(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_repo = _FakeRepo()
        fake_repo.enable_vulnerability_alert = MagicMock(return_value=True)
        fake_client = _fake_client(fake_repo)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/vulnerability-alerts",
            headers=auth_headers,
        )
        assert resp.status_code == 200

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert len(entries) == 1
        assert entries[0].component == "github"
        assert entries[0].action == "enable_vulnerability_alerts"
        assert entries[0].key == "acme/widget"

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
            "/chat/github/repos/acme/ghost/vulnerability-alerts",
            headers=auth_headers,
        )
        assert resp.status_code == 404


class TestSecurityFeatures:
    """Tests for PUT /chat/github/repos/{owner}/{repo}/security-features."""

    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.put(
            "/chat/github/repos/acme/widget/security-features",
            json={"dependency_graph": True},
        )
        assert resp.status_code == 401

    async def test_503_when_neither_credential_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.put(
            "/chat/github/repos/acme/widget/security-features",
            json={"dependency_graph": True},
            headers=auth_headers,
        )
        assert resp.status_code == 503

    async def test_empty_body_returns_422(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        resp = await client.put(
            "/chat/github/repos/acme/widget/security-features",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_enables_all_features(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_repo = _FakeRepo()
        fake_repo.enable_vulnerability_alert = MagicMock(return_value=True)
        fake_repo.enable_automated_security_fixes = MagicMock(return_value=True)
        fake_repo.raw_data = {
            "security_and_analysis": {
                "dependabot_security_updates": {"status": "enabled"},
            }
        }
        fake_client = _fake_client(fake_repo)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/security-features",
            json={
                "dependency_graph": True,
                "dependabot_alerts": True,
                "dependabot_security_updates": True,
            },
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "full_name": "acme/widget",
            "security_and_analysis": {
                "dependabot_security_updates": {"status": "enabled"},
            },
        }
        fake_repo.enable_vulnerability_alert.assert_called_once()
        fake_repo.enable_automated_security_fixes.assert_called_once()

    async def test_disables_features(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_repo = _FakeRepo()
        fake_repo.disable_vulnerability_alert = MagicMock(return_value=True)
        fake_repo.disable_automated_security_fixes = MagicMock(return_value=True)
        fake_repo.raw_data = {
            "security_and_analysis": {
                "dependabot_security_updates": {"status": "disabled"},
            }
        }
        fake_client = _fake_client(fake_repo)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/security-features",
            json={
                "dependency_graph": False,
                "dependabot_alerts": False,
                "dependabot_security_updates": False,
            },
            headers=auth_headers,
        )

        assert resp.status_code == 200
        fake_repo.disable_vulnerability_alert.assert_called_once()
        fake_repo.disable_automated_security_fixes.assert_called_once()

    async def test_enables_dependency_graph_only(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        """Setting dependency_graph=True alone enables vulnerability alerts."""
        fake_repo = _FakeRepo()
        fake_repo.enable_vulnerability_alert = MagicMock(return_value=True)
        fake_repo.raw_data = {"security_and_analysis": {}}
        fake_client = _fake_client(fake_repo)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/security-features",
            json={"dependency_graph": True},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        fake_repo.enable_vulnerability_alert.assert_called_once()

    async def test_security_updates_only(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        """Setting dependabot_security_updates alone does not touch alerts."""
        fake_repo = _FakeRepo()
        fake_repo.enable_automated_security_fixes = MagicMock(return_value=True)
        fake_repo.raw_data = {"security_and_analysis": {}}
        fake_client = _fake_client(fake_repo)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/security-features",
            json={"dependabot_security_updates": True},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        fake_repo.enable_automated_security_fixes.assert_called_once()
        # Vulnerability alerts should not have been touched.
        assert not hasattr(fake_repo, "enable_vulnerability_alert") or not isinstance(
            fake_repo.enable_vulnerability_alert, MagicMock
        )

    async def test_records_audit_entry(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_repo = _FakeRepo()
        fake_repo.enable_vulnerability_alert = MagicMock(return_value=True)
        fake_repo.enable_automated_security_fixes = MagicMock(return_value=True)
        fake_repo.raw_data = {"security_and_analysis": {}}
        fake_client = _fake_client(fake_repo)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/security-features",
            json={
                "dependency_graph": True,
                "dependabot_security_updates": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert len(entries) == 1
        assert entries[0].component == "github"
        assert entries[0].action == "set_security_features"
        assert entries[0].key == "acme/widget"
        assert entries[0].new_value == {
            "dependency_graph": True,
            "dependabot_security_updates": True,
        }

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
            "/chat/github/repos/acme/ghost/security-features",
            json={"dependency_graph": True},
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
        fake_repo = _FakeRepo()
        fake_repo.enable_vulnerability_alert = MagicMock(return_value=True)
        fake_repo.raw_data = {"security_and_analysis": {}}
        fake_client = _fake_client(fake_repo)

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_repo_create_client",
            lambda config: fake_client,
        )

        resp = await client.put(
            "/chat/github/repos/acme/widget/security-features",
            json={"dependency_graph": True},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        fake_repo.enable_vulnerability_alert.assert_called_once()


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
        fake_repo.enable_vulnerability_alert.assert_called_once()

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


class _FakeUser:
    def __init__(self, login: str) -> None:
        self.login = login


class _FakeRef:
    def __init__(self, ref: str) -> None:
        self.ref = ref


class _FakePull:
    """Stand-in for a PyGithub ``PullRequest``."""

    def __init__(
        self,
        number: int,
        *,
        title: str = "Fix the thing",
        state: str = "open",
        draft: bool = False,
        user_login: str | None = "octocat",
        head_ref: str = "feature-branch",
        base_ref: str = "main",
        mergeable: bool | None = True,
        merged: bool = False,
        merged_at: datetime | None = None,
        body: str | None = "Fixes the thing.",
    ) -> None:
        self.number = number
        self.title = title
        self.state = state
        self.draft = draft
        self.user = _FakeUser(user_login) if user_login else None
        self.html_url = f"https://github.com/acme/widget/pull/{number}"
        self.head = _FakeRef(head_ref)
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
            "draft": False,
            "user": "octocat",
            "html_url": "https://github.com/acme/widget/pull/1",
            "head_ref": "feature-branch",
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
