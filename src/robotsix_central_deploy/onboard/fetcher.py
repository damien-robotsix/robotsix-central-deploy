from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from robotsix_central_deploy.onboard.models import FetchError


@dataclass
class RepoFiles:
    compose_bytes: bytes
    config_yaml: bytes | None  # None if config/config.yaml absent in repo


def fetch_repo_files(git_url: str, timeout_sec: int = 30) -> RepoFiles:
    """Clone a repo shallowly and return the bytes of docker-compose.yml
    and (if present) config/config.yaml.

    Raises:
        FetchError: if the URL is not https://, git clone fails, or
            docker-compose.yml is absent from the repo root.
    """
    if not git_url.startswith("https://"):
        raise FetchError("only https:// git URLs are supported")

    with tempfile.TemporaryDirectory() as tmpdir:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", git_url, tmpdir],
            check=False,
            capture_output=True,
            timeout=timeout_sec,
        )
        if proc.returncode != 0:
            stderr_tail = proc.stderr.decode(errors="replace")[:500]
            raise FetchError(f"git clone failed: {stderr_tail}")

        compose_path = Path(tmpdir) / "docker-compose.yml"
        if not compose_path.is_file():
            raise FetchError("docker-compose.yml not found in repo root")

        config_path = Path(tmpdir) / "config" / "config.yaml"
        config_yaml = config_path.read_bytes() if config_path.is_file() else None

        return RepoFiles(
            compose_bytes=compose_path.read_bytes(),
            config_yaml=config_yaml,
        )


def fetch_compose_bytes(git_url: str, timeout_sec: int = 30) -> bytes:
    """Clone a repo shallowly and return the raw bytes of its docker-compose.yml.

    Convenience wrapper around ``fetch_repo_files`` for callers that only
    need the compose file.

    Raises:
        FetchError: if the URL is not https://, git clone fails, or
            docker-compose.yml is absent from the repo root.
    """
    return fetch_repo_files(git_url, timeout_sec).compose_bytes
