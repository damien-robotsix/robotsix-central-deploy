"""Service config.yaml endpoints for the lifecycle server."""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from robotsix_central_deploy._yaml_utils import deep_merge

from ..auth import verify_auth
from ..backend import ExecutionBackend
from ..deps import (
    _canonical_hash,
    _derive_account_id,
    _get_backend,
    _get_component_config_store,
    _get_config_yaml_store,
    _get_env_store,
    _get_or_create_record,
    _get_sibling_pairs,
    _get_store,
    _mask_secrets,
    _merge_config,
    _prune_unset,
    _relocate_account_seed_values,
    _resolve_placeholders,
    _seed_for_detect,
    _validate_account_ids,
    _validate_config_or_422,
)
from ..models import (
    ErrorDetail,
    ServiceState,
)
from ..schemas import (
    ConfigAssistRequest,
    ConfigAssistResponse,
    ConfigDriftConflict,
    ConfigImportResponse,
    ConfigSchemaRefreshResponse,
    ConfigResponse,
    ConfigUpdate,
)
from ..store import ServiceStore
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.env_store import EnvStore
from ...registry.loader import ComponentRegistry

logger = logging.getLogger(__name__)


def _sanitize(value: str) -> str:
    """Replace newlines to prevent log-injection (CWE-117)."""
    return value.replace("\n", "\\n").replace("\r", "\\r")


router = APIRouter(tags=["services"])


# ---------------------------------------------------------------------------
# Private helpers extracted from long route handlers
# ---------------------------------------------------------------------------


def _resolve_account_mode(
    current_raw: dict[str, Any] | None,
    target_account_index: int | None,
    config_assist_seeds: list[Any],
    template: dict[str, Any],
    existing: dict[str, Any],
    values: dict[str, Any],
    account_name: str | None,
    assist_command: str,
) -> tuple[str, int, dict[str, Any], str]:
    """Resolve account mode, target index, updated partial, and command.

    Returns (mode, target_idx, partial, updated_assist_command).
    Modifies *values* in-place for add_new relocation.
    """
    import re as _re  # noqa: PLC0415

    existing_accounts: list[dict[str, Any]] = (
        [
            a
            for a in current_raw.get("accounts", [])
            if isinstance(a, dict) and a.get("id")
        ]
        if current_raw is not None and isinstance(current_raw.get("accounts"), list)
        else []
    )
    req_idx = target_account_index

    if req_idx is not None and req_idx < len(existing_accounts):
        mode, target_idx = "update", req_idx
    elif existing_accounts:  # req_idx is None OR req_idx >= len
        mode, target_idx = "add_new", len(existing_accounts)
    else:
        mode, target_idx = "first_setup", 0

    # Rewrite accounts.0.* placeholders to the target index in the command.
    if target_idx != 0:
        assist_command = _re.sub(
            r"\{accounts\.0\.",
            f"{{accounts.{target_idx}.",
            assist_command,
        )

    # Sparse submission merge
    partial = _merge_config(template, existing, values, prefer_existing_for_unset=True)

    # For add_new: relocate seed values to the target slot, restore existing
    # accounts, re-merge, and validate.
    if mode == "add_new":
        _relocate_account_seed_values(values, config_assist_seeds, 0, target_idx)
        submitted_accts: list[dict[str, Any]] = values.setdefault("accounts", [])
        for i, ea in enumerate(existing_accounts):
            if i < len(submitted_accts):
                submitted_accts[i] = dict(ea)
            else:
                submitted_accts.append(dict(ea))
        partial = _merge_config(
            template, existing, values, prefer_existing_for_unset=True
        )

        new_id = _derive_account_id(config_assist_seeds, partial, target_idx)
        if account_name:
            _name_slug = _re.sub(r"[^a-z0-9]+", "-", account_name.lower()).strip("-")[
                :40
            ]
            if _name_slug:
                new_id = _name_slug
        acct_list: list[dict[str, Any]] = partial.setdefault("accounts", [])
        while len(acct_list) <= target_idx:
            acct_list.append({})
        acct_list[target_idx]["id"] = new_id
        _validate_account_ids(partial)  # fail fast: id must match ^[A-Za-z0-9._-]+$

    return mode, target_idx, partial, assist_command


def _postprocess_config_assist(
    merged: dict[str, Any], output: str
) -> tuple[dict[str, Any], str]:
    """Drop unconfigured accounts, fix default_account, detect Office365.

    Returns (merged, output) — both may be mutated.
    """
    accts_obj = merged.get("accounts")
    if not isinstance(accts_obj, list):
        return merged, output

    kept: list[Any] = []
    for a in accts_obj:
        if not isinstance(a, dict):
            continue
        auth = a.get("auth")
        imap = a.get("imap")
        user = auth.get("username") if isinstance(auth, dict) else None
        host = imap.get("host") if isinstance(imap, dict) else None
        if user or host:
            kept.append(a)
    merged["accounts"] = kept
    kept_ids = [a.get("id") for a in kept]
    if kept and merged.get("default_account") not in kept_ids:
        merged["default_account"] = kept[0].get("id", "")

    # Office365 accounts: ensure oauth2_provider is flagged and prompt operator
    _O365_SUFFIX = "office365.com"
    _o365_detected = False
    for _acct in kept:
        _imap = _acct.get("imap")
        _smtp = _acct.get("smtp")
        _imap_host = _imap.get("host", "") if isinstance(_imap, dict) else ""
        _smtp_host = _smtp.get("host", "") if isinstance(_smtp, dict) else ""
        if _imap_host.endswith(_O365_SUFFIX) or _smtp_host.endswith(_O365_SUFFIX):
            _acct_auth: Any = _acct.get("auth")
            if not isinstance(_acct_auth, dict):
                _acct_auth = {}
                _acct["auth"] = _acct_auth
            _acct_auth["oauth2_provider"] = "microsoft"
            _acct_auth.pop("password", None)
            _o365_detected = True
    if _o365_detected:
        _o365_msg = (
            "Microsoft/Office365 account detected — authorize it from the "
            "mail board (Authorize button) to connect."
        )
        output = f"{output}\n{_o365_msg}" if output.strip() else _o365_msg

    return merged, output


def _build_assist_command(
    assist_command: str,
    partial: dict[str, Any],
    mode: str,
) -> str:
    """Resolve placeholders in *assist_command* and strip --overwrite for add_new."""
    resolved = shlex.join(
        _resolve_placeholders(arg, partial) for arg in shlex.split(assist_command)
    )
    if mode == "add_new":
        resolved = shlex.join(
            arg for arg in shlex.split(resolved) if arg != "--overwrite"
        )
    return resolved


# ---------------------------------------------------------------------------
# GET /services/{name}/config
# ---------------------------------------------------------------------------


@router.get(
    "/services/{name}/config",
    response_model=ConfigResponse,
    summary="Get config.yaml schema and current values for a service",
    responses={
        404: {"model": ErrorDetail, "description": "Service has no config schema"}
    },
)
async def get_service_config(
    name: str,
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ConfigResponse:
    """Return the config.yaml schema and current masked values for a service.

    Raises 404 if the service has no config schema.
    """
    await _get_or_create_record(name, store)
    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for component '{name}'",
        )
    current_raw = await config_yaml_store.get_current(name)
    if current_raw is None:
        current_raw = _merge_config(template, {}, {})
    current_masked = _mask_secrets(template, current_raw)
    comp_cfg = component_config_store.get(name)

    drift = False
    if comp_cfg and comp_cfg.config_volume:
        stored_hash = await config_yaml_store.get_volume_hash(name)
        if stored_hash is not None:
            live_dict = await backend.read_config_from_volume(comp_cfg.config_volume)
            drift = _canonical_hash(live_dict) != stored_hash

    return ConfigResponse(
        config_schema=template,
        current=current_masked,
        drift=drift,
        config_assist_command=comp_cfg.config_assist_command if comp_cfg else None,
        config_assist_seeds=comp_cfg.config_assist_seeds if comp_cfg else [],
    )


# ---------------------------------------------------------------------------
# PUT /services/{name}/config
# ---------------------------------------------------------------------------


@router.put(
    "/services/{name}/config",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Merge and save config.yaml values for a service",
    responses={
        404: {"model": ErrorDetail, "description": "Service has no config schema"}
    },
)
async def put_service_config(
    name: str,
    body: ConfigUpdate,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> None:
    """Merge and save config.yaml values for a service, then write to the config volume.

    Restarts the running container (and any siblings sharing the config
    volume) so new values take effect immediately. Returns 204 No Content.
    Raises 404 if the service has no config schema.
    """
    await _get_or_create_record(name, store)
    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for component '{name}'",
        )
    existing = await config_yaml_store.get_current(name) or template

    # --- drift guard ---
    drifted = False
    live_dict_for_conflict: dict[str, Any] | None = None
    comp_cfg = component_config_store.get(name)
    if comp_cfg and comp_cfg.config_volume:
        stored_hash = await config_yaml_store.get_volume_hash(name)
        if stored_hash is not None:
            live_dict_for_conflict = await backend.read_config_from_volume(
                comp_cfg.config_volume
            )
            drifted = _canonical_hash(live_dict_for_conflict) != stored_hash
    if drifted and not body.force_overwrite:
        assert live_dict_for_conflict is not None
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ConfigDriftConflict(
                live_config=_mask_secrets(template, live_dict_for_conflict),
                stored_config=_mask_secrets(template, existing),
            ).model_dump(),
        )
    # --- end drift guard ---

    try:
        merged = _merge_config(template, existing, body.values)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": str(exc)},
        )
    if "accounts" in merged:
        _validate_account_ids(merged)  # Bug 2: reject invalid id slugs
    merged = _prune_unset(merged, existing)  # Bug 3: prune resurrected empty fields
    _validate_config_or_422(template, merged)

    if comp_cfg and comp_cfg.config_volume:
        await backend.write_config_to_volume(comp_cfg.config_volume, merged)
        new_hash = _canonical_hash(merged)
        await config_yaml_store.update_current_and_hash(name, merged, new_hash)
        # Restart primary + siblings sharing the same config volume so the
        # running container(s) pick up the new values immediately.
        registry: ComponentRegistry = request.app.state.registry
        store2: ServiceStore = store  # local alias for clarity
        record = await store2.get(name)
        if record and record.state == ServiceState.RUNNING:
            try:
                await backend.restart(record)
            except Exception as exc:
                logger.warning(
                    "config saved for %s but restart failed: %s", _sanitize(name), exc
                )
        # Fan out to siblings that share the same config volume
        config = registry.get(name) if registry else None
        if config and config.siblings:
            for sib, sib_record in await _get_sibling_pairs(name, config, store2):
                if sib_record.state != ServiceState.RUNNING:
                    continue
                try:
                    await backend.restart(sib_record)
                except Exception as exc:
                    logger.warning(
                        "config saved for %s but sibling '%s' restart failed: %s",
                        _sanitize(name),
                        _sanitize(sib_record.name),
                        exc,
                    )
    else:
        await config_yaml_store.update_current(name, merged)
        logger.warning(
            "put_service_config: no config_volume for %s — config written to store only",
            _sanitize(name),
        )


# ---------------------------------------------------------------------------
# POST /services/{name}/config/import
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/config/import",
    response_model=ConfigImportResponse,
    summary="Import live volume content into the config store, clearing drift",
    responses={
        404: {
            "model": ErrorDetail,
            "description": "Service has no config schema or config volume",
        },
    },
)
async def import_service_config(
    name: str,
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ConfigImportResponse:
    """Read the live volume file and store it as the new *current*, clearing drift.

    The imported dict is stored as-is (real secret values preserved, since the
    volume holds real values). The volume hash is updated to match, so subsequent
    drift checks see a clean state.
    """
    await _get_or_create_record(name, store)
    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for '{name}'",
        )
    comp_cfg = component_config_store.get(name)
    if comp_cfg is None or not comp_cfg.config_volume:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config volume for '{name}'",
        )
    live_dict = await backend.read_config_from_volume(comp_cfg.config_volume)
    new_hash = _canonical_hash(live_dict)
    await config_yaml_store.update_current_and_hash(name, live_dict, new_hash)
    return ConfigImportResponse(
        current=_mask_secrets(template, live_dict),
        volume_hash=new_hash,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/config/refresh-schema
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/config/refresh-schema",
    response_model=ConfigSchemaRefreshResponse,
    summary="Refetch config/config.schema.json from the repo and replace the stored template",
    responses={
        400: {"model": ErrorDetail, "description": "Component has no git_url"},
        404: {
            "model": ErrorDetail,
            "description": "Component not found or repo has no config/config.schema.json",
        },
        422: {
            "model": ErrorDetail,
            "description": "Repo fetch failed or schema is invalid JSON",
        },
    },
)
async def refresh_config_schema(
    name: str,
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    _auth: None = Depends(verify_auth),
) -> ConfigSchemaRefreshResponse:
    """Replace the stored config template with the repo's committed schema.

    Components onboarded before the schema-driven config keep the legacy raw
    template captured at onboard time; this refetches ``config/config.schema.json``
    from the repo HEAD so the typed schema (field types, enums, descriptions)
    reaches the dashboard without re-onboarding. Stored *values* are untouched.
    """
    import json as _json  # noqa: PLC0415

    from robotsix_central_deploy.onboard.fetcher import (  # noqa: PLC0415
        FetchError,
        fetch_repo_files,
    )

    comp_cfg = component_config_store.get(name)
    if comp_cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Component '{name}' not found",
        )
    if not comp_cfg.git_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Component '{name}' has no git_url — cannot fetch its repo",
        )

    loop = asyncio.get_running_loop()
    try:
        repo_files = await loop.run_in_executor(
            None, fetch_repo_files, comp_cfg.git_url
        )
    except FetchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if repo_files.config_schema_json is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Repo of '{name}' has no config/config.schema.json — the "
                "component must commit a typed schema first"
            ),
        )
    try:
        schema = _json.loads(repo_files.config_schema_json)
    except _json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"config/config.schema.json is not valid JSON: {exc}",
        ) from exc

    await config_yaml_store.save_template(name, schema)
    logger.info("Refreshed config schema for %s from repo", _sanitize(name))
    return ConfigSchemaRefreshResponse(config_schema=schema)


# ---------------------------------------------------------------------------
# POST /services/{name}/config/assist
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/config/assist",
    response_model=ConfigAssistResponse,
    summary="Run a repo-declared config-assist command in a one-shot container and return auto-filled config",
    responses={
        400: {
            "model": ErrorDetail,
            "description": "No config-assist command or config volume configured",
        },
        404: {"model": ErrorDetail, "description": "Component not found"},
        504: {"model": ErrorDetail, "description": "Assist command timed out"},
    },
)
async def run_config_assist(
    name: str,
    body: ConfigAssistRequest,
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    env_store: EnvStore = Depends(_get_env_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ConfigAssistResponse:
    """Run a repo-declared config-assist command in a one-shot container.

    Fetches fresh config-assist metadata from the component's git repo, runs
    the detect/assist command inside a temporary container with the config
    volume mounted, and merges detected fields into the user-submitted values.
    Raises 400 if no config-assist command or config volume is configured,
    404 if the component is not found, and 504 if the assist command times out.
    """
    comp_cfg = component_config_store.get(name)
    if comp_cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Component '{name}' not found",
        )

    # Re-read config-assist fields from repo HEAD. Write back if changed so
    # GET /config and future assist calls also see the fresh values.
    try:
        loop = asyncio.get_running_loop()
        from ..server import _fetch_fresh_config_assist  # noqa: PLC0415

        fresh_cmd, fresh_seeds = await loop.run_in_executor(
            None, _fetch_fresh_config_assist, comp_cfg.git_url, name
        )
        if (
            fresh_cmd != comp_cfg.config_assist_command
            or fresh_seeds != comp_cfg.config_assist_seeds
        ):
            comp_cfg = comp_cfg.model_copy(
                update={
                    "config_assist_command": fresh_cmd,
                    "config_assist_seeds": fresh_seeds,
                }
            )
            await component_config_store.put(comp_cfg)
            logger.info(
                "Refreshed config-assist fields for %s from repo", _sanitize(name)
            )
    except Exception as exc:
        logger.warning(
            "Could not refresh config-assist fields for %s from repo (%s); "
            "using stored values",
            _sanitize(name),
            exc,
        )

    if comp_cfg.config_assist_command is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No config-assist command configured for '{name}'",
        )
    if comp_cfg.config_volume is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No config volume for '{name}' — add robotsix.deploy.config-target label",
        )

    # Fetch config template + existing current
    template = await config_yaml_store.get_template(name) or {}
    current_raw = await config_yaml_store.get_current(name)
    existing = current_raw or template
    # --- Account-aware mode resolution ---
    mode, target_idx, partial, assist_command = _resolve_account_mode(
        current_raw,
        body.target_account_index,
        comp_cfg.config_assist_seeds,
        template,
        existing,
        body.values,
        body.account_name,
        comp_cfg.config_assist_command,
    )

    # Write sparse seed config into the volume (only submitted keys, no
    # template-default empty strings).  This lets the detect program fill
    # in absent/null fields correctly instead of treating pre-existing
    # empty strings as "already configured".
    if mode == "add_new":
        # Write existing accounts verbatim so detect does not re-validate them.
        # Write only the new account's seed fields (not template defaults).
        item_template = (template.get("accounts") or [{}])[0]
        submitted_accts = body.values.get("accounts", [])
        new_acct_vals = (
            submitted_accts[target_idx] if target_idx < len(submitted_accts) else {}
        )
        new_acct_seed = _seed_for_detect(item_template, {}, new_acct_vals)
        detect_seed: dict[str, Any] = {
            k: v for k, v in existing.items() if k != "accounts"
        }
        detect_seed["accounts"] = list(existing.get("accounts", [])) + [new_acct_seed]
        await backend.write_config_to_volume(comp_cfg.config_volume, detect_seed)
    else:
        await backend.write_config_to_volume(
            comp_cfg.config_volume,
            _seed_for_detect(template, existing, body.values),
        )

    # Resolve the container-side mount path for the config volume
    volume_mount_path = next(
        (m.container for m in comp_cfg.mounts if m.host == comp_cfg.config_volume),
        "/config",  # safe fallback (matches busybox writer convention)
    )

    # Fetch decrypted env+secrets
    merged_env = await env_store.get_merged_env(name, comp_cfg.env)

    # Build resolved command from template with placeholder substitution
    resolved_command = _build_assist_command(assist_command, partial, mode)

    # Run the one-shot container (60 s timeout)
    try:
        output = await backend.run_config_assist(
            image=comp_cfg.image,
            command_str=resolved_command,
            volume_name=comp_cfg.config_volume,
            volume_mount_path=volume_mount_path,
            env_dict=merged_env,
            timeout_seconds=60,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        )
    except RuntimeError as exc:
        output = str(exc)

    # Read back the updated config from the volume
    filled = await backend.read_config_from_volume(comp_cfg.config_volume)

    # Merge detected fields into the submitted config so the detected
    # output never clobbers other fields the user already entered.
    if mode == "add_new":
        # deep_merge replaces the accounts list wholesale. Guard: always take
        # existing accounts from storage (not from what the detect program may
        # have re-written), and only take the new account's slot from filled.
        filled_accts = filled.get("accounts", [])
        # Prefer the detected slot at target_idx; fall back to last entry.
        new_acct_from_filled = (
            filled_accts[target_idx]
            if target_idx < len(filled_accts)
            else (filled_accts[-1] if filled_accts else {})
        )
        new_acct_partial = (
            partial["accounts"][target_idx]
            if target_idx < len(partial.get("accounts", []))
            else {}
        )
        merged_new_acct = deep_merge(dict(new_acct_partial), new_acct_from_filled)
        # Merge non-accounts keys normally.
        merged = deep_merge(
            dict({k: v for k, v in partial.items() if k != "accounts"}),
            {k: v for k, v in filled.items() if k != "accounts"},
        )
        assert (
            current_raw is not None
        )  # add_new mode only reachable when current_raw is set
        merged["accounts"] = list(current_raw.get("accounts", [])) + [merged_new_acct]
    else:
        merged = deep_merge(dict(partial), filled)

    # Post-process: drop unconfigured accounts and detect Office365
    merged, output = _postprocess_config_assist(merged, output)

    # Write the cleaned config back to the volume so the board reads the
    # de-stubbed config with a valid default_account (the detect output left
    # the empty template slot and/or default_account='main').
    await backend.write_config_to_volume(comp_cfg.config_volume, merged)
    # Persist detected config so GET /config shows it and Save is idempotent
    await config_yaml_store.update_current_and_hash(
        name, merged, _canonical_hash(merged)
    )

    return ConfigAssistResponse(config=merged, output=output)
