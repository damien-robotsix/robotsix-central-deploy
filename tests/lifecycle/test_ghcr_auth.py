"""Tests for the shared GHCR credential resolver."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from robotsix_central_deploy._ghcr_auth import GhcrCredentials, GhcrCredentialResolver


def _app_resolver(**kw: str) -> GhcrCredentialResolver:
    defaults = {
        "github_app_id": "123",
        "github_app_private_key": "key",
        "installation_id": "456",
    }
    return GhcrCredentialResolver(**{**defaults, **kw})


async def test_no_credentials_resolve_to_anonymous() -> None:
    assert await GhcrCredentialResolver().resolve() is None


async def test_pull_token_wins_over_the_github_app(monkeypatch) -> None:
    mint = MagicMock()
    monkeypatch.setattr(
        "robotsix_central_deploy._ghcr_auth.mint_installation_token", mint
    )
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", True)

    creds = await _app_resolver(ghcr_pull_token="ghp_static").resolve()

    assert creds == GhcrCredentials(username="robot", password="ghp_static")
    mint.assert_not_called()


async def test_partial_github_app_config_resolves_to_anonymous() -> None:
    assert await _app_resolver(installation_id="").resolve() is None
    assert await _app_resolver(github_app_id="   ").resolve() is None


async def test_github_app_token_is_minted(monkeypatch) -> None:
    mint = MagicMock(return_value=MagicMock(token="ghs_minted"))
    monkeypatch.setattr(
        "robotsix_central_deploy._ghcr_auth.mint_installation_token", mint
    )
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", True)

    creds = await _app_resolver().resolve()

    assert creds == GhcrCredentials(username="x-access-token", password="ghs_minted")
    mint.assert_called_once_with("123", "key", "456")


async def test_mint_failure_raises(monkeypatch) -> None:
    """A configured-but-broken App is a misconfiguration, not "no credential"."""
    monkeypatch.setattr(
        "robotsix_central_deploy._ghcr_auth.mint_installation_token",
        MagicMock(side_effect=ValueError("bad key")),
    )
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", True)

    with pytest.raises(RuntimeError, match="Failed to mint"):
        await _app_resolver().resolve()


async def test_missing_github_auth_package_degrades_to_anonymous(monkeypatch) -> None:
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", False)

    assert await _app_resolver().resolve() is None


async def test_pull_token_is_updatable_at_runtime() -> None:
    """``PUT /services/central-deploy/env`` sets this on the live resolver."""
    resolver = GhcrCredentialResolver()

    resolver.pull_token = "  ghp_new  "

    assert resolver.pull_token == "ghp_new"
    assert await resolver.resolve() == GhcrCredentials("robot", "ghp_new")
