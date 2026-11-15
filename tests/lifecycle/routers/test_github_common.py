"""Unit tests for the shared GitHub router plumbing in ``_github_common``."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from github import GithubException, UnknownObjectException
from pydantic import SecretStr

from robotsix_central_deploy.lifecycle import github_app
from robotsix_central_deploy.lifecycle.github_app import GitHubAppNotConfiguredError
from robotsix_central_deploy.lifecycle.routers import _github_common, chat_github
from robotsix_central_deploy.registry.chat_agent_audit_store import (
    ChatAgentAuditEntry,
)


def _config(*, app: bool = True, pat: bool = False) -> Any:
    """Return a LifecycleConfig stand-in carrying only the GitHub secrets."""
    return SimpleNamespace(
        github_app_id=SecretStr("123" if app else ""),
        github_app_private_key=SecretStr("pem" if app else ""),
        installation_id=SecretStr("456" if app else ""),
        github_repo_create_token=SecretStr("pat-token" if pat else ""),
    )


def _unknown() -> UnknownObjectException:
    return UnknownObjectException(404, {"message": "Not Found"}, None)


def _gh_error() -> GithubException:
    return GithubException(500, {"message": "boom"}, None)


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch) -> object:
    client = object()
    monkeypatch.setattr(
        chat_github, "get_github_client", AsyncMock(return_value=client)
    )
    return client


@pytest.fixture
def pat_client(monkeypatch: pytest.MonkeyPatch) -> object:
    client = object()
    monkeypatch.setattr(
        chat_github, "get_repo_create_client", MagicMock(return_value=client)
    )
    return client


# Tests for _get_client_or_503


async def test_get_client_or_503_returns_client(app_client: object) -> None:
    result = await _github_common._get_client_or_503(_config(), "o", "r")
    assert result is app_client


async def test_get_client_or_503_unknown_object_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chat_github, "get_github_client", AsyncMock(side_effect=_unknown())
    )
    with pytest.raises(HTTPException) as exc_info:
        await _github_common._get_client_or_503(_config(), "o", "r")
    assert exc_info.value.status_code == 404
    assert "o/r" in exc_info.value.detail


async def test_get_client_or_503_not_configured_is_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chat_github,
        "get_github_client",
        AsyncMock(side_effect=GitHubAppNotConfiguredError("app missing")),
    )
    with pytest.raises(HTTPException) as exc_info:
        await _github_common._get_client_or_503(_config(), "o", "r")
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "app missing"


# Tests for _get_client_or_503_with_pat_fallback


async def test_pat_fallback_prefers_app_client(
    app_client: object, pat_client: object
) -> None:
    result = await _github_common._get_client_or_503_with_pat_fallback(
        _config(app=True, pat=True), "o", "r"
    )
    assert result is app_client
    chat_github.get_repo_create_client.assert_not_called()  # type: ignore[attr-defined]


async def test_pat_fallback_uses_pat_when_app_unconfigured(
    app_client: object, pat_client: object
) -> None:
    config = _config(app=False, pat=True)
    result = await _github_common._get_client_or_503_with_pat_fallback(config, "o", "r")
    assert result is pat_client
    chat_github.get_github_client.assert_not_called()  # type: ignore[attr-defined]
    chat_github.get_repo_create_client.assert_called_once_with(config)  # type: ignore[attr-defined]


async def test_pat_fallback_neither_configured_is_503(
    app_client: object, pat_client: object
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _github_common._get_client_or_503_with_pat_fallback(
            _config(app=False, pat=False), "o", "r"
        )
    assert exc_info.value.status_code == 503
    chat_github.get_github_client.assert_not_called()  # type: ignore[attr-defined]
    chat_github.get_repo_create_client.assert_not_called()  # type: ignore[attr-defined]


async def test_pat_fallback_app_unknown_object_is_404(
    monkeypatch: pytest.MonkeyPatch, pat_client: object
) -> None:
    monkeypatch.setattr(
        chat_github, "get_github_client", AsyncMock(side_effect=_unknown())
    )
    with pytest.raises(HTTPException) as exc_info:
        await _github_common._get_client_or_503_with_pat_fallback(
            _config(app=True, pat=True), "o", "r"
        )
    assert exc_info.value.status_code == 404
    chat_github.get_repo_create_client.assert_not_called()  # type: ignore[attr-defined]


# Tests for _reraise_github_errors


def test_reraise_unknown_object_is_404() -> None:
    exc = _unknown()
    with pytest.raises(HTTPException) as exc_info:
        _github_common._reraise_github_errors(exc, "o", "r")
    assert exc_info.value.status_code == 404
    assert exc_info.value.__cause__ is exc


def test_reraise_github_exception_is_502() -> None:
    exc = _gh_error()
    with pytest.raises(HTTPException) as exc_info:
        _github_common._reraise_github_errors(exc, "o", "r")
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail.startswith("GitHub API error:")
    assert exc_info.value.__cause__ is exc


def test_reraise_unknown_exception_unchanged() -> None:
    exc = ValueError("nope")
    with pytest.raises(ValueError, match="nope") as exc_info:
        _github_common._reraise_github_errors(exc, "o", "r")
    assert exc_info.value is exc


# Tests for _require_installation_token


async def test_require_installation_token_unconfigured_is_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mint = MagicMock()
    monkeypatch.setattr(github_app, "get_installation_token_sync", mint)
    with pytest.raises(HTTPException) as exc_info:
        await _github_common._require_installation_token(_config(app=False), "o", "r")
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "GitHub App not configured"
    mint.assert_not_called()


async def test_require_installation_token_mints_via_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mint = MagicMock(return_value="ghs_token")
    monkeypatch.setattr(github_app, "get_installation_token_sync", mint)
    token = await _github_common._require_installation_token(_config(), "o", "r")
    assert token == "ghs_token"
    mint.assert_called_once_with("123", "pem", "456")


@pytest.mark.parametrize(
    ("error", "expected"),
    [(_unknown(), 404), (_gh_error(), 502)],
)
async def test_require_installation_token_maps_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: int
) -> None:
    monkeypatch.setattr(
        github_app, "get_installation_token_sync", MagicMock(side_effect=error)
    )
    with pytest.raises(HTTPException) as exc_info:
        await _github_common._require_installation_token(_config(), "o", "r")
    assert exc_info.value.status_code == expected


# Tests for _call_github_endpoint and _call_github_endpoint_with_pat_fallback

_CALLERS = [
    _github_common._call_github_endpoint,
    _github_common._call_github_endpoint_with_pat_fallback,
]


def _entry() -> ChatAgentAuditEntry:
    return ChatAgentAuditEntry(component="o/r", action="test")


@pytest.mark.parametrize("caller", _CALLERS)
async def test_call_dispatches_args_in_order_via_thread(
    monkeypatch: pytest.MonkeyPatch, app_client: object, caller: Any
) -> None:
    seen: list[Any] = []
    real_to_thread = asyncio.to_thread

    async def _spy(fn: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append((fn, args, kwargs))
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(_github_common.asyncio, "to_thread", _spy)
    sync_fn = MagicMock(return_value="result")

    result = await caller(_config(), "o", "r", sync_fn, "a1", 2)

    assert result == "result"
    assert seen == [(sync_fn, (app_client, "o", "r", "a1", 2), {})]
    sync_fn.assert_called_once_with(app_client, "o", "r", "a1", 2)


@pytest.mark.parametrize("caller", _CALLERS)
async def test_call_appends_audit_on_success(app_client: object, caller: Any) -> None:
    store = MagicMock()
    store.append = AsyncMock()
    entry = _entry()
    await caller(
        _config(),
        "o",
        "r",
        MagicMock(return_value=None),
        audit_store=store,
        audit_entry=entry,
    )
    store.append.assert_awaited_once_with(entry)


@pytest.mark.parametrize("caller", _CALLERS)
@pytest.mark.parametrize("with_store", [True, False])
async def test_call_skips_audit_when_store_or_entry_missing(
    app_client: object, caller: Any, with_store: bool
) -> None:
    store = MagicMock()
    store.append = AsyncMock()
    kwargs: dict[str, Any] = (
        {"audit_store": store} if with_store else {"audit_entry": _entry()}
    )
    await caller(_config(), "o", "r", MagicMock(return_value=None), **kwargs)
    store.append.assert_not_awaited()


@pytest.mark.parametrize("caller", _CALLERS)
@pytest.mark.parametrize(
    ("error", "expected"),
    [(_unknown(), 404), (_gh_error(), 502)],
)
async def test_call_maps_github_errors_and_skips_audit(
    app_client: object, caller: Any, error: Exception, expected: int
) -> None:
    store = MagicMock()
    store.append = AsyncMock()
    with pytest.raises(HTTPException) as exc_info:
        await caller(
            _config(),
            "o",
            "r",
            MagicMock(side_effect=error),
            audit_store=store,
            audit_entry=_entry(),
        )
    assert exc_info.value.status_code == expected
    store.append.assert_not_awaited()


@pytest.mark.parametrize("caller", _CALLERS)
async def test_call_reraises_unknown_exception(app_client: object, caller: Any) -> None:
    with pytest.raises(RuntimeError, match="kaboom"):
        await caller(_config(), "o", "r", MagicMock(side_effect=RuntimeError("kaboom")))


async def test_call_with_pat_fallback_uses_pat_client(
    app_client: object, pat_client: object
) -> None:
    sync_fn = MagicMock(return_value="ok")
    result = await _github_common._call_github_endpoint_with_pat_fallback(
        _config(app=False, pat=True), "o", "r", sync_fn, "x"
    )
    assert result == "ok"
    sync_fn.assert_called_once_with(pat_client, "o", "r", "x")


async def test_call_with_pat_fallback_neither_configured_is_503() -> None:
    sync_fn = MagicMock()
    with pytest.raises(HTTPException) as exc_info:
        await _github_common._call_github_endpoint_with_pat_fallback(
            _config(app=False, pat=False), "o", "r", sync_fn
        )
    assert exc_info.value.status_code == 503
    sync_fn.assert_not_called()
