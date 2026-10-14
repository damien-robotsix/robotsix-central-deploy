"""Tests for lifecycle.deps._github_token.github_token_for_repo."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, call

import pytest
from pydantic import SecretStr
from robotsix_github_auth import TokenMintError

from robotsix_central_deploy.lifecycle import github_app
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.deps import _github_token
from robotsix_central_deploy.lifecycle.deps._github_token import github_token_for_repo


def _cfg(installation_id: str = "456") -> LifecycleConfig:
    return LifecycleConfig(  # type: ignore[call-arg]
        github_app_id=SecretStr("app"),
        github_app_private_key=SecretStr("key"),
        installation_id=SecretStr(installation_id),
        store_backend="memory",
        execution_backend="noop",
    )


@pytest.fixture(autouse=True)
def _reset_pinned_flag(monkeypatch) -> None:
    """The pinned-id-rejected flag is process-global; reset it per test."""
    monkeypatch.setattr(_github_token, "_pinned_id_rejected", False)


async def test_empty_installation_id_mints_per_repo(monkeypatch) -> None:
    tok = MagicMock(return_value="ghs_tok")
    monkeypatch.setattr(github_app, "get_installation_token_sync", tok)

    out = await github_token_for_repo(
        "https://github.com/org/foo", _cfg(installation_id="")
    )

    assert out == "ghs_tok"
    tok.assert_called_once_with("app", "key", "", owner="org", repo="foo")


async def test_pinned_id_404_retries_per_repo(monkeypatch, caplog) -> None:
    def _get(app_id, key, inst_id, *, owner=None, repo=None):  # type: ignore[no-untyped-def]
        if inst_id == "456":
            raise TokenMintError("installation not found for id 456")
        return "ghs_resolved"

    tok = MagicMock(side_effect=_get)
    monkeypatch.setattr(github_app, "get_installation_token_sync", tok)

    with caplog.at_level(logging.WARNING):
        out = await github_token_for_repo("https://github.com/org/foo", _cfg("456"))

    assert out == "ghs_resolved"
    assert tok.call_args_list == [
        call("app", "key", "456"),
        call("app", "key", "", owner="org", repo="foo"),
    ]
    assert "org/foo" in caplog.text

    # The rejection is remembered: a later clone skips the pinned id.
    tok.reset_mock()
    out2 = await github_token_for_repo("https://github.com/org/bar", _cfg("456"))
    assert out2 == "ghs_resolved"
    assert tok.call_count == 1
    assert tok.call_args == call("app", "key", "", owner="org", repo="bar")


async def test_non_github_url_returns_none() -> None:
    assert await github_token_for_repo("https://example.com/x", _cfg()) is None


async def test_app_not_configured_returns_none(monkeypatch) -> None:
    cfg = LifecycleConfig(  # type: ignore[call-arg]
        github_app_id=SecretStr(""),
        github_app_private_key=SecretStr(""),
        installation_id=SecretStr(""),
        store_backend="memory",
        execution_backend="noop",
    )
    assert await github_token_for_repo("https://github.com/org/foo", cfg) is None


async def test_mint_failure_returns_none(monkeypatch) -> None:
    tok = MagicMock(side_effect=ValueError("bad key"))
    monkeypatch.setattr(github_app, "get_installation_token_sync", tok)

    out = await github_token_for_repo("https://github.com/org/foo", _cfg(""))

    assert out is None
