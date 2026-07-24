"""Tests for the GitHub App client wrapper (``lifecycle.github_app``)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("github")

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
    cfg = _cfg(
        github_app_id="",
        github_app_private_key="pem-data",
        installation_id="12345",
    )
    with pytest.raises(GitHubAppNotConfiguredError):
        await get_github_client(cfg, "owner", "repo")


async def test_raises_when_private_key_missing():
    cfg = _cfg(
        github_app_id="12345",
        github_app_private_key="",
        installation_id="12345",
    )
    with pytest.raises(GitHubAppNotConfiguredError):
        await get_github_client(cfg, "owner", "repo")


async def test_raises_when_installation_id_missing():
    cfg = _cfg(
        github_app_id="12345",
        github_app_private_key="pem-data",
        installation_id="",
    )
    with pytest.raises(GitHubAppNotConfiguredError):
        await get_github_client(cfg, "owner", "repo")


async def test_builds_client_via_mint_installation_token():
    cfg = _cfg(
        github_app_id="12345",
        github_app_private_key="pem-data",
        installation_id="999",
    )
    fake_client = MagicMock(name="fake-github-client")

    with patch(
        "robotsix_central_deploy.lifecycle.github_app._mint_installation_token",
        return_value=SimpleNamespace(token="ghs_test-token"),
    ) as mock_mint:
        with patch(
            "robotsix_central_deploy.lifecycle.github_app._bearer_client",
            return_value=fake_client,
        ) as mock_bearer:
            result = await get_github_client(cfg, "owner", "repo")

    assert result is fake_client
    mock_mint.assert_called_once_with("12345", "pem-data", "999")
    mock_bearer.assert_called_once_with("ghs_test-token")


async def test_client_reused_while_token_is_unchanged():
    """While the minted token is unchanged the cached client is reused
    (no rebuild), even though the token is re-minted each call.

    ``mint_installation_token`` is called on every request — it keeps its
    own TTL cache — but ``_bearer_client`` (which wraps the static token)
    is only invoked once because the token string does not change."""
    cfg = _cfg(
        github_app_id="12345",
        github_app_private_key="pem-data",
        installation_id="999",
    )
    fake_client = MagicMock(name="fake-github-client")

    with patch(
        "robotsix_central_deploy.lifecycle.github_app._mint_installation_token",
        return_value=SimpleNamespace(token="ghs_test-token"),
    ) as mock_mint:
        with patch(
            "robotsix_central_deploy.lifecycle.github_app._bearer_client",
            return_value=fake_client,
        ) as mock_bearer:
            first = await get_github_client(cfg, "owner-a", "repo-a")
            second = await get_github_client(cfg, "owner-b", "repo-b")

    assert first is second is fake_client
    # Token minted on every call (cheap TTL-cached lookup)...
    assert mock_mint.call_count == 2
    # ...but the client is built only once while the token is unchanged.
    mock_bearer.assert_called_once_with("ghs_test-token")


async def test_client_rebuilt_when_token_rotates():
    """Regression: when the installation token rotates (the library
    re-mints ~5 min before expiry), a fresh client wrapping the new token
    is built — the stale client must not be served indefinitely.

    A client cached by ``installation_id`` alone would keep sending the
    first, now-expired token and every request would 401 "Bad
    credentials" ~1h after startup."""
    cfg = _cfg(
        github_app_id="12345",
        github_app_private_key="pem-data",
        installation_id="999",
    )
    old_client = MagicMock(name="old-client")
    new_client = MagicMock(name="new-client")

    with patch(
        "robotsix_central_deploy.lifecycle.github_app._mint_installation_token",
        side_effect=[
            SimpleNamespace(token="ghs_old-token"),
            SimpleNamespace(token="ghs_new-token"),
        ],
    ) as mock_mint:
        with patch(
            "robotsix_central_deploy.lifecycle.github_app._bearer_client",
            side_effect=[old_client, new_client],
        ) as mock_bearer:
            first = await get_github_client(cfg, "owner", "repo")
            second = await get_github_client(cfg, "owner", "repo")

    assert first is old_client
    assert second is new_client
    assert mock_mint.call_count == 2
    assert mock_bearer.call_count == 2


async def test_different_installation_ids_get_different_cache_entries():
    cfg_a = _cfg(
        github_app_id="12345",
        github_app_private_key="pem-data",
        installation_id="999",
    )
    cfg_b = _cfg(
        github_app_id="12345",
        github_app_private_key="pem-data",
        installation_id="888",
    )
    fake_client_a = MagicMock(name="client-a")
    fake_client_b = MagicMock(name="client-b")

    with patch(
        "robotsix_central_deploy.lifecycle.github_app._mint_installation_token",
        side_effect=[
            SimpleNamespace(token="token-a"),
            SimpleNamespace(token="token-b"),
        ],
    ) as mock_mint:
        with patch(
            "robotsix_central_deploy.lifecycle.github_app._bearer_client",
            side_effect=[fake_client_a, fake_client_b],
        ):
            result_a = await get_github_client(cfg_a, "owner", "repo")
            result_b = await get_github_client(cfg_b, "owner", "repo")

    assert result_a is fake_client_a
    assert result_b is fake_client_b
    assert mock_mint.call_count == 2
