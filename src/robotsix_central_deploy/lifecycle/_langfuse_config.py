"""Canonical reader for a component's Langfuse credential block.

Every deployed component owns its Langfuse project credentials in its
own standardized config, under a single top-level ``langfuse`` block::

    "langfuse": {
      "host": "https://langfuse.robotsix.net",
      "projects": {
        "robotsix-chat": {
          "public_key": "pk-lf-...",
          "secret_key": "sk-lf-...",
          "project_id": "cmd..."
        },
        "robotsix-chat-cognee": {...}
      }
    }

The alias keying ``projects`` is the Langfuse **project name**, which the
component standard fixes as ``<repo>`` for a component's main LLM
function and ``<repo>-<function>`` for every additional
LLM-generating subsystem.  A component with two tracing functions
therefore declares two entries, never one shared project.

This module is the single place that knows the block's shape.  Both
consumers of component Langfuse credentials — the chat-agent trace proxy
(``routers/chat_langfuse.py``) and the fleet credential registry
(``routers/fleet_langfuse.py``) — read through it, so they cannot drift
apart again.

There is deliberately **no fallback** to deploy-plane environment
variables or to any pre-standard config shape.  Per the config-ownership
standard, first-party credentials live in the component's ``config.json``
and nowhere else; a component whose credentials have not been migrated
reports no projects, which is the intended, visible failure.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from fastapi import Request

    from ..registry.config_store import ComponentConfigStore
    from .backends.base import ExecutionBackend
    from .config import LangfuseProjectCreds, LifecycleConfig

from ._config_utils import read_component_config

logger = logging.getLogger(__name__)


class LangfuseProjectEntry(BaseModel):
    """One Langfuse project declared by a component."""

    alias: str = Field(description="Langfuse project name (the `projects` key)")
    public_key: str = Field(description="Langfuse public key for the project")
    secret_key: str = Field(description="Langfuse secret key for the project")
    project_id: str | None = Field(
        default=None,
        description="Langfuse project id, when the component records it",
    )


def extract_langfuse_block(
    config_dict: dict[str, object],
) -> tuple[str | None, list[LangfuseProjectEntry]]:
    """Return ``(host, projects)`` from a component's standardized config.

    *config_dict* is a component's current config values as stored by
    ``ConfigYamlStore``.  Only projects declaring both a public and a
    secret key are returned — a half-filled entry is treated as
    unconfigured rather than surfaced as a broken credential.

    Returns ``(None, [])`` when the component declares no ``langfuse``
    block at all.
    """
    langfuse = config_dict.get("langfuse")
    if not isinstance(langfuse, dict):
        return None, []

    host: str | None = None
    raw_host = langfuse.get("host")
    if isinstance(raw_host, str) and raw_host:
        host = raw_host

    projects: list[LangfuseProjectEntry] = []
    raw_projects = langfuse.get("projects")
    if isinstance(raw_projects, dict):
        for alias, creds in raw_projects.items():
            if not isinstance(creds, dict):
                continue
            public_key = creds.get("public_key") or ""
            secret_key = creds.get("secret_key") or ""
            if not public_key or not secret_key:
                continue
            raw_pid = creds.get("project_id")
            projects.append(
                LangfuseProjectEntry(
                    alias=str(alias),
                    public_key=str(public_key),
                    secret_key=str(secret_key),
                    project_id=str(raw_pid) if raw_pid else None,
                )
            )

    return host, projects


# ---------------------------------------------------------------------------
# Auto-discovery reconciliation
# ---------------------------------------------------------------------------


def _extract_langfuse_project_creds(
    config_dict: dict[str, object],
) -> dict[str, LangfuseProjectCreds]:
    """Extract Langfuse project credentials from a component's config.

    Reads the canonical ``langfuse.projects.<alias>`` block via
    :func:`extract_langfuse_block`, which is the single definition of
    that shape.  Returns an empty dict when the config has no Langfuse
    projects.
    """
    from .config import LangfuseProjectCreds

    _host, entries = extract_langfuse_block(config_dict)
    return {
        entry.alias: LangfuseProjectCreds(
            public_key=entry.public_key,
            secret_key=entry.secret_key,
        )
        for entry in entries
    }


def build_central_deploy_langfuse_config(
    config: LifecycleConfig,
    auto_langfuse: dict[str, LangfuseProjectCreds],
) -> dict[str, Any]:
    """Return central-deploy's own config view, computed rather than stored.

    central-deploy has no component config volume — it is the control plane,
    and its settings live in its own config file. The only "component config"
    it ever had was this Langfuse view, which is derived: the auto-discovered
    projects with the operator-configured ones layered on top, exactly as
    :func:`_build_project_creds` merges them for the proxy.

    It used to be persisted into ``ConfigYamlStore`` on every startup and
    every toggle so ``GET /services/central-deploy/config`` could read it
    back. That stored a copy of data the process already holds — and wrote
    the secret values into a file on disk to do it. Computing it on request
    keeps the same response with no copy and no persisted secrets.
    """
    projects: dict[str, dict[str, str]] = {}
    for alias, creds in auto_langfuse.items():
        projects[alias] = {
            "public_key": creds.public_key,
            "secret_key": creds.secret_key.get_secret_value(),
        }
    # Operator-configured wins on alias collision (pinned / rotated keys).
    for alias, creds in config.langfuse_projects.items():
        projects[alias] = {
            "public_key": creds.public_key,
            "secret_key": creds.secret_key.get_secret_value(),
        }
    return {"langfuse_projects": projects}


async def _reconcile_auto_langfuse_projects(
    component_config_store: ComponentConfigStore,
    backend: ExecutionBackend,
) -> dict[str, LangfuseProjectCreds]:
    """Scan every component with chat access enabled and extract Langfuse keys.

    Components are processed in registration order.  When two components
    declare the same project alias the first one wins (no overwrite).
    """
    result: dict[str, LangfuseProjectCreds] = {}
    for cfg in component_config_store.all():
        if not (cfg.allow_chat_access or cfg.chat_agent_mutatable):
            continue
        current = await read_component_config(backend, cfg)
        if not current:
            continue
        projects = _extract_langfuse_project_creds(current)
        for alias, creds in projects.items():
            if alias not in result:
                result[alias] = creds
    return result


async def reconcile_langfuse_after_toggle(
    component_config_store: ComponentConfigStore,
    request: Request,
) -> None:
    """Re-run Langfuse auto-discovery and update ``app.state``.

    Called after every ``allow_chat_access`` or ``chat_agent_mutatable``
    toggle (and at onboarding) so the chat-agent Langfuse proxy sees the
    latest project set.  Also refreshes central-deploy's own config
    current values so ``GET /services/central-deploy/config`` stays in
    sync.

    Failures are logged but never raised — auto-projects degrade
    gracefully to the last-known-good set.
    """
    try:
        auto_langfuse = await _reconcile_auto_langfuse_projects(
            component_config_store, request.app.state.backend
        )
        request.app.state.auto_langfuse_projects = auto_langfuse

        logger.debug(
            "Reconciled Langfuse auto-projects: %d project(s)",
            len(auto_langfuse),
        )
    except Exception:
        logger.warning(
            "Langfuse auto-discovery reconciliation failed",
            exc_info=True,
        )
