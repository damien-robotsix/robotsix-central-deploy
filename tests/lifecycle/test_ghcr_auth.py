"""Tests for the shared GHCR credential resolver."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from robotsix_central_deploy._ghcr_auth import GhcrCredentialResolver, GhcrCredentials


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
    """The PAT is preferred — but it no longer *excludes* the App.

    This test used to assert ``mint.assert_not_called()``.  That exclusivity
    is exactly what let a revoked PAT block every pull while a healthy App
    credential sat unused behind it, so the App is now resolved as a fallback
    (one cached mint per hour) even when a PAT is present.
    """
    mint = MagicMock(return_value=MagicMock(token="ghs_minted"))
    monkeypatch.setattr(
        "robotsix_central_deploy._ghcr_auth.mint_installation_token", mint
    )
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", True)

    creds = await _app_resolver(ghcr_pull_token="ghp_static").resolve()

    assert creds == GhcrCredentials(username="robot", password="ghp_static")


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


async def test_resolve_all_keeps_the_app_behind_the_pull_token(monkeypatch) -> None:
    """A static PAT is a *preference*, not an exclusive choice.

    A revoked ``ghp_`` token in system_settings.json shadowed a healthy App
    installation for 15 days because resolution stopped at the first entry.
    """
    monkeypatch.setattr(
        "robotsix_central_deploy._ghcr_auth.mint_installation_token",
        MagicMock(return_value=MagicMock(token="ghs_minted")),
    )
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", True)

    candidates = await _app_resolver(ghcr_pull_token="ghp_revoked").resolve_all()

    assert candidates == [
        GhcrCredentials("robot", "ghp_revoked"),
        GhcrCredentials("x-access-token", "ghs_minted"),
    ], "the App credential must remain reachable behind a stale PAT"


async def test_resolve_all_degrades_when_minting_fails_but_a_pat_exists(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "robotsix_central_deploy._ghcr_auth.mint_installation_token",
        MagicMock(side_effect=ValueError("bad key")),
    )
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", True)

    candidates = await _app_resolver(ghcr_pull_token="ghp_only").resolve_all()

    assert candidates == [GhcrCredentials("robot", "ghp_only")]


async def test_resolve_all_still_raises_when_the_app_is_the_only_credential(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "robotsix_central_deploy._ghcr_auth.mint_installation_token",
        MagicMock(side_effect=ValueError("bad key")),
    )
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", True)

    with pytest.raises(RuntimeError, match="Failed to mint"):
        await _app_resolver().resolve_all()


async def test_resolve_all_is_empty_when_nothing_is_configured() -> None:
    assert await GhcrCredentialResolver().resolve_all() == []
