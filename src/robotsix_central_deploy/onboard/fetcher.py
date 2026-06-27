from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from robotsix_central_deploy.onboard.models import FetchError


def fetch_compose_bytes(git_url: str, timeout_sec: int = 30) -> bytes:
    """Clone a repo shallowly and return the raw bytes of its docker-compose.yml.

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

        return compose_path.read_bytes()
