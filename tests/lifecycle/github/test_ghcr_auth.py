"""Tests for the shared GHCR credential resolver."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, call

import pytest
from robotsix_github_auth import TokenMintError

from robotsix_central_deploy._ghcr_auth import GhcrCredentialResolver, GhcrCredentials


def _app_resolver(**kw: str) -> GhcrCredentialResolver:
    defaults = {
        "github_app_id": "123",
        "github_app_private_key": "key",
        "installation_id": "456",
    }
    return GhcrCredentialResolver(**{**defaults, **kw})


async def test_no_credentials_resolve_to_anonymous() -> None:
    assert await GhcrCredentialResolver().resolve_all() == []


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

    candidates = await _app_resolver(ghcr_pull_token="ghp_static").resolve_all()

    assert candidates[0] == GhcrCredentials(username="robot", password="ghp_static")


async def test_partial_github_app_config_resolves_to_anonymous() -> None:
    assert await _app_resolver(installation_id="").resolve_all() == []
    assert await _app_resolver(github_app_id="   ").resolve_all() == []


async def test_github_app_token_is_minted(monkeypatch) -> None:
    mint = MagicMock(return_value=MagicMock(token="ghs_minted"))
    monkeypatch.setattr(
        "robotsix_central_deploy._ghcr_auth.mint_installation_token", mint
    )
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", True)

    candidates = await _app_resolver().resolve_all()

    assert candidates == [
        GhcrCredentials(username="x-access-token", password="ghs_minted")
    ]
    mint.assert_called_once_with("123", "key", "456")


async def test_mint_failure_raises(monkeypatch) -> None:
    """A configured-but-broken App is a misconfiguration, not "no credential"."""
    monkeypatch.setattr(
        "robotsix_central_deploy._ghcr_auth.mint_installation_token",
        MagicMock(side_effect=ValueError("bad key")),
    )
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", True)

    with pytest.raises(RuntimeError, match="Failed to mint"):
        await _app_resolver().resolve_all()


async def test_missing_github_auth_package_degrades_to_anonymous(monkeypatch) -> None:
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", False)

    assert await _app_resolver().resolve_all() == []


async def test_pull_token_is_updatable_at_runtime() -> None:
    """``PUT /services/central-deploy/env`` sets this on the live resolver."""
    resolver = GhcrCredentialResolver()

    resolver.pull_token = "  ghp_new  "

    assert resolver.pull_token == "ghp_new"
    assert await resolver.resolve_all() == [GhcrCredentials("robot", "ghp_new")]


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


# ---------------------------------------------------------------------------
# Per-repository installation resolution
# ---------------------------------------------------------------------------


async def test_empty_installation_id_resolves_per_repo(monkeypatch) -> None:
    """Empty installation_id + an image ref mints with owner/repo resolution."""
    mint = MagicMock(return_value=MagicMock(token="ghs_minted"))
    monkeypatch.setattr(
        "robotsix_central_deploy._ghcr_auth.mint_installation_token", mint
    )
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", True)

    candidates = await _app_resolver(installation_id="").resolve_all(
        "ghcr.io/damien-robotsix/foo:latest"
    )

    assert candidates == [
        GhcrCredentials(username="x-access-token", password="ghs_minted")
    ]
    mint.assert_called_once_with(
        "123", "key", None, owner="damien-robotsix", repo="foo"
    )


async def test_pinned_id_rejected_retries_per_repo(monkeypatch, caplog) -> None:
    """A pinned id GitHub rejects falls back to per-repo resolution, logged once."""
    mint = MagicMock(
        side_effect=[
            TokenMintError("installation 456 not found"),
            MagicMock(token="ghs_resolved"),
        ]
    )
    monkeypatch.setattr(
        "robotsix_central_deploy._ghcr_auth.mint_installation_token", mint
    )
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", True)

    resolver = _app_resolver(installation_id="456")
    with caplog.at_level(logging.WARNING):
        candidates = await resolver.resolve_all("ghcr.io/damien-robotsix/foo:latest")

    assert candidates == [
        GhcrCredentials(username="x-access-token", password="ghs_resolved")
    ]
    assert mint.call_args_list == [
        call("123", "key", "456"),
        call("123", "key", None, owner="damien-robotsix", repo="foo"),
    ]
    assert "456" in caplog.text
    assert "damien-robotsix/foo" in caplog.text


async def test_pinned_rejection_is_remembered(monkeypatch) -> None:
    """After one pinned-id rejection later mints skip the pinned id entirely."""

    def _mint(app_id, key, inst_id, *, owner=None, repo=None):  # type: ignore[no-untyped-def]
        if inst_id == "456":
            raise TokenMintError("installation 456 not found")
        return MagicMock(token="ghs_resolved")

    mint = MagicMock(side_effect=_mint)
    monkeypatch.setattr(
        "robotsix_central_deploy._ghcr_auth.mint_installation_token", mint
    )
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", True)

    resolver = _app_resolver(installation_id="456")
    await resolver.resolve_all("ghcr.io/org/foo")  # triggers rejection + retry
    mint.reset_mock()

    await resolver.resolve_all("ghcr.io/org/bar")

    assert mint.call_count == 1
    assert mint.call_args == call("123", "key", None, owner="org", repo="bar")


async def test_consecutive_mint_failures_tracked_and_reset(monkeypatch) -> None:
    """Three failures in a row report 3; a later success resets the counter."""
    mint = MagicMock(side_effect=ValueError("bad key"))
    monkeypatch.setattr(
        "robotsix_central_deploy._ghcr_auth.mint_installation_token", mint
    )
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", True)

    resolver = _app_resolver()
    for _ in range(3):
        with pytest.raises(RuntimeError, match="Failed to mint"):
            await resolver.resolve_all()
    assert resolver.consecutive_mint_failures == 3

    mint.side_effect = None
    mint.return_value = MagicMock(token="ghs_ok")
    candidates = await resolver.resolve_all()

    assert candidates == [GhcrCredentials("x-access-token", "ghs_ok")]
    assert resolver.consecutive_mint_failures == 0


async def test_empty_installation_id_without_image_ref_skips_app(
    monkeypatch, caplog
) -> None:
    """Empty id + no image ref skips the App credential with a WARNING, no raise."""
    mint = MagicMock(return_value=MagicMock(token="unused"))
    monkeypatch.setattr(
        "robotsix_central_deploy._ghcr_auth.mint_installation_token", mint
    )
    monkeypatch.setattr("robotsix_central_deploy._ghcr_auth._HAS_GITHUB_AUTH", True)

    with caplog.at_level(logging.WARNING):
        candidates = await _app_resolver(installation_id="").resolve_all()

    assert candidates == []
    mint.assert_not_called()
    assert "skipping the App credential" in caplog.text

    # Static PAT / anonymous behaviour is unchanged by the skip.
    pat_only = _app_resolver(installation_id="", ghcr_pull_token="ghp_x")
    assert await pat_only.resolve_all() == [GhcrCredentials("robot", "ghp_x")]
