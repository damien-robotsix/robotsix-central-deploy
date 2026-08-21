"""Integration tests for chat-agent GitHub security-feature endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

pytest.importorskip("github")

import robotsix_central_deploy.lifecycle.app as server_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


class _FakeRepo:
    """Stand-in for a PyGithub ``Repository``."""

    def __init__(self, *, full_name: str = "acme/widget") -> None:
        self.full_name = full_name
        self.raw_data: dict = {"security_and_analysis": {}}


# ---------------------------------------------------------------------------
# PUT /chat/github/repos/{owner}/{repo}/vulnerability-alerts
# ---------------------------------------------------------------------------


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
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
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
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
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

        resp = await client.put(
            "/chat/github/repos/acme/ghost/vulnerability-alerts",
            headers=auth_headers,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /chat/github/repos/{owner}/{repo}/security-features
# ---------------------------------------------------------------------------


class TestSecurityFeatures:
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
        self,
        client: AsyncClient,
        auth_headers: dict,
        enable_github_app,
    ):
        resp = await client.put(
            "/chat/github/repos/acme/widget/security-features",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_enables_all_features(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
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

    async def test_disables_all_features(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
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
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
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

    async def test_enables_dependabot_alerts_only(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
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
            json={"dependabot_alerts": True},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        fake_repo.enable_vulnerability_alert.assert_called_once()

    async def test_enables_security_updates_only(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
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

    async def test_disables_dependency_graph_only(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        fake_repo = _FakeRepo()
        fake_repo.disable_vulnerability_alert = MagicMock(return_value=True)
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
            json={"dependency_graph": False},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        fake_repo.disable_vulnerability_alert.assert_called_once()

    async def test_enables_vuln_alerts_disables_updates(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        """Mixed: enable vulnerability alerts, disable automated security fixes."""
        fake_repo = _FakeRepo()
        fake_repo.enable_vulnerability_alert = MagicMock(return_value=True)
        fake_repo.disable_automated_security_fixes = MagicMock(return_value=True)
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
                "dependabot_alerts": True,
                "dependabot_security_updates": False,
            },
            headers=auth_headers,
        )

        assert resp.status_code == 200
        fake_repo.enable_vulnerability_alert.assert_called_once()
        fake_repo.disable_automated_security_fixes.assert_called_once()

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

        resp = await client.put(
            "/chat/github/repos/acme/ghost/security-features",
            json={"dependency_graph": True},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_records_audit_entry(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
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

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert len(entries) == 1
        assert entries[0].component == "github"
        assert entries[0].action == "set_security_features"
        assert entries[0].key == "acme/widget"
