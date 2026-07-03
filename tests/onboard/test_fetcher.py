"""Dedicated tests for the onboard fetcher module.

Exercises ``fetch_repo_files`` and ``fetch_compose_bytes`` against real
local git repos (via ``git init`` + patched ``subprocess.run``) so the
actual git-clone-and-read logic is verified.  Network-only scenarios
(timeout, clone-to-remote-failure) use subprocess mocking.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from robotsix_central_deploy.onboard.fetcher import (
    FetchError,
    RepoFiles,
    fetch_compose_bytes,
    fetch_repo_files,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _init_local_git_repo(
    root: Path,
    *,
    deploy_compose: bytes | None = None,
    config_yaml: bytes | None = None,
    extra_file: str | None = None,
) -> None:
    """Create a local git repo at *root* with at least one commit.

    By default only a root ``README.md`` is committed so the repo has at
    least one commit (required for ``git clone --depth 1`` to succeed).
    Callers can pass optional payload files.
    """
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(root)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )

    # Seed file so the repo has at least one commit
    (root / "README.md").write_text("# test repo\n")

    if deploy_compose is not None:
        deploy_dir = root / "deploy"
        deploy_dir.mkdir()
        (deploy_dir / "docker-compose.yml").write_bytes(deploy_compose)

    if config_yaml is not None:
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "config.json").write_bytes(config_yaml)

    if extra_file is not None:
        (root / extra_file).write_text("unrelated\n")

    subprocess.run(
        ["git", "-C", str(root), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )


# Saved before any mock.patch("subprocess.run") so the side-effect can
# delegate to the real implementation without recursing into the mock.
_real_subprocess_run = subprocess.run


def _real_git_clone_side_effect(source_repo: Path):
    """Return a side_effect for ``subprocess.run`` that runs real
    ``git clone --depth 1`` from *source_repo* into whatever tmpdir
    the call-site requested.

    The returned callable ignores the *git_url* and *timeout* args of
    the real ``subprocess.run`` call — it only checks that the first
    argument is ``["git", "clone", "--depth", "1", ...]`` and redirects
    the clone source to *source_repo*.
    """

    def _side_effect(args, **kwargs):
        if args[:4] == ["git", "clone", "--depth", "1"]:
            # args[4] is the original URL, args[5] is the dest tmpdir
            dest = args[5]
            cp = _real_subprocess_run(
                ["git", "clone", "--depth", "1", str(source_repo), dest],
                check=False,
                capture_output=True,
                timeout=kwargs.get("timeout", 30),
            )
            return cp
        # Unexpected command — let it fail naturally
        return _real_subprocess_run(args, **kwargs)

    return _side_effect


# ---------------------------------------------------------------------------
# fetch_repo_files
# ---------------------------------------------------------------------------


class TestFetchRepoFiles:
    """Tests for ``fetch_repo_files`` using real local git repos."""

    def test_successful_clone_with_config(self, tmp_path: Path):
        """Shallow clone of a local repo that has both deploy/compose
        and config/config.json — both are returned."""
        source_repo = tmp_path / "source"
        compose_bytes = b"# central-deploy-contract-version: 1\nservices:\n  svc:\n    image: img:latest\n"
        config_bytes = b'{"host": "localhost", "port": 8080}'
        _init_local_git_repo(
            source_repo,
            deploy_compose=compose_bytes,
            config_yaml=config_bytes,
        )

        with mock.patch(
            "subprocess.run", side_effect=_real_git_clone_side_effect(source_repo)
        ):
            result = fetch_repo_files("https://example.com/repo.git")

        assert isinstance(result, RepoFiles)
        assert result.compose_bytes == compose_bytes
        assert result.config_yaml == config_bytes

    def test_successful_clone_without_config(self, tmp_path: Path):
        """When config/config.json is absent, ``config_yaml`` is ``None``."""
        source_repo = tmp_path / "source"
        compose_bytes = b"# central-deploy-contract-version: 1\nservices:\n  svc:\n    image: img:latest\n"
        _init_local_git_repo(source_repo, deploy_compose=compose_bytes)

        with mock.patch(
            "subprocess.run", side_effect=_real_git_clone_side_effect(source_repo)
        ):
            result = fetch_repo_files("https://example.com/repo.git")

        assert result.compose_bytes == compose_bytes
        assert result.config_yaml is None

    def test_missing_deploy_compose_raises_fetch_error(self, tmp_path: Path):
        """Repo without ``deploy/docker-compose.yml`` raises ``FetchError``."""
        source_repo = tmp_path / "source"
        _init_local_git_repo(source_repo)

        with mock.patch(
            "subprocess.run", side_effect=_real_git_clone_side_effect(source_repo)
        ):
            with pytest.raises(FetchError, match="no deploy/docker-compose.yml found"):
                fetch_repo_files("https://example.com/repo.git")

    def test_root_compose_is_ignored(self, tmp_path: Path):
        """Root ``docker-compose.yml`` is not the deploy contract — still raises."""
        source_repo = tmp_path / "source"
        _init_local_git_repo(source_repo, extra_file="docker-compose.yml")

        with mock.patch(
            "subprocess.run", side_effect=_real_git_clone_side_effect(source_repo)
        ):
            with pytest.raises(FetchError, match="no deploy/docker-compose.yml found"):
                fetch_repo_files("https://example.com/repo.git")

    def test_non_https_url_raises_fetch_error(self):
        """Non-HTTPS URLs are rejected before any subprocess call."""
        with pytest.raises(FetchError, match="only https://"):
            fetch_repo_files("git@github.com:user/repo.git")

        with pytest.raises(FetchError, match="only https://"):
            fetch_repo_files("http://example.com/repo.git")

        with pytest.raises(FetchError, match="only https://"):
            fetch_repo_files("file:///etc/passwd")

    def test_git_clone_failure_fetch_error(self):
        """When ``git clone`` exits non-zero, ``FetchError`` carries the stderr tail."""
        with mock.patch("subprocess.run") as m_run:
            m_run.return_value = mock.Mock(
                returncode=128,
                stderr=b"fatal: repository 'https://example.com/bogus.git' not found\n",
            )
            with pytest.raises(FetchError, match="git clone failed"):
                fetch_repo_files("https://example.com/bogus.git")

    def test_timeout_raises_subprocess_timeout(self):
        """A tiny timeout triggers ``subprocess.TimeoutExpired``, propagated upward."""
        with mock.patch("subprocess.run") as m_run:
            m_run.side_effect = subprocess.TimeoutExpired(
                cmd=[
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "https://example.com/repo.git",
                    "/tmp/x",
                ],
                timeout=0.001,
            )
            with pytest.raises(subprocess.TimeoutExpired):
                fetch_repo_files("https://example.com/repo.git", timeout_sec=0.001)


# ---------------------------------------------------------------------------
# fetch_compose_bytes
# ---------------------------------------------------------------------------


class TestFetchComposeBytes:
    """Tests for the ``fetch_compose_bytes`` convenience wrapper."""

    def test_returns_compose_bytes(self, tmp_path: Path):
        """Convenience wrapper returns only ``compose_bytes``."""
        source_repo = tmp_path / "source"
        compose_bytes = b"# central-deploy-contract-version: 1\nservices:\n  svc:\n    image: img:latest\n"
        _init_local_git_repo(source_repo, deploy_compose=compose_bytes)

        with mock.patch(
            "subprocess.run", side_effect=_real_git_clone_side_effect(source_repo)
        ):
            result = fetch_compose_bytes("https://example.com/repo.git")

        assert result == compose_bytes

    def test_non_https_url_raises_fetch_error(self):
        """Non-HTTPS rejection identical to ``fetch_repo_files``."""
        with pytest.raises(FetchError, match="only https://"):
            fetch_compose_bytes("git@github.com:user/repo.git")

    def test_git_clone_failure_raises_fetch_error(self):
        """Clone failure propagates as ``FetchError``."""
        with mock.patch("subprocess.run") as m_run:
            m_run.return_value = mock.Mock(
                returncode=128,
                stderr=b"fatal: repository 'https://example.com/bogus.git' not found\n",
            )
            with pytest.raises(FetchError, match="git clone failed"):
                fetch_compose_bytes("https://example.com/bogus.git")

    def test_missing_deploy_compose_raises_fetch_error(self, tmp_path: Path):
        """Empty repo → ``FetchError`` from the wrapper too."""
        source_repo = tmp_path / "source"
        _init_local_git_repo(source_repo)

        with mock.patch(
            "subprocess.run", side_effect=_real_git_clone_side_effect(source_repo)
        ):
            with pytest.raises(FetchError, match="no deploy/docker-compose.yml found"):
                fetch_compose_bytes("https://example.com/repo.git")


# ---------------------------------------------------------------------------
# helpers for extra files beyond _init_local_git_repo
# ---------------------------------------------------------------------------


def _add_and_commit(repo: Path, files: dict[str, bytes]) -> None:
    """Write files into *repo* and commit them (existing repo)."""
    for rel, content in files.items():
        full = repo / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "add files"],
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# fetch_repo_files — config template fallback
# ---------------------------------------------------------------------------


class TestFetchRepoFilesTemplateFallback:
    """Tests for the config_yaml_template fallback in ``fetch_repo_files``."""

    def test_adjacent_example_yaml_used_when_config_absent(self, tmp_path: Path):
        """When config/config.json is absent but config.example.json exists,
        config_yaml_template is populated from the example file."""
        source_repo = tmp_path / "source"
        compose_bytes = (
            b"# central-deploy-contract-version: 1\n"
            b"services:\n"
            b"  svc:\n"
            b"    image: img:latest\n"
        )
        example_bytes = b'{"host": "example.localhost", "port": 9090}'
        _init_local_git_repo(source_repo, deploy_compose=compose_bytes)
        _add_and_commit(
            source_repo,
            {"config/config.example.json": example_bytes},
        )

        with mock.patch(
            "subprocess.run",
            side_effect=_real_git_clone_side_effect(source_repo),
        ):
            result = fetch_repo_files("https://example.com/repo.git")

        assert result.config_yaml is None
        assert result.config_yaml_template == example_bytes
        parsed = json.loads(result.config_yaml_template)
        assert parsed == {"host": "example.localhost", "port": 9090}

    def test_label_template_path_used_when_adjacent_absent(self, tmp_path: Path):
        """When neither config/config.json nor config.example.json exist,
        but the compose carries robotsix.deploy.config-template pointing
        to a committed file, config_yaml_template is populated."""
        source_repo = tmp_path / "source"
        compose_bytes = (
            b"# central-deploy-contract-version: 1\n"
            b"services:\n"
            b"  svc:\n"
            b"    image: img:latest\n"
            b"    labels:\n"
            b'      robotsix.deploy.config-template: "templates/schema.yaml"\n'
        )
        template_bytes = b"host: template.localhost\nport: 7070\n"
        _init_local_git_repo(source_repo, deploy_compose=compose_bytes)
        _add_and_commit(
            source_repo,
            {"templates/schema.yaml": template_bytes},
        )

        with mock.patch(
            "subprocess.run",
            side_effect=_real_git_clone_side_effect(source_repo),
        ):
            result = fetch_repo_files("https://example.com/repo.git")

        assert result.config_yaml is None
        assert result.config_yaml_template == template_bytes

    def test_config_yaml_takes_precedence_over_template(self, tmp_path: Path):
        """When config/config.json IS present, config_yaml_template is
        left as None — the fallback block never runs."""
        source_repo = tmp_path / "source"
        compose_bytes = (
            b"# central-deploy-contract-version: 1\n"
            b"services:\n"
            b"  svc:\n"
            b"    image: img:latest\n"
        )
        config_bytes = b'{"host": "real.localhost"}'
        example_bytes = b'{"host": "example.localhost"}'
        _init_local_git_repo(
            source_repo,
            deploy_compose=compose_bytes,
            config_yaml=config_bytes,
        )
        _add_and_commit(
            source_repo,
            {"config/config.example.json": example_bytes},
        )

        with mock.patch(
            "subprocess.run",
            side_effect=_real_git_clone_side_effect(source_repo),
        ):
            result = fetch_repo_files("https://example.com/repo.git")

        assert result.config_yaml == config_bytes
        assert result.config_yaml_template is None

    def test_no_fallback_when_neither_present(self, tmp_path: Path):
        """When no config files at all exist, both fields are None."""
        source_repo = tmp_path / "source"
        compose_bytes = (
            b"# central-deploy-contract-version: 1\n"
            b"services:\n"
            b"  svc:\n"
            b"    image: img:latest\n"
        )
        _init_local_git_repo(source_repo, deploy_compose=compose_bytes)

        with mock.patch(
            "subprocess.run",
            side_effect=_real_git_clone_side_effect(source_repo),
        ):
            result = fetch_repo_files("https://example.com/repo.git")

        assert result.config_yaml is None
        assert result.config_yaml_template is None

    def test_label_template_traversal_rejected(self, tmp_path: Path):
        """A label path with .. traversal is silently ignored."""
        source_repo = tmp_path / "source"
        compose_bytes = (
            b"# central-deploy-contract-version: 1\n"
            b"services:\n"
            b"  svc:\n"
            b"    image: img:latest\n"
            b"    labels:\n"
            b'      robotsix.deploy.config-template: "../../etc/passwd"\n'
        )
        _init_local_git_repo(source_repo, deploy_compose=compose_bytes)

        with mock.patch(
            "subprocess.run",
            side_effect=_real_git_clone_side_effect(source_repo),
        ):
            result = fetch_repo_files("https://example.com/repo.git")

        assert result.config_yaml is None
        assert result.config_yaml_template is None
