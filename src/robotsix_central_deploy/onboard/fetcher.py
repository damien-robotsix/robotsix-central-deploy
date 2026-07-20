from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from robotsix_central_deploy.onboard.models import FetchError

__all__ = ["FetchError", "RepoFiles", "fetch_compose_bytes", "fetch_repo_files"]

_LABEL_CONFIG_TEMPLATE = "robotsix.deploy.config-template"


@dataclass
class RepoFiles:
    compose_bytes: bytes
    config_json: bytes | None  # None if config/config.json absent in repo
    config_json_template: bytes | None = None  # fallback template bytes
    config_schema_json: bytes | None = None  # config/config.schema.json bytes, or None


def fetch_repo_files(
    git_url: str, timeout_sec: int = 30, github_token: str | None = None
) -> RepoFiles:
    """Clone a repo shallowly and return the bytes of deploy/docker-compose.yml
    and (if present) config/config.json.

    The repo root ``docker-compose.yml`` (dev compose) is **ignored**.
    Only ``deploy/docker-compose.yml`` is read — this is the deploy-
    contract-compliant compose.

    When ``config/config.json`` is absent (e.g. gitignored), two
    fallback strategies are tried in order to locate a config template:

    * **Strategy A** — adjacent convention:
      ``config/config.example.json`` alongside the config file.
    * **Strategy B** — label-declared path: the
      ``robotsix.deploy.config-template`` label on the first service
      in the compose file points to a relative path inside the repo.

    If *github_token* is provided and *git_url* points to GitHub, the
    URL is rewritten to use ``x-access-token`` authentication so the
    clone works for private repos the GitHub App is installed on.

    Raises:
        FetchError: if the URL is not https://, git clone fails, or
            ``deploy/docker-compose.yml`` is absent from the cloned repo.
    """
    if not git_url.startswith("https://"):
        raise FetchError("only https:// git URLs are supported")

    # If a GitHub token is provided, inject it into the URL so the
    # clone works for private repos.  git sanitises credentials in
    # error output (since 2.26), but we redact anyway as a defence-
    # in-depth measure.
    clone_url = git_url
    if github_token and urlparse(git_url).hostname in (
        "github.com",
        "www.github.com",
    ):
        clone_url = git_url.replace(
            "https://github.com/",
            f"https://x-access-token:{github_token}@github.com/",
            1,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, tmpdir],
            check=False,
            capture_output=True,
            timeout=timeout_sec,
        )
        if proc.returncode != 0:
            stderr_tail = proc.stderr.decode(errors="replace")[:500]
            if github_token:
                stderr_tail = stderr_tail.replace(github_token, "***")
            raise FetchError(f"git clone failed: {stderr_tail}")

        compose_path = Path(tmpdir) / "deploy" / "docker-compose.yml"
        if not compose_path.is_file():
            raise FetchError(
                "no deploy/docker-compose.yml found — see the deploy contract"
            )
        compose_bytes = compose_path.read_bytes()

        config_path = Path(tmpdir) / "config" / "config.json"
        config_json = config_path.read_bytes() if config_path.is_file() else None

        schema_path = Path(tmpdir) / "config" / "config.schema.json"
        config_schema_json = schema_path.read_bytes() if schema_path.is_file() else None

        config_json_template: bytes | None = None
        if config_json is None:
            # Strategy A — adjacent convention
            example_path = Path(tmpdir) / "config" / "config.example.json"
            if example_path.is_file():
                config_json_template = example_path.read_bytes()
            else:
                # Strategy B — label-declared path
                try:
                    doc = yaml.safe_load(compose_bytes)
                    if isinstance(doc, dict):
                        for svc in (doc.get("services") or {}).values():
                            if isinstance(svc, dict) and isinstance(
                                svc.get("labels"), dict
                            ):
                                tmpl = svc["labels"].get(_LABEL_CONFIG_TEMPLATE)
                                if isinstance(tmpl, str) and tmpl.strip():
                                    rel = tmpl.strip()
                                    candidate = (Path(tmpdir) / rel).resolve()
                                    if (
                                        candidate.is_relative_to(Path(tmpdir))
                                        and candidate.is_file()
                                    ):
                                        config_json_template = candidate.read_bytes()
                                break
                except Exception:  # noqa: S110
                    pass  # non-fatal; compose YAML errors surface later in parse_compose

        return RepoFiles(
            compose_bytes=compose_bytes,
            config_json=config_json,
            config_json_template=config_json_template,
            config_schema_json=config_schema_json,
        )


def fetch_compose_bytes(
    git_url: str, timeout_sec: int = 30, github_token: str | None = None
) -> bytes:
    """Clone a repo shallowly and return the raw bytes of its
    ``deploy/docker-compose.yml``.

    Convenience wrapper around ``fetch_repo_files`` for callers that only
    need the compose file.  The repo root ``docker-compose.yml`` (dev
    compose) is ignored — only ``deploy/docker-compose.yml`` is read.

    Raises:
        FetchError: if the URL is not https://, git clone fails, or
            ``deploy/docker-compose.yml`` is absent from the cloned repo.
    """
    return fetch_repo_files(git_url, timeout_sec, github_token).compose_bytes
