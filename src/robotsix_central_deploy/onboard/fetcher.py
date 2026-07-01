from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from robotsix_central_deploy.onboard.models import FetchError

__all__ = ["FetchError", "RepoFiles", "fetch_compose_bytes", "fetch_repo_files"]

_LABEL_CONFIG_TEMPLATE = "robotsix.deploy.config-template"


@dataclass
class RepoFiles:
    compose_bytes: bytes
    config_yaml: bytes | None  # None if config/config.yaml absent in repo
    config_yaml_template: bytes | None = None  # fallback template bytes


def fetch_repo_files(git_url: str, timeout_sec: int = 30) -> RepoFiles:
    """Clone a repo shallowly and return the bytes of deploy/docker-compose.yml
    and (if present) config/config.yaml.

    The repo root ``docker-compose.yml`` (dev compose) is **ignored**.
    Only ``deploy/docker-compose.yml`` is read — this is the deploy-
    contract-compliant compose.

    When ``config/config.yaml`` is absent (e.g. gitignored), two
    fallback strategies are tried in order to locate a config template:

    * **Strategy A** — adjacent convention:
      ``config/config.example.yaml`` alongside the config file.
    * **Strategy B** — label-declared path: the
      ``robotsix.deploy.config-template`` label on the first service
      in the compose file points to a relative path inside the repo.

    Raises:
        FetchError: if the URL is not https://, git clone fails, or
            ``deploy/docker-compose.yml`` is absent from the cloned repo.
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

        compose_path = Path(tmpdir) / "deploy" / "docker-compose.yml"
        if not compose_path.is_file():
            raise FetchError(
                "no deploy/docker-compose.yml found — see the deploy contract"
            )
        compose_bytes = compose_path.read_bytes()

        config_path = Path(tmpdir) / "config" / "config.yaml"
        config_yaml = config_path.read_bytes() if config_path.is_file() else None

        config_yaml_template: bytes | None = None
        if config_yaml is None:
            # Strategy A — adjacent convention
            example_path = Path(tmpdir) / "config" / "config.example.yaml"
            if example_path.is_file():
                config_yaml_template = example_path.read_bytes()
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
                                        config_yaml_template = candidate.read_bytes()
                                break
                except Exception:  # noqa: S110
                    pass  # non-fatal; compose YAML errors surface later in parse_compose

        return RepoFiles(
            compose_bytes=compose_bytes,
            config_yaml=config_yaml,
            config_yaml_template=config_yaml_template,
        )


def fetch_compose_bytes(git_url: str, timeout_sec: int = 30) -> bytes:
    """Clone a repo shallowly and return the raw bytes of its
    ``deploy/docker-compose.yml``.

    Convenience wrapper around ``fetch_repo_files`` for callers that only
    need the compose file.  The repo root ``docker-compose.yml`` (dev
    compose) is ignored — only ``deploy/docker-compose.yml`` is read.

    Raises:
        FetchError: if the URL is not https://, git clone fails, or
            ``deploy/docker-compose.yml`` is absent from the cloned repo.
    """
    return fetch_repo_files(git_url, timeout_sec).compose_bytes
