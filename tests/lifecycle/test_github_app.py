"""Tests for the GitHub App client wrapper (``lifecycle.github_app``)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.github_app import (
    GitHubAppNotConfiguredError,
    _client_cache,
    get_github_client,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with an empty client cache."""
    _client_cache.clear()
    yield
    _client_cache.clear()


def _cfg(**overrides: object) -> LifecycleConfig:
    return LifecycleConfig(**overrides)  # type: ignore[call-arg]


async def test_raises_when_app_id_missing():
    cfg = _cfg(github_app_id="", github_app_private_key="pem-data")
    with pytest.raises(GitHubAppNotConfiguredError):
        await get_github_client(cfg, "owner", "repo")


async def test_raises_when_private_key_missing():
    cfg = _cfg(github_app_id="12345", github_app_private_key="")
    with pytest.raises(GitHubAppNotConfiguredError):
        await get_github_client(cfg, "owner", "repo")


async def test_builds_client_via_pygithub_app_auth():
    cfg = _cfg(github_app_id="12345", github_app_private_key="pem-data")
    fake_client = MagicMock(name="fake-github-client")
    fake_installation = MagicMock(id=999)

    with patch(
        "robotsix_central_deploy.lifecycle.github_app._build_client_sync",
        return_value=fake_client,
    ) as mock_build:
        result = await get_github_client(cfg, "owner", "repo")

    assert result is fake_client
    mock_build.assert_called_once_with("12345", "pem-data", "owner", "repo")
    del fake_installation  # unused placeholder for clarity of intent


async def test_client_is_cached_across_calls():
    """A second call for the same (app_id, owner, repo) reuses the cached
    client instead of rebuilding it — PyGithub's own AppInstallationAuth
    already handles token refresh internally."""
    cfg = _cfg(github_app_id="12345", github_app_private_key="pem-data")
    fake_client = MagicMock(name="fake-github-client")

    with patch(
        "robotsix_central_deploy.lifecycle.github_app._build_client_sync",
        return_value=fake_client,
    ) as mock_build:
        first = await get_github_client(cfg, "owner", "repo")
        second = await get_github_client(cfg, "owner", "repo")

    assert first is second is fake_client
    mock_build.assert_called_once()


async def test_different_repos_get_different_cache_entries():
    cfg = _cfg(github_app_id="12345", github_app_private_key="pem-data")
    fake_client_a = MagicMock(name="client-a")
    fake_client_b = MagicMock(name="client-b")

    with patch(
        "robotsix_central_deploy.lifecycle.github_app._build_client_sync",
        side_effect=[fake_client_a, fake_client_b],
    ) as mock_build:
        result_a = await get_github_client(cfg, "owner", "repo-a")
        result_b = await get_github_client(cfg, "owner", "repo-b")

    assert result_a is fake_client_a
    assert result_b is fake_client_b
    assert mock_build.call_count == 2


def test_build_client_sync_uses_app_auth_and_installation_lookup():
    """_build_client_sync wires Auth.AppAuth + GithubIntegration correctly."""
    with patch("github.GithubIntegration") as mock_integration_cls:
        mock_integration = mock_integration_cls.return_value
        mock_installation = MagicMock(id=42)
        mock_integration.get_repo_installation.return_value = mock_installation
        mock_integration.get_github_for_installation.return_value = "the-client"

        from robotsix_central_deploy.lifecycle.github_app import _build_client_sync

        result = _build_client_sync("app-id", "private-key", "owner", "repo")

    mock_integration.get_repo_installation.assert_called_once_with("owner", "repo")
    mock_integration.get_github_for_installation.assert_called_once_with(42)
    assert result == "the-client"
