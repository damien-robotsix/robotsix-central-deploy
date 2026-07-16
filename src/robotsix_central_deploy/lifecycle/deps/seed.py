"""Config namespace, validation, and onboard-seed helpers."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, status

if TYPE_CHECKING:
    from robotsix_central_deploy.onboard.fetcher import RepoFiles
    from robotsix_central_deploy.onboard.models import DerivedSpec
    from ...registry.config_store import ComponentConfigStore
    from ...registry import ComponentConfig, ConfigAssistSeed

_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _namespace_spec_volumes(spec: "DerivedSpec", component_name: str) -> "DerivedSpec":
    """Prefix all named-volume hosts with the component name.

    Converts image-hardcoded names (e.g. ``auto-mail-config``) into
    per-component names (e.g. ``mail-auto-mail-config``) so two components
    from the same image never share storage.
    """
    from robotsix_central_deploy.onboard.models import SiblingDerivedSpec  # noqa: PLC0415
    from robotsix_central_deploy.registry.models import VolumeMount  # noqa: PLC0415

    old_to_new: dict[str, str] = {}

    def _rename(vm: VolumeMount) -> VolumeMount:
        new_host = f"{component_name}-{vm.host}"
        old_to_new[vm.host] = new_host
        return vm.model_copy(update={"host": new_host})

    new_primary_mounts = [_rename(vm) for vm in spec.volume_mounts]

    new_siblings: list[SiblingDerivedSpec] = [
        sib.model_copy(update={"mounts": [_rename(vm) for vm in sib.mounts]})
        for sib in spec.siblings
    ]

    new_config_vol = (
        old_to_new.get(spec.config_volume, spec.config_volume)
        if spec.config_volume is not None
        else None
    )

    return spec.model_copy(
        update={
            "volume_mounts": new_primary_mounts,
            "siblings": new_siblings,
            "config_volume": new_config_vol,
        }
    )


def _build_component_config_from_spec(
    spec: "DerivedSpec", *, git_url: str, **overrides: Any
) -> "ComponentConfig":
    """Build a ``ComponentConfig`` from a parsed ``DerivedSpec``.

    Shared factory for the onboard-confirm and contract-refresh paths so
    that field additions to ``ComponentConfig`` only need to happen once.
    *git_url* is required explicitly because the two callers source it
    differently (onboard uses ``spec.git_url``; refresh preserves the
    existing config's URL).  *overrides* lets the refresh path layer on
    operator-set fields (``repo_id``, ``caretaker_auto_update``)
    without branching.
    """
    from robotsix_central_deploy.registry.models import ComponentConfig  # noqa: PLC0415

    config = ComponentConfig(
        id=spec.name,
        image=spec.image,
        container_name=spec.container_name or spec.name,
        ports=spec.ports,
        mounts=spec.volume_mounts,
        env=spec.env,
        health_check=spec.health_check,
        command=spec.command,
        entrypoint=spec.entrypoint,
        tmpfs=spec.tmpfs,
        mem_limit=spec.mem_limit,
        claude_mount=spec.claude_mount,
        claude_mount_path=spec.claude_mount_path,
        host_docker_sock=spec.host_docker_sock,
        named_volumes=[m.host for m in spec.volume_mounts]
        + [m.host for sib in spec.siblings for m in sib.mounts],
        siblings=[sib.model_copy() for sib in spec.siblings],
        git_url=git_url,
        has_config_yaml=(spec.config_schema is not None),
        config_volume=spec.config_volume,
        config_assist_command=spec.config_assist_command,
        config_assist_seeds=spec.config_assist_seeds,
        llmio_tier_level=spec.llmio_tier_level,
        allow_chat_access=spec.allow_chat_access,
        user=spec.user,
        **overrides,
    )
    return config


def _validate_config_or_422(schema: dict[str, Any], values: dict[str, Any]) -> None:
    """Validate *values* against JSON Schema, raising HTTP 422 on failure."""
    import jsonschema

    try:
        jsonschema.validate(instance=values, schema=schema)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path)
        loc = f" at '{path}'" if path else ""
        raise HTTPException(
            status_code=422,
            detail={
                "error": f"Config validation error{loc}: {exc.message}",
            },
        )


def _validate_account_ids(merged: dict[str, Any]) -> None:
    """Raise HTTP 422 when any account id contains disallowed characters.

    auto-mail enforces account_id =~ ^[A-Za-z0-9._-]+$ at startup.
    The @ character (e.g. from using an email address as the id) triggers
    a crash-loop.  Validate before writing to storage.
    """
    for item in merged.get("accounts", []):
        if not isinstance(item, dict):
            continue
        id_val = item.get("id", "")
        if id_val and not _ACCOUNT_ID_RE.fullmatch(id_val):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"account_id {id_val!r} must match ^[A-Za-z0-9._-]+$ "
                    f"(no @ or spaces — use the slug derived from the email address)"
                ),
            )


def _prune_unset(merged: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    """Remove template-default empty fields that were absent from existing.

    Prevents empty-string placeholders for unused template sections
    (e.g. ``archive.namespace``, ``calendar.broker_*``) from being
    written to stored config after they fall through merge with
    empty/no value.

    Rules:
    - Empty string (``""``) or ``None`` at a scalar leaf: prune unless the key
      was already in *existing*.
    - Non-empty scalars (including int/float/bool and 0/False): always kept.
    - Dict values: recurse; include the sub-dict only when non-empty or
      the key was already present in *existing*.
    - List-of-dicts: recurse per item against the corresponding
      *existing* item (or ``{}`` for out-of-range indices).
    """
    result: dict[str, Any] = {}
    for k, v in merged.items():
        if isinstance(v, dict):
            sub_existing = existing[k] if isinstance(existing.get(k), dict) else {}
            pruned = _prune_unset(v, sub_existing)
            if pruned or k in existing:
                result[k] = pruned
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            ex_val = existing.get(k)
            ex_list: list[Any] = ex_val if isinstance(ex_val, list) else []
            result[k] = [
                _prune_unset(
                    item,
                    ex_list[i]
                    if i < len(ex_list) and isinstance(ex_list[i], dict)
                    else {},
                )
                for i, item in enumerate(v)
            ]
        elif v in ("", None) and k not in existing:
            pass  # skip: field was absent from existing and no new value set
        else:
            result[k] = v
    return result


_SEED_LIST_NO_MATCH = object()


def _seed_list_item(
    tval: Any,
    val: list[Any],
    ex_val: Any,
) -> list[Any]:
    """Handle the list-item branch of ``_seed_for_detect``.

    When *tval* is a non-empty list of dicts, recursively seeds each
    item in *val* using the first template element and the corresponding
    *ex_val* element (when available).  Returns the resolved list (may be
    empty).

    When *tval* does **not** match the dict-list pattern, returns
    ``_SEED_LIST_NO_MATCH`` to signal that the caller should use *val*
    as-is.
    """
    if not (isinstance(tval, list) and tval and isinstance(tval[0], dict)):
        return _SEED_LIST_NO_MATCH  # type: ignore[return-value]

    item_template = tval[0]
    ex_list = ex_val if isinstance(ex_val, list) else []
    items: list[dict[str, Any]] = []
    for i, item in enumerate(val):
        if isinstance(item, dict):
            sub = _seed_for_detect(
                item_template,
                ex_list[i] if i < len(ex_list) and isinstance(ex_list[i], dict) else {},
                item,
            )
            if sub:
                items.append(sub)
        else:
            items.append(item)
    return items


def _seed_for_detect(
    template: dict[str, Any],
    existing: dict[str, Any],
    submitted: dict[str, Any],
) -> dict[str, Any]:
    """Build a sparse seed config for the pre-detect volume write.

    Only keys present in *submitted* are emitted (recursively).
    ``"***"`` sentinel values are resolved from *existing* for secret fields.
    Template-default empty strings are skipped even when present in
    *submitted*, so the detect program sees absent fields and fills them
    in correctly.  Dict/list results that are entirely empty are also
    omitted.
    """
    result: dict[str, Any] = {}
    for key, val in submitted.items():
        tval = template.get(key) if isinstance(template, dict) else None
        ex_val = existing.get(key) if isinstance(existing, dict) else None

        if isinstance(val, str) and val == "":
            # Template default — skip, let detect fill it in.
            continue
        if isinstance(val, str) and val == "***":
            # Secret restoration: use existing value, or empty string if none.
            result[key] = ex_val if ex_val is not None else ""
        elif isinstance(val, str):
            result[key] = val
        elif isinstance(val, dict):
            sub = _seed_for_detect(
                tval if isinstance(tval, dict) else {},
                ex_val if isinstance(ex_val, dict) else {},
                val,
            )
            if sub:
                result[key] = sub
        elif isinstance(val, list):
            items = _seed_list_item(tval, val, ex_val)
            if items is _SEED_LIST_NO_MATCH:
                result[key] = val
            elif items:
                result[key] = items
        else:
            # Any other type (bool, int, float): include as-is.
            result[key] = val
    return result


def _relocate_account_seed_values(
    values: dict[str, Any],
    seeds: list["ConfigAssistSeed"],
    src_idx: int,
    dst_idx: int,
) -> None:
    """Move seed values from ``accounts[src_idx]`` to ``accounts[dst_idx]`` in-place.

    For each seed whose key starts with ``accounts.<src_idx>.``, extracts
    the value from the source slot and sets it on the destination slot —
    but ONLY when the destination slot does not already carry a non-empty
    value for that same seed key (so pre-populated multi-account submits
    from tests are not double-moved).

    ``"***"`` sentinels (unchanged secrets) are skipped — they already
    carry the correct meaning at the source and should not be relocated.
    """
    accts: list[dict[str, Any]] = values.setdefault("accounts", [])
    while len(accts) <= max(src_idx, dst_idx):
        accts.append({})
    src_acct: dict[str, Any] = accts[src_idx] if src_idx < len(accts) else {}
    dst_acct: dict[str, Any] = accts[dst_idx]

    for seed in seeds:
        parts = seed.key.split(".")
        if len(parts) < 3 or parts[0] != "accounts" or parts[1] != str(src_idx):
            continue

        # Check whether destination already has a non-empty value.
        dst_node: dict[str, Any] = dst_acct
        for p in parts[2:-1]:
            if not isinstance(dst_node, dict):
                dst_node = {}
                break
            dst_node = dst_node.get(p, {})
        if (
            isinstance(dst_node, dict)
            and parts[-1] in dst_node
            and dst_node[parts[-1]] not in (None, "", "***")
        ):
            continue  # already present at destination — nothing to move

        # Navigate to the leaf dict containing the key at source.
        node: dict[str, Any] = src_acct
        for p in parts[2:-1]:
            if not isinstance(node, dict):
                node = {}
                break
            node = node.get(p, {})
        last = parts[-1]
        if not isinstance(node, dict) or last not in node:
            continue
        val = node[last]
        if isinstance(val, str) and val == "***":
            continue  # unchanged secret — stays at source
        del node[last]
        # Place at destination.
        dst_node2: dict[str, Any] = dst_acct
        for p in parts[2:-1]:
            if isinstance(dst_node2, dict):
                dst_node2 = dst_node2.setdefault(p, {})
            else:
                break
        if isinstance(dst_node2, dict):
            dst_node2[last] = val


def _derive_account_id(
    seeds: list["ConfigAssistSeed"],
    partial: dict[str, Any],
    n: int,
) -> str:
    """Derive a slug-based account ID for a new account slot at index *n*.

    Looks for the first ConfigAssistSeed whose last path segment is
    ``username`` or ``email``, then navigates *partial* (replacing the
    hardcoded ``0`` index in the seed key with *n*) to get the submitted
    value.  Slugifies it (lower-case, non-alnum chars → ``-``, max 40
    chars).  Falls back to ``f'accounts-{n}'``.
    """
    import re as _re

    for seed in seeds:
        parts = seed.key.split(".")
        if parts[-1] not in ("username", "email"):
            continue
        nav_parts = [str(n) if p == "0" else p for p in parts]
        node: object = partial
        for part in nav_parts:
            if isinstance(node, dict):
                node = node.get(part)
            elif isinstance(node, list):
                try:
                    node = node[int(part)]
                except ValueError, IndexError:
                    node = None
            else:
                node = None
            if node is None:
                break
        if isinstance(node, str) and node:
            slug = _re.sub(r"[^a-z0-9]+", "-", node.lower()).strip("-")
            return slug[:40] or f"accounts-{n}"
    return f"accounts-{n}"


def _resolve_placeholders(command_str: str, values: dict[str, Any]) -> str:
    """Substitute ``{dotted.path}`` placeholders in *command_str* from *values*.

    Each placeholder is a dot-separated path of dict keys and list indices
    (e.g. ``accounts.0.auth.username``) into the nested *values* dict.
    Unresolvable placeholders are left as-is.
    """

    def _navigate(path: str) -> str | None:
        parts = path.split(".")
        node: object = values
        for part in parts:
            if isinstance(node, dict):
                node = node.get(part, _MISSING)
            elif isinstance(node, list):
                try:
                    idx = int(part)
                except ValueError:
                    return None
                if idx < 0 or idx >= len(node):
                    return None
                node = node[idx]
            else:
                return None
            if node is _MISSING:
                return None
        if isinstance(node, (str, int, float, bool)):
            return str(node)
        return None

    _MISSING = object()

    def _replacer(m: re.Match[str]) -> str:
        resolved = _navigate(m.group(1))
        return resolved if resolved is not None else m.group(0)

    return re.sub(r"\{([^{}]+)\}", _replacer, command_str)


async def _fetch_component_repo_files(
    name: str,
    component_config_store: "ComponentConfigStore",
) -> "tuple[ComponentConfig, RepoFiles]":
    """Look up *name* in *component_config_store*, verify it has a git_url,
    and fetch its repo files — raising the appropriate HTTPException at
    each step (404, 400, 422).  Returns the ``ComponentConfig`` and the
    fetched ``RepoFiles`` so callers can use whichever fields they need.
    """
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

    from robotsix_central_deploy.onboard.fetcher import (  # noqa: PLC0415
        FetchError,
        fetch_repo_files,
    )

    loop = asyncio.get_running_loop()
    try:
        repo_files = await loop.run_in_executor(
            None, fetch_repo_files, comp_cfg.git_url
        )
    except FetchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return comp_cfg, repo_files
