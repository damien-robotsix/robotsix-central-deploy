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

from pydantic import BaseModel, Field


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
