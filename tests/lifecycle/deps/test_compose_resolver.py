"""Unit tests for lifecycle.deps._compose_resolver."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.deps._compose_resolver import (
    _resolve_compose_backbone,
)
from robotsix_central_deploy.lifecycle.deps._github_token import (
    parse_github_owner_repo as _parse_github_owner_repo,
)
from robotsix_central_deploy.onboard.fetcher import FetchError, RepoFiles
from robotsix_central_deploy.onboard.models import DerivedSpec
from robotsix_central_deploy.onboard.parser import ParseError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_derived_spec(**overrides) -> DerivedSpec:
    """Minimal valid DerivedSpec with sensible defaults for testing."""
    defaults: dict = {
        "name": "test-svc",
        "git_url": "https://github.com/org/test-svc",
        "image": "ghcr.io/org/test-svc:main",
        "ports": [],
        "volume_mounts": [],
        "env": {},
        "claude_mount": False,
        "host_docker_sock": False,
        "health_check": None,
        "command": None,
        "entrypoint": None,
        "tmpfs": [],
        "mem_limit": "512m",
        "container_name": "",
        "siblings": [],
        "config_schema": None,
        "config_example_values": None,
        "config_volume": None,
        "config_assist_command": None,
        "config_assist_seeds": [],
        "llmio_tier_level": None,
        "allow_chat_access": False,
        "user": None,
    }
    defaults.update(overrides)
    return DerivedSpec(**defaults)


def _make_repo_files(
    compose_bytes: bytes = b"version: '3'",
    config_schema_json: bytes | None = b'{"type":"object"}',
) -> RepoFiles:
    return RepoFiles(
        compose_bytes=compose_bytes,
        config_json=None,
        config_json_template=None,
        config_schema_json=config_schema_json,
    )


# ===================================================================
# _parse_github_owner_repo
# ===================================================================


class TestParseGithubOwnerRepo:
    """Tests for ``_parse_github_owner_repo`` — GitHub HTTPS URL parsing."""

    def test_valid_https_url_no_dot_git(self) -> None:
        """HTTPS URL without .git suffix returns (owner, repo)."""
        assert _parse_github_owner_repo("https://github.com/owner/repo") == (
            "owner",
            "repo",
        )

    def test_valid_https_url_with_dot_git(self) -> None:
        """HTTPS URL with .git suffix returns (owner, repo)."""
        assert _parse_github_owner_repo("https://github.com/owner/repo.git") == (
            "owner",
            "repo",
        )

    def test_valid_https_url_trailing_slash(self) -> None:
        """HTTPS URL with trailing slash returns (owner, repo)."""
        assert _parse_github_owner_repo("https://github.com/owner/repo/") == (
            "owner",
            "repo",
        )

    def test_valid_https_url_dot_git_trailing_slash(self) -> None:
        """HTTPS URL with .git and trailing slash returns (owner, repo)."""
        assert _parse_github_owner_repo("https://github.com/owner/repo.git/") == (
            "owner",
            "repo",
        )

    def test_non_github_domain_returns_none(self) -> None:
        """Non-GitHub HTTPS URL returns None."""
        assert _parse_github_owner_repo("https://gitlab.com/owner/repo") is None

    def test_ssh_url_returns_none(self) -> None:
        """SSH-style git URL returns None."""
        assert _parse_github_owner_repo("git@github.com:owner/repo.git") is None

    def test_single_path_segment_returns_none(self) -> None:
        """URL with only one path segment (no owner) returns None."""
        assert _parse_github_owner_repo("https://github.com/repo") is None

    def test_extra_path_segments_returns_none(self) -> None:
        """URL with extra path segments returns None."""
        assert (
            _parse_github_owner_repo("https://github.com/owner/repo/tree/main") is None
        )

    def test_different_domain_returns_none(self) -> None:
        """Similar-looking but different domain returns None."""
        assert _parse_github_owner_repo("https://notgithub.com/owner/repo") is None

    def test_owner_with_hyphen(self) -> None:
        """Owner name with hyphens is valid."""
        assert _parse_github_owner_repo("https://github.com/my-org/my-repo.git") == (
            "my-org",
            "my-repo",
        )

    def test_repo_name_with_dots(self) -> None:
        """Repo name containing dots (but not trailing .git) is valid."""
        assert _parse_github_owner_repo("https://github.com/owner/repo.name") == (
            "owner",
            "repo.name",
        )


# ===================================================================
# _resolve_compose_backbone
# ===================================================================


class TestResolveComposeBackbone:
    """Tests for ``_resolve_compose_backbone`` — the async compose-resolution
    pipeline covering happy path, every error branch, the token-fetch path,
    and token-fetch-failure fallback."""

    # -- fixtures -------------------------------------------------------

    @pytest.fixture
    def git_url(self) -> str:
        return "https://github.com/owner/test-repo"

    @pytest.fixture
    def name(self) -> str:
        return "test-repo"

    @pytest.fixture
    def lifecycle_config(self) -> LifecycleConfig:
        return LifecycleConfig()

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _cfg_with_github_app() -> LifecycleConfig:
        """LifecycleConfig with GitHub App credentials configured."""
        return LifecycleConfig(
            github_app_id=SecretStr("12345"),
            github_app_private_key=SecretStr("key-content"),
            installation_id=SecretStr("67890"),
        )

    @staticmethod
    def _cfg_empty_github_app() -> LifecycleConfig:
        """LifecycleConfig with empty-string GitHub App credentials."""
        return LifecycleConfig(
            github_app_id=SecretStr(""),
            github_app_private_key=SecretStr(""),
            installation_id=SecretStr(""),
        )

    # -- Happy path -----------------------------------------------------

    async def test_happy_path_returns_repo_files_and_spec(
        self,
        git_url: str,
        name: str,
        lifecycle_config: LifecycleConfig,
    ) -> None:
        """Happy path: fetch → parse → validate returns RepoFiles and DerivedSpec."""
        loop = asyncio.get_running_loop()
        spec = _make_derived_spec(
            config_schema={"type": "object"},
            config_volume="config",
        )
        repo_files = _make_repo_files()

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=repo_files,
            ) as mock_fetch,
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ) as mock_parse,
            patch(
                "robotsix_central_deploy.lifecycle.deps._compose_resolver._require_config_standard",
            ) as mock_require,
        ):
            result_repo_files, result_spec = await _resolve_compose_backbone(
                git_url, name, lifecycle_config, loop
            )

        assert result_repo_files is repo_files
        assert result_spec is spec
        mock_fetch.assert_called_once_with(git_url, 30, None)
        mock_parse.assert_called_once_with(repo_files.compose_bytes, name, git_url)
        mock_require.assert_called_once_with(spec)

    # -- FetchError → 422 -----------------------------------------------

    async def test_fetch_error_raises_422(
        self,
        git_url: str,
        name: str,
        lifecycle_config: LifecycleConfig,
    ) -> None:
        """FetchError is converted to HTTP 422 with error detail."""
        loop = asyncio.get_running_loop()

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                side_effect=FetchError("clone failed"),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await _resolve_compose_backbone(git_url, name, lifecycle_config, loop)

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail == {"error": "clone failed"}

    # -- ParseError → 422 -----------------------------------------------

    async def test_parse_error_raises_422(
        self,
        git_url: str,
        name: str,
        lifecycle_config: LifecycleConfig,
    ) -> None:
        """ParseError is converted to HTTP 422 with violations detail."""
        loop = asyncio.get_running_loop()
        repo_files = _make_repo_files()

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=repo_files,
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                side_effect=ParseError(["missing port", "bad volume"]),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await _resolve_compose_backbone(git_url, name, lifecycle_config, loop)

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail == {
            "error": "compose validation failed",
            "violations": ["missing port", "bad volume"],
        }

    # -- JSONDecodeError → 422 ------------------------------------------

    async def test_json_decode_error_raises_422(
        self,
        git_url: str,
        name: str,
        lifecycle_config: LifecycleConfig,
    ) -> None:
        """Invalid config.schema.json raises HTTP 422."""
        loop = asyncio.get_running_loop()
        spec = _make_derived_spec()
        repo_files = _make_repo_files(config_schema_json=b"not json")

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=repo_files,
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
            patch(
                "robotsix_central_deploy.lifecycle.deps._compose_resolver._require_config_standard",
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await _resolve_compose_backbone(git_url, name, lifecycle_config, loop)

        assert exc_info.value.status_code == 422
        assert "not valid JSON" in exc_info.value.detail["error"]

    # -- No config schema → sets to None --------------------------------

    async def test_no_config_schema_sets_none(
        self,
        git_url: str,
        name: str,
        lifecycle_config: LifecycleConfig,
    ) -> None:
        """When config_schema_json is None, spec.config_schema is set to None."""
        loop = asyncio.get_running_loop()
        spec = _make_derived_spec(
            config_schema={"type": "object"},
            config_volume="config",
        )
        repo_files = _make_repo_files(config_schema_json=None)

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=repo_files,
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
            patch(
                "robotsix_central_deploy.lifecycle.deps._compose_resolver._require_config_standard",
            ),
        ):
            _result_repo_files, result_spec = await _resolve_compose_backbone(
                git_url, name, lifecycle_config, loop
            )

        assert result_spec.config_schema is None

    # -- Config-standard violation → 422 --------------------------------

    async def test_config_standard_violation_raises_422(
        self,
        git_url: str,
        name: str,
        lifecycle_config: LifecycleConfig,
    ) -> None:
        """_require_config_standard raises HTTP 422 on contract violation."""
        loop = asyncio.get_running_loop()
        spec = _make_derived_spec()
        repo_files = _make_repo_files()

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=repo_files,
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
            patch(
                "robotsix_central_deploy.lifecycle.deps._compose_resolver._require_config_standard",
                side_effect=HTTPException(
                    status_code=422,
                    detail={"error": "config standard not met"},
                ),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await _resolve_compose_backbone(git_url, name, lifecycle_config, loop)

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail == {"error": "config standard not met"}

    # -- Token fetch success --------------------------------------------

    async def test_github_app_token_fetched_and_passed(
        self,
        git_url: str,
        name: str,
    ) -> None:
        """When GitHub App is configured, token is fetched and passed to fetch."""
        loop = asyncio.get_running_loop()
        cfg = self._cfg_with_github_app()
        spec = _make_derived_spec(
            config_schema={"type": "object"},
            config_volume="config",
        )
        repo_files = _make_repo_files()

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=repo_files,
            ) as mock_fetch,
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
            patch(
                "robotsix_central_deploy.lifecycle.deps._compose_resolver._require_config_standard",
            ),
            patch(
                "robotsix_central_deploy.lifecycle.github_app.get_installation_token_sync",
                return_value="ghs_test-token",
            ) as mock_token,
        ):
            await _resolve_compose_backbone(git_url, name, cfg, loop)

        mock_token.assert_called_once_with("12345", "key-content", "67890")
        mock_fetch.assert_called_once_with(git_url, 30, "ghs_test-token")

    # -- Token fetch failure → warning + fallback -----------------------

    async def test_token_fetch_failure_warns_and_falls_back(
        self,
        git_url: str,
        name: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Token-fetch failure logs warning and proceeds unauthenticated."""
        loop = asyncio.get_running_loop()
        cfg = self._cfg_with_github_app()
        spec = _make_derived_spec(
            config_schema={"type": "object"},
            config_volume="config",
        )
        repo_files = _make_repo_files()

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=repo_files,
            ) as mock_fetch,
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
            patch(
                "robotsix_central_deploy.lifecycle.deps._compose_resolver._require_config_standard",
            ),
            patch(
                "robotsix_central_deploy.lifecycle.github_app.get_installation_token_sync",
                side_effect=RuntimeError("GitHub API unreachable"),
            ),
        ):
            await _resolve_compose_backbone(git_url, name, cfg, loop)

        # Falls back to unauthenticated clone
        mock_fetch.assert_called_once_with(git_url, 30, None)

        # Warning logged with sanitised owner/repo
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) >= 1
        assert "Cannot get GitHub App installation token" in warnings[0].message
        assert "owner" in warnings[0].message
        assert "test-repo" in warnings[0].message

    # -- Non-GitHub URL → skips token fetch -----------------------------

    async def test_non_github_url_skips_token_fetch(
        self,
        name: str,
    ) -> None:
        """Non-GitHub URL skips token fetch entirely even when App configured."""
        loop = asyncio.get_running_loop()
        cfg = self._cfg_with_github_app()
        non_github_url = "https://gitlab.com/owner/repo"
        spec = _make_derived_spec(
            config_schema={"type": "object"},
            config_volume="config",
        )
        repo_files = _make_repo_files()

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=repo_files,
            ) as mock_fetch,
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
            patch(
                "robotsix_central_deploy.lifecycle.deps._compose_resolver._require_config_standard",
            ),
            patch(
                "robotsix_central_deploy.lifecycle.github_app.get_installation_token_sync",
            ) as mock_token,
        ):
            await _resolve_compose_backbone(non_github_url, name, cfg, loop)

        mock_token.assert_not_called()
        mock_fetch.assert_called_once_with(non_github_url, 30, None)

    # -- Empty GitHub App fields → skips token fetch --------------------

    async def test_empty_app_fields_skip_token_fetch(
        self,
        git_url: str,
        name: str,
    ) -> None:
        """When GitHub App fields are empty strings, token fetch is skipped."""
        loop = asyncio.get_running_loop()
        cfg = self._cfg_empty_github_app()
        spec = _make_derived_spec(
            config_schema={"type": "object"},
            config_volume="config",
        )
        repo_files = _make_repo_files()

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=repo_files,
            ) as mock_fetch,
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
            patch(
                "robotsix_central_deploy.lifecycle.deps._compose_resolver._require_config_standard",
            ),
            patch(
                "robotsix_central_deploy.lifecycle.github_app.get_installation_token_sync",
            ) as mock_token,
        ):
            await _resolve_compose_backbone(git_url, name, cfg, loop)

        mock_token.assert_not_called()
        mock_fetch.assert_called_once_with(git_url, 30, None)
