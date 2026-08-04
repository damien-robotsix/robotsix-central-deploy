"""Config merge and hashing utilities.

Extracted from ``lifecycle/deps.py`` and ``lifecycle/routers/services.py``
so the config-merge logic is independently testable.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from .backends import ExecutionBackend
    from ..registry.models import ComponentConfig

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Shared JSON Schema walker
# ---------------------------------------------------------------------------


class _SchemaWalker:
    """Shared recursive JSON Schema walker.

    Walks a schema's ``properties``, resolving ``$ref`` against the ROOT
    schema (``*$defs`` only exists there — resolving against the current
    sub-schema silently fails for objects nested two or more ``$ref``
    levels deep).  Handles the object-recursion and array-of-objects
    recursion skeleton and dispatches every other leaf type
    (secret / scalar / non-object array) to *visitor*.

    The visitor methods each receive ``(key, resolved, value_dicts,
    walk)`` where *resolved* is the ``$ref``-resolved property schema,
    ``value_dicts`` is the tuple of value dicts for the current schema
    level and ``walk`` is ``_SchemaWalker.walk`` for recursing into object
    / array-of-objects items.  The ``array`` method receives an extra
    *resolved_items* argument (the ``$ref``-resolved ``items`` schema) so
    ``$ref`` resolution never leaks into visitor code.

    A visitor method returns the value to store under *key* (including
    ``None``), or the ``SKIP`` sentinel to omit the key from the result
    entirely (used by ``_strip_secret_values`` to drop secret leaves).
    """

    #: sentinel returned by a visitor to omit a key from the result.
    SKIP = object()

    def __init__(self, schema: dict[str, Any], visitor: Any) -> None:
        self._schema = schema
        self._visitor = visitor

    def walk(
        self, i_schema: dict[str, Any], *value_dicts: dict[str, Any]
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        props = i_schema.get("properties", {})
        visited: set[str] = set()
        for key, prop in props.items():
            visited.add(key)
            resolved = _resolve_ref(prop, self._schema)
            if _is_secret_prop(resolved):
                value = self._visitor.secret(key, resolved, value_dicts, self.walk)
            elif resolved.get("type") == "object":
                sub_dicts: list[dict[str, Any]] = []
                for d in value_dicts:
                    if isinstance(d, dict):
                        child = d.get(key)
                        sub_dicts.append(child if isinstance(child, dict) else {})
                value = self.walk(resolved, *sub_dicts)
            elif resolved.get("type") == "array":
                items_schema = resolved.get("items", {})
                resolved_items = (
                    _resolve_ref(items_schema, self._schema)
                    if isinstance(items_schema, dict)
                    else None
                )
                value = self._visitor.array(
                    key, resolved, resolved_items, value_dicts, self.walk
                )
            else:
                value = self._visitor.scalar(key, resolved, value_dicts, self.walk)
            if value is not _SchemaWalker.SKIP:
                result[key] = value
        # Dispatch keys present in value dicts but absent from the schema
        # (preserves unrecognized keys for _strip_secret_values; a no-op
        # for visitors that don't implement ``unknown``).
        unknown = getattr(self._visitor, "unknown", None)
        if unknown is not None and value_dicts:
            first = value_dicts[0]
            if isinstance(first, dict):
                for key in first:
                    if key not in visited:
                        value = unknown(key, value_dicts)
                        if value is not _SchemaWalker.SKIP:
                            result[key] = value
        return result


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

    class _Visitor:
        def secret(
            self,
            key: str,
            resolved: dict[str, Any],
            value_dicts: tuple[dict[str, Any], ...],
            walk: Any,
        ) -> Any:
            return _SchemaWalker.SKIP  # drop secret leaves — operator supplies them

        def scalar(
            self,
            key: str,
            resolved: dict[str, Any],
            value_dicts: tuple[dict[str, Any], ...],
            walk: Any,
        ) -> Any:
            (i_values,) = value_dicts
            return i_values[key] if key in i_values else _SchemaWalker.SKIP

        def array(
            self,
            key: str,
            resolved: dict[str, Any],
            resolved_items: dict[str, Any] | None,
            value_dicts: tuple[dict[str, Any], ...],
            walk: Any,
        ) -> Any:
            (i_values,) = value_dicts
            val = i_values.get(key)
            if not isinstance(val, list):
                return val
            if (
                isinstance(resolved_items, dict)
                and resolved_items.get("type") == "object"
            ):
                return [
                    walk(resolved_items, item) if isinstance(item, dict) else item
                    for item in val
                ]
            return val

        def unknown(
            self,
            key: str,
            value_dicts: tuple[dict[str, Any], ...],
        ) -> Any:
            # Preserve any key in values that is not recognised by the schema.
            (i_values,) = value_dicts
            return i_values[key]

    return _SchemaWalker(schema, _Visitor()).walk(schema, values)


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

    class _Visitor:
        def secret(
            self,
            key: str,
            resolved: dict[str, Any],
            value_dicts: tuple[dict[str, Any], ...],
            walk: Any,
        ) -> Any:
            (i_current,) = value_dicts
            cval = i_current.get(key)
            if isinstance(cval, str) and cval and cval != "***":
                return "***"
            return ""

        def scalar(
            self,
            key: str,
            resolved: dict[str, Any],
            value_dicts: tuple[dict[str, Any], ...],
            walk: Any,
        ) -> Any:
            (i_current,) = value_dicts
            return i_current[key] if key in i_current else ""

        def array(
            self,
            key: str,
            resolved: dict[str, Any],
            resolved_items: dict[str, Any] | None,
            value_dicts: tuple[dict[str, Any], ...],
            walk: Any,
        ) -> Any:
            (i_current,) = value_dicts
            cval = i_current.get(key)
            if (
                isinstance(cval, list)
                and isinstance(resolved_items, dict)
                and resolved_items.get("type") == "object"
            ):
                return [
                    walk(resolved_items, item) if isinstance(item, dict) else item
                    for item in cval
                ]
            return cval if key in i_current else []

    return _SchemaWalker(schema, _Visitor()).walk(schema, current)


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

    class _Visitor:
        def secret(
            self,
            key: str,
            resolved: dict[str, Any],
            value_dicts: tuple[dict[str, Any], ...],
            walk: Any,
        ) -> Any:
            (i_existing, i_submitted) = value_dicts
            # Secrets always keep existing when unset — an omitted, blank,
            # or sentinel ("***") value means "keep existing".  Only an
            # explicitly supplied non-empty, non-sentinel value overwrites
            # the stored secret.
            sub_val = i_submitted.get(key)
            if sub_val and sub_val != "***":
                return str(sub_val)
            if key in i_existing:
                return i_existing[key]
            return ""

        def array(
            self,
            key: str,
            resolved: dict[str, Any],
            resolved_items: dict[str, Any] | None,
            value_dicts: tuple[dict[str, Any], ...],
            walk: Any,
        ) -> Any:
            (i_existing, i_submitted) = value_dicts
            if (
                isinstance(resolved_items, dict)
                and resolved_items.get("type") == "object"
                and isinstance(i_submitted.get(key), list)
            ):
                # Merge array-of-objects: iterate submitted items, merge
                # each against the corresponding existing item.
                submitted_list = i_submitted[key]
                existing_list: list[dict[str, Any]] = (
                    i_existing[key] if isinstance(i_existing.get(key), list) else []
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
                        merged_items.append(walk(resolved_items, eitem, sitem))
                    else:
                        merged_items.append(sitem)
                return merged_items
            if key in i_submitted:
                return i_submitted[key]
            if prefer_existing_for_unset and key in i_existing:
                return i_existing[key]
            return i_existing.get(key, [])

        def scalar(
            self,
            key: str,
            resolved: dict[str, Any],
            value_dicts: tuple[dict[str, Any], ...],
            walk: Any,
        ) -> Any:
            (i_existing, i_submitted) = value_dicts
            sub_val = i_submitted.get(key)
            if key in i_submitted and sub_val is None:
                # The form submits null for a field left empty (e.g. a
                # cleared number input on a nullable field).  Treat it like
                # an absent key instead of feeding None to type coercion,
                # which would 422 the whole save.
                if prefer_existing_for_unset and key in i_existing:
                    return i_existing[key]
                if "default" in resolved:
                    return resolved["default"]
                return None
            if key in i_submitted:
                return _coerce_by_schema(resolved, sub_val)
            if prefer_existing_for_unset and key in i_existing:
                return i_existing[key]
            if "default" in resolved:
                return resolved["default"]
            return ""

    return _SchemaWalker(schema, _Visitor()).walk(schema, existing, submitted)


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


# ---------------------------------------------------------------------------
# Secret-key path helpers
# ---------------------------------------------------------------------------


def _is_key_secret(schema: dict[str, Any], dotted_path: str) -> bool:
    """Return True when *dotted_path* resolves to a secret field in *schema*.

    Walks the JSON Schema properties and resolves ``$ref`` references
    against the root *schema*.  Returns ``False`` for legacy flat-dict
    templates (no ``"properties"`` key).
    """
    if not _is_json_schema(schema):
        return False
    parts = dotted_path.split(".")
    current_schema = schema
    for part in parts:
        props = current_schema.get("properties", {})
        prop = props.get(part)
        if prop is None:
            # The path segment may index into an array (e.g. accounts.0.password).
            # Try interpreting the part as an integer index and resolve
            # the parent's items schema instead.
            try:
                int(part)
            except ValueError:
                return False
            # Walk back: find the array property.  For simplicity we
            # resolve the next non-index segment against the current
            # schema's items, but dotted paths do not carry enough
            # structural info to map indexes precisely — we treat any
            # integer segment as "descend into array items" and
            # continue with the next segment.
            continue
        resolved = _resolve_ref(prop, schema)
        if _is_secret_prop(resolved):
            return True
        if resolved.get("type") == "object":
            current_schema = resolved
        elif resolved.get("type") == "array":
            items = resolved.get("items", {})
            if isinstance(items, dict):
                items = _resolve_ref(items, schema)
            if isinstance(items, dict):
                current_schema = items
            else:
                return False
        else:
            return False
    return False


def _restore_secrets_from_current(
    schema: dict[str, Any],
    restored: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """Copy secret leaf values from *current* into *restored*.

    Walks the JSON Schema properties recursively (including arrays of
    objects) and for every ``writeOnly``/``password`` field copies the
    value from *current* into *restored*.  Non-secret keys already
    present in *restored* are left unchanged.  Returns a new dict; the
    original *restored* is not modified.
    """
    if not _is_json_schema(schema):
        return restored

    class _Visitor:
        def secret(
            self,
            key: str,
            resolved: dict[str, Any],
            value_dicts: tuple[dict[str, Any], ...],
            walk: Any,
        ) -> Any:
            (i_restored, i_current) = value_dicts
            if key in i_current:
                return i_current[key]
            if key in i_restored:
                return i_restored[key]
            return _SchemaWalker.SKIP

        def array(
            self,
            key: str,
            resolved: dict[str, Any],
            resolved_items: dict[str, Any] | None,
            value_dicts: tuple[dict[str, Any], ...],
            walk: Any,
        ) -> Any:
            (i_restored, i_current) = value_dicts
            r_list = i_restored.get(key)
            c_list = i_current.get(key)
            if (
                isinstance(resolved_items, dict)
                and resolved_items.get("type") == "object"
                and isinstance(r_list, list)
                and isinstance(c_list, list)
            ):
                n = min(len(r_list), len(c_list))
                merged_items: list[Any] = []
                for i in range(n):
                    if isinstance(r_list[i], dict) and isinstance(c_list[i], dict):
                        merged_items.append(walk(resolved_items, r_list[i], c_list[i]))
                    else:
                        merged_items.append(r_list[i])
                # Preserve extra items from restored that extend beyond
                # current (the original in-place mutation left the tail
                # of r_list untouched).
                merged_items.extend(r_list[n:])
                return merged_items
            if key in i_restored:
                return i_restored[key]
            return _SchemaWalker.SKIP

        def scalar(
            self,
            key: str,
            resolved: dict[str, Any],
            value_dicts: tuple[dict[str, Any], ...],
            walk: Any,
        ) -> Any:
            (i_restored, i_current) = value_dicts
            if key in i_restored:
                return i_restored[key]
            return _SchemaWalker.SKIP

    return _SchemaWalker(schema, _Visitor()).walk(schema, restored, current)


# ---------------------------------------------------------------------------
# llmio tier config writer (shared by services_config, services_deploy)
# ---------------------------------------------------------------------------


def _sanitize_log(s: str) -> str:
    """Replace newlines so user input cannot inject fake log entries."""
    return s.replace("\n", "\\n").replace("\r", "\\r")


async def _write_llmio_tier_config(
    backend: ExecutionBackend,
    component_config: ComponentConfig,
    settings_store: Any,
    component_name: str,
    log_context: str = "config",
) -> None:
    """Write fleet-global llmio tier config into a component's config volume.

    No-op when the component has no ``llmio_tier_level`` or ``config_volume``,
    or the system settings lack ``llmio_tier_config``.  Logs a warning on
    failure but never raises — callers treat this as best-effort.

    *settings_store* may be ``None`` (e.g. when not yet attached to app state
    in early-startup paths).
    """
    if not component_config.llmio_tier_level or not component_config.config_volume:
        return

    try:
        settings = await settings_store.get() if settings_store is not None else None
        if settings and settings.llmio_tier_config:
            await backend.write_llmio_tier_config_to_volume(
                component_config.config_volume, settings.llmio_tier_config
            )
    except Exception as exc:
        logger.warning(
            "%s %s: could not write llmio tier config to volume %s: %s",
            log_context,
            _sanitize_log(component_name),
            _sanitize_log(component_config.config_volume),
            exc,
        )
