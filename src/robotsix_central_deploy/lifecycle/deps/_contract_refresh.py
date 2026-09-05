"""Shared contract-refresh backbone.

Re-fetches a component's ``deploy/docker-compose.yml`` (and
``config/config.schema.json``) from its repo HEAD, rebuilds the stored
``ComponentConfig`` from the parsed contract while preserving operator-set
fields and host-port assignments, persists it, and re-registers it in the
in-memory registry.

Extracted from the ``POST /services/{name}/refresh-contract`` handler so the
deploy/update path can re-apply compose-only changes (e.g. added or edited
``robotsix.deploy.*`` proxy labels) even when the image digest is unchanged —
otherwise a label change that lives only in the compose file never reaches the
recreated container and the proxy route table never gains the route.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from ...onboard.port_utils import (
    collect_occupied_host_ports,
    preserve_host_port_assignments,
)
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.loader import ComponentRegistry
from ...registry.models import ComponentConfig
from .._config_utils import _sanitize_log
from ..config import LifecycleConfig
from .seed import (
    _build_component_config_from_spec,
    _fetch_component_repo_files,
    _namespace_spec_volumes,
)

logger = logging.getLogger(__name__)


# Contract-derived fields compared when diffing the refreshed config against
# the stored one.  Operator-set fields (repo_id, auto_update_enabled, etc.)
# are deliberately excluded — the manifest cannot know about them.
_CONTRACT_FIELDS = (
    "image",
    "container_name",
    "ports",
    "mounts",
    "env",
    "health_check",
    "command",
    "entrypoint",
    "tmpfs",
    "mem_limit",
    "memswap_limit",
    "claude_mount",
    "claude_mount_path",
    "host_docker_sock",
    "named_volumes",
    "siblings",
    "config_volume",
    "config_assist_command",
    "config_assist_seeds",
    "llmio_tier_level",
    "allow_chat_access",
    "user",
)


@dataclass
class ContractRefreshResult:
    """Outcome of a contract refresh."""

    new_config: ComponentConfig
    changed_fields: list[str] = field(default_factory=list)
    previous: dict[str, Any] = field(default_factory=dict)
    current: dict[str, Any] = field(default_factory=dict)


async def refresh_component_contract(
    name: str,
    component_config_store: ComponentConfigStore,
    config_yaml_store: ConfigYamlStore,
    registry: ComponentRegistry,
    lifecycle_config: LifecycleConfig,
) -> ContractRefreshResult:
    """Re-fetch a component's deploy contract and update the stored config.

    Fetches ``deploy/docker-compose.yml`` (and ``config/config.schema.json``)
    from the component's repo HEAD, rebuilds the ``ComponentConfig`` from the
    parsed contract (preserving operator-set fields and existing host-port
    assignments), persists it via *component_config_store*, and re-registers it
    in *registry*.  Returns which contract-derived fields changed.

    Raises ``HTTPException`` (404/400/422) on missing component, absent
    ``git_url``, repo-fetch failure, or compose/schema parse failure — same as
    the ``refresh-contract`` endpoint.
    """
    from robotsix_central_deploy.onboard.parser import (
        ParseError,
        parse_compose,
    )

    comp_cfg, repo_files = await _fetch_component_repo_files(
        name, component_config_store, lifecycle_config
    )

    loop = asyncio.get_running_loop()

    if repo_files.compose_bytes is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Repo of '{name}' has no deploy/docker-compose.yml — "
                "the component must commit a deploy contract first"
            ),
        )

    try:
        spec = await loop.run_in_executor(
            None, parse_compose, repo_files.compose_bytes, name, comp_cfg.git_url
        )
    except ParseError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"deploy/docker-compose.yml parse failed: {'; '.join(exc.violations)}",
        ) from exc

    if repo_files.config_schema_json is not None:
        try:
            spec.config_schema = json.loads(repo_files.config_schema_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"config/config.schema.json is not valid JSON: {exc}",
            ) from exc

    # Namespace volume names (same as onboard confirm)
    spec = _namespace_spec_volumes(spec, name)

    # Carry over host-port assignments before building the config. The manifest
    # states the port the repo author picked; the port this component actually
    # runs on was assigned at onboarding and may have been shifted to dodge a
    # collision.
    occupied = collect_occupied_host_ports(
        component_config_store, lifecycle_config.port, exclude_id=name
    )
    preserve_host_port_assignments(spec.ports, comp_cfg.ports, occupied)
    old_sibling_ports = {sib.service_key: sib.ports for sib in comp_cfg.siblings}
    for sib in spec.siblings:
        preserve_host_port_assignments(
            sib.ports, old_sibling_ports.get(sib.service_key, []), occupied
        )

    # Build the new ComponentConfig from the DerivedSpec, preserving
    # operator-set / system-set fields from the existing config.
    new_config = _build_component_config_from_spec(
        spec,
        git_url=comp_cfg.git_url,
        repo_id=comp_cfg.repo_id,
        auto_update_enabled=comp_cfg.auto_update_enabled,
        mem_limit=(spec.mem_limit if spec.mem_limit != "2g" else comp_cfg.mem_limit),
        memswap_limit=spec.memswap_limit or comp_cfg.memswap_limit,
        allow_chat_access=comp_cfg.allow_chat_access,
        # claude_mount has TWO legitimate grant sources: the compose label
        # (parsed into the spec) and the operator API (stored). A refresh must
        # never lose a grant from either side — the pre-2026-09-01 code pinned
        # the stored value, so a label added to the compose could never take
        # effect and a stale stored False silently dropped the claude-auth
        # mount on the next recreate (mill outage). The path follows the label
        # when the label grants the mount (it may relocate the credentials,
        # e.g. mill's /home/mill/.claude); an operator-only grant keeps the
        # stored path. Revocation is operator-API-only, deliberately.
        claude_mount=comp_cfg.claude_mount or spec.claude_mount,
        claude_mount_path=(
            spec.claude_mount_path if spec.claude_mount else comp_cfg.claude_mount_path
        ),
    )

    # Diff: collect which contract-derived fields changed.
    changed: list[str] = []
    previous: dict[str, Any] = {}
    current: dict[str, Any] = {}
    for cfg_field in _CONTRACT_FIELDS:
        old_val = getattr(comp_cfg, cfg_field)
        new_val = getattr(new_config, cfg_field)
        if old_val != new_val:
            changed.append(cfg_field)
            if hasattr(old_val, "model_dump"):
                previous[cfg_field] = old_val.model_dump()
            elif (
                isinstance(old_val, list)
                and old_val
                and hasattr(old_val[0], "model_dump")
            ):
                previous[cfg_field] = [v.model_dump() for v in old_val]
            else:
                previous[cfg_field] = old_val
            if hasattr(new_val, "model_dump"):
                current[cfg_field] = new_val.model_dump()
            elif (
                isinstance(new_val, list)
                and new_val
                and hasattr(new_val[0], "model_dump")
            ):
                current[cfg_field] = [v.model_dump() for v in new_val]
            else:
                current[cfg_field] = new_val

    # Persist the updated config
    await component_config_store.put(new_config)
    registry.register(new_config)

    # If the config schema changed (new or removed), refresh the stored template.
    if spec.config_schema is not None:
        if spec.config_schema != await config_yaml_store.get_template(name):
            changed.append("config_schema")
        await config_yaml_store.save_template(name, spec.config_schema)
    # Note: we do NOT remove the template if the schema is now absent —
    # the operator may still want the old schema in the dashboard.

    logger.info(
        "Refreshed contract for %s from repo: %d field(s) changed (%s)",
        _sanitize_log(name),
        len(changed),
        ", ".join(changed) if changed else "none",
    )

    return ContractRefreshResult(
        new_config=new_config,
        changed_fields=changed,
        previous=previous,
        current=current,
    )
