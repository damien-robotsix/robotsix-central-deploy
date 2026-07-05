"""Config merge and hashing utilities.

Extracted from ``lifecycle/deps.py`` and ``lifecycle/routers/services.py``
so the config-merge logic is independently testable.
"""

from __future__ import annotations

import hashlib
from typing import Any, cast

import yaml


# ---------------------------------------------------------------------------
# _deep_merge (from services.py)
# ---------------------------------------------------------------------------


def _deep_merge(
    base: dict[str, object], override: dict[str, object]
) -> dict[str, object]:
    """Recursively merge *override* into *base*; override values win on conflict."""
    result: dict[str, object] = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(
                cast("dict[str, object]", result[key]),
                cast("dict[str, object]", val),
            )
        else:
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# Config helpers (from deps.py)
# ---------------------------------------------------------------------------


def _is_json_schema(schema: dict[str, Any]) -> bool:
    """Return True when *schema* is a JSON Schema dict (has ``"properties"`` key).

    Legacy flat-dict templates are plain dicts without ``"properties"``.
    """
    return "properties" in schema


def _is_secret_prop(prop_schema: dict[str, Any]) -> bool:
    """Return True when *prop_schema* marks a secret field.

    Secrets are detected via ``"format": "password"`` and
    ``"writeOnly": true`` (pydantic ``SecretStr`` convention).
    """
    return (
        prop_schema.get("format") == "password" and prop_schema.get("writeOnly") is True
    )


def _strip_secret_values(
    schema: dict[str, Any] | None, values: dict[str, Any]
) -> dict[str, Any]:
    """Return a copy of *values* with all secret-typed leaves removed.

    Walks a JSON Schema and drops any value whose property is a secret
    (``format: password`` + ``writeOnly: true``), recursing into object
    properties.  Used to stop example/config-template files from injecting
    secret values during onboard — the operator must always supply secrets,
    so seeded example values must never resurrect a secret field.

    Non-JSON-Schema (flat) templates have no secret concept, so *values* is
    returned unchanged.
    """
    if not schema or not _is_json_schema(schema) or not isinstance(values, dict):
        return values if isinstance(values, dict) else {}

    def _recursive(
        i_schema: dict[str, Any], i_values: dict[str, Any]
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        props = i_schema.get("properties", {})
        for key, val in i_values.items():
            prop = props.get(key)
            resolved = _resolve_ref(prop, i_schema) if isinstance(prop, dict) else None
            if resolved is not None and _is_secret_prop(resolved):
                continue  # drop secret leaves — the operator supplies them
            if (
                resolved is not None
                and resolved.get("type") == "object"
                and isinstance(val, dict)
            ):
                result[key] = _recursive(resolved, val)
            else:
                result[key] = val
        return result

    return _recursive(schema, values)


def _resolve_ref(prop: dict[str, Any], root_schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve a local ``$ref`` (``#/$defs/<Name>``) against *root_schema*.

    Returns the original dict if no ``$ref`` is present.  External URI
    references are not supported.
    """
    ref = prop.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        defs = root_schema.get("$defs", {})
        def_name = ref[len("#/$defs/") :]
        resolved = defs.get(def_name)
        if isinstance(resolved, dict):
            return resolved
    return prop


def _coerce_by_schema(prop_schema: dict[str, Any], sval: Any) -> Any:
    """Coerce *sval* to the type declared in JSON Schema ``type`` keyword.

    Raises ``ValueError`` when coercion fails — callers surface the error
    as HTTP 422.
    """
    schema_type = prop_schema.get("type")
    if schema_type == "integer":
        if isinstance(sval, bool):
            raise ValueError(f"expected integer, got bool: {sval!r}")
        if not isinstance(sval, (int, float)):
            try:
                return int(sval)
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"expected integer, got {type(sval).__name__}: {sval!r}"
                ) from exc
        return int(sval)
    if schema_type == "number":
        if isinstance(sval, bool):
            raise ValueError(f"expected number, got bool: {sval!r}")
        try:
            return float(sval)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"expected number, got {type(sval).__name__}: {sval!r}"
            ) from exc
    if schema_type == "boolean":
        if isinstance(sval, bool):
            return sval
        if isinstance(sval, str):
            low = sval.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off", ""):
                return False
        raise ValueError(f"expected boolean, got {type(sval).__name__}: {sval!r}")
    if schema_type == "string":
        return str(sval)
    return sval


# ---------------------------------------------------------------------------
# canonical hash (config drift detection)
# ---------------------------------------------------------------------------


def _canonical_hash(d: dict[str, Any]) -> str:
    """SHA-256 of a canonically serialised YAML dict.

    Serialises via ``yaml.dump`` with ``sort_keys=True`` before hashing so
    key-insertion-order differences and Python-vs-docker-exec YAML
    formatting differences do not cause false drift positives.
    Returns the full 64-char hex digest.
    """
    serialised = yaml.dump(
        d, default_flow_style=False, allow_unicode=True, sort_keys=True
    )
    return hashlib.sha256(serialised.encode()).hexdigest()


# ---------------------------------------------------------------------------
# secret masking
# ---------------------------------------------------------------------------


def _mask_secrets(schema: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Return *current* with secret leaf values replaced by ``"***"``.

    Secrets are detected via ``"format": "password"`` + ``"writeOnly": true``
    in the JSON Schema properties.  When *schema* is a legacy flat-dict
    template (no ``"properties"`` key) there are no secret annotations, so
    *current* is returned unchanged.
    """
    if _is_json_schema(schema):
        return _mask_secrets_json_schema(schema, current)
    return current


def _mask_secrets_json_schema(
    schema: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:

    def _recursive(
        i_schema: dict[str, Any], i_current: dict[str, Any]
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, prop in i_schema.get("properties", {}).items():
            resolved = _resolve_ref(prop, i_schema)
            cval = i_current.get(key)
            if _is_secret_prop(resolved):
                if isinstance(cval, str) and cval and cval != "***":
                    result[key] = "***"
                else:
                    result[key] = ""
            elif resolved.get("type") == "object":
                result[key] = (
                    _recursive(resolved, cval) if isinstance(cval, dict) else {}
                )
            else:
                result[key] = cval if key in i_current else ""
        return result

    return _recursive(schema, current)


def _merge_config(
    schema: dict[str, Any],
    existing: dict[str, Any],
    submitted: dict[str, Any],
    *,
    prefer_existing_for_unset: bool = False,
) -> dict[str, Any]:
    """Deep-merge *submitted* over *existing*, respecting secret handling.

    * **JSON Schema** (has ``"properties"`` key): iterates properties,
      resolves ``$ref``, coerces by ``type``, detects secrets via
      ``format:password`` + ``writeOnly:true``.
    * **Legacy flat-dict template** (no ``"properties"`` key): iterates
      dict keys directly, recurses into nested dicts.

    *prefer_existing_for_unset*: when True, a key absent from *submitted*
    falls back to ``existing[key]`` (not the template default) whenever the
    operator already has a value for it.
    """
    if _is_json_schema(schema):
        return _merge_config_json_schema(
            schema,
            existing,
            submitted,
            prefer_existing_for_unset=prefer_existing_for_unset,
        )
    return _merge_config_flat(
        schema, existing, submitted, prefer_existing_for_unset=prefer_existing_for_unset
    )


def _merge_config_json_schema(
    schema: dict[str, Any],
    existing: dict[str, Any],
    submitted: dict[str, Any],
    *,
    prefer_existing_for_unset: bool = False,
) -> dict[str, Any]:

    def _recursive(
        i_schema: dict[str, Any],
        i_existing: dict[str, Any],
        i_submitted: dict[str, Any],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, prop in i_schema.get("properties", {}).items():
            resolved = _resolve_ref(prop, i_schema)
            if _is_secret_prop(resolved) and i_submitted.get(key) == "***":
                result[key] = i_existing.get(key, "")
            elif resolved.get("type") == "object":
                sub_existing = (
                    i_existing[key] if isinstance(i_existing.get(key), dict) else {}
                )
                sub_submitted = (
                    i_submitted[key] if isinstance(i_submitted.get(key), dict) else {}
                )
                result[key] = _recursive(resolved, sub_existing, sub_submitted)
            elif key in i_submitted:
                try:
                    result[key] = _coerce_by_schema(resolved, i_submitted[key])
                except ValueError:
                    raise
            elif prefer_existing_for_unset and key in i_existing:
                result[key] = i_existing[key]
            elif "default" in resolved:
                result[key] = resolved["default"]
            else:
                result[key] = ""
        return result

    return _recursive(schema, existing, submitted)


def _merge_config_flat(
    template: dict[str, Any],
    existing: dict[str, Any],
    submitted: dict[str, Any],
    *,
    prefer_existing_for_unset: bool = False,
) -> dict[str, Any]:
    """Deep-merge for legacy flat-dict templates (no ``"properties"`` key).

    Iterates *template* keys directly, recurses into nested dicts, and
    merges array-of-objects lists when the template leaf is a list of dicts.
    No secret-sentinel handling — that heuristic has been deleted.
    """

    def _recursive(
        i_template: dict[str, Any],
        i_existing: dict[str, Any],
        i_submitted: dict[str, Any],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, tval in i_template.items():
            if (
                isinstance(tval, dict)
                and isinstance(i_existing.get(key), dict)
                and isinstance(i_submitted.get(key), dict)
            ):
                result[key] = _recursive(tval, i_existing[key], i_submitted[key])
            elif isinstance(tval, list) and tval and isinstance(tval[0], dict):
                item_template = tval[0]
                submitted_list = i_submitted[key]
                raw_existing = i_existing.get(key)
                existing_list: list[dict[str, Any]] = (
                    raw_existing if isinstance(raw_existing, list) else []
                )
                merged_items: list[dict[str, Any]] = []
                for i, sitem in enumerate(submitted_list):
                    if isinstance(sitem, dict):
                        eitem = (
                            existing_list[i]
                            if i < len(existing_list)
                            and isinstance(existing_list[i], dict)
                            else {}
                        )
                        merged_items.append(_recursive(item_template, eitem, sitem))
                    else:
                        merged_items.append(sitem)
                result[key] = merged_items
            elif key in i_submitted:
                result[key] = i_submitted[key]
            elif prefer_existing_for_unset and key in i_existing:
                result[key] = i_existing[key]
            else:
                result[key] = tval
        return result

    return _recursive(template, existing, submitted)
