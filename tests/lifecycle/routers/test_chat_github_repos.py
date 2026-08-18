"""Integration tests for the chat-agent GitHub repository management endpoints."""

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
    """Configure github_app_id/private_key/installation_id so the endpoints don't 503."""
    from pydantic import SecretStr

    server_mod.app.state.config.github_app_id = "12345"
    server_mod.app.state.config.github_app_private_key = SecretStr("pem-data")
    server_mod.app.state.config.installation_id = SecretStr("999")
    yield
    server_mod.app.state.config.github_app_id = ""
    server_mod.app.state.config.github_app_private_key = SecretStr("")
    server_mod.app.state.config.installation_id = SecretStr("")


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
    async def test_unauthorized_no_longer_401(self, client: AsyncClient):
        resp = await client.get("/chat/github/repos/acme/widget")
        assert resp.status_code != 401

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
    async def test_unauthorized_no_longer_401(self, client: AsyncClient):
        resp = await client.patch(
            "/chat/github/repos/acme/widget", json={"description": "new"}
        )
        assert resp.status_code != 401

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
    async def test_unauthorized_no_longer_401(self, client: AsyncClient):
        resp = await client.put("/chat/github/repos/acme/widget/vulnerability-alerts")
        assert resp.status_code != 401

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

    async def test_unauthorized_no_longer_401(self, client: AsyncClient):
        resp = await client.put(
            "/chat/github/repos/acme/widget/security-features",
            json={"dependency_graph": True},
        )
        assert resp.status_code != 401

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
    async def test_unauthorized_no_longer_401(self, client: AsyncClient):
        resp = await client.post("/chat/github/repos", json={"name": "widget"})
        assert resp.status_code != 401

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


class TestUpdateRepoExtended:
    """Tests for the extended PATCH: allow_auto_merge, delete_branch_on_merge,
    and unknown-key rejection."""

    async def test_updates_allow_auto_merge(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_repo = _FakeRepo()
        fake_repo_after = _FakeRepo()
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
            json={"allow_auto_merge": True},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        fake_repo.edit.assert_called_once()
        _, kwargs = fake_repo.edit.call_args
        assert kwargs["allow_auto_merge"] is True

    async def test_updates_delete_branch_on_merge(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        fake_repo = _FakeRepo()
        fake_repo_after = _FakeRepo()
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
            json={"delete_branch_on_merge": True},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        fake_repo.edit.assert_called_once()
        _, kwargs = fake_repo.edit.call_args
        assert kwargs["delete_branch_on_merge"] is True

    async def test_unknown_key_returns_422(
        self, client: AsyncClient, auth_headers: dict, enable_github_app
    ):
        resp = await client.patch(
            "/chat/github/repos/acme/widget",
            json={"not_a_real_field": True},
            headers=auth_headers,
        )
        assert resp.status_code == 422
