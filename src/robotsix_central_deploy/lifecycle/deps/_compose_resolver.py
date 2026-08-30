"""Shared compose-resolution backbone extracted from onboard_preflight
and _resolve_deploy_contract to eliminate near-verbatim duplication.

The token-fetch + fetch-repo-files + parse-compose + schema-parse
+ _require_config_standard sequence was duplicated across two
long route-handler functions, creating drift risk (a bug fix in
the token-sanitisation / logging path needed to be applied in
two places).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from robotsix_central_deploy.onboard.fetcher import RepoFiles
    from robotsix_central_deploy.onboard.models import DerivedSpec

from ..config import LifecycleConfig
from .seed import _require_config_standard

logger = logging.getLogger(__name__)


def _parse_github_owner_repo(git_url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub HTTPS git URL, or ``None``."""
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", git_url)
    if m:
        return m.group(1), m.group(2)
    return None


async def _resolve_compose_backbone(
    git_url: str,
    name: str,
    lifecycle_config: LifecycleConfig,
    loop: asyncio.AbstractEventLoop,
) -> tuple[RepoFiles, DerivedSpec]:
    """Shared compose-resolution backbone.

    Fetches the repo, parses ``deploy/docker-compose.yml``, validates
    the config-standard contract, and returns the raw ``RepoFiles``
    together with the populated ``DerivedSpec``.

    Callers apply their own post-resolution steps (config-defaults,
    target-disk, volume-namespacing, port-collision preflight, etc.)
    on the returned objects.
    """
    from robotsix_central_deploy.onboard.fetcher import (
        FetchError,
        fetch_repo_files,
    )
    from robotsix_central_deploy.onboard.parser import (
        ParseError,
        parse_compose,
    )

    # --- Token fetch (GitHub App installation token for private repos) ---
    from ._github_token import github_token_for_repo

    github_token: str | None = await github_token_for_repo(
        git_url, lifecycle_config, loop
    )
    # --- Fetch repo files (clone is blocking → run in executor) ---
    try:
        repo_files = await loop.run_in_executor(
            None,
            fetch_repo_files,
            git_url,
            30,
            github_token,
        )
    except FetchError as e:
        raise HTTPException(status_code=422, detail={"error": str(e)})

    # --- Parse deploy/docker-compose.yml ---
    try:
        derived_spec = parse_compose(repo_files.compose_bytes, name, git_url)
    except ParseError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": "compose validation failed", "violations": e.violations},
        )

    # --- Parse config/config.schema.json if present ---
    if repo_files.config_schema_json is not None:
        try:
            derived_spec.config_schema = json.loads(repo_files.config_schema_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": f"config/config.schema.json is not valid JSON: {exc}"},
            )
    else:
        derived_spec.config_schema = None

    # --- Hard precondition: config contract must be satisfied ---
    _require_config_standard(derived_spec)

    return repo_files, derived_spec
