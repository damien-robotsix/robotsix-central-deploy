"""One-time startup migration for the caretaker auto-update flags.

Retires the special-case ``caretaker_self_update_enabled`` operator setting and
the legacy per-component ``caretaker_auto_update`` field in favour of the single
unified per-component ``auto_update_enabled`` flag. Runs once at startup, before
the component registry / settings are consumed, and rewrites the persisted JSON
files so the legacy keys are never silently ignored.

Rules:

* A persisted component config carrying ``caretaker_auto_update`` has that value
  copied onto ``auto_update_enabled`` (an already-present ``auto_update_enabled``
  wins) and the legacy key is dropped.
* The operator-level ``caretaker_self_update_enabled`` value (settings store
  first, falling back to config.json — matching the settings-overlay
  precedence) is returned to the caller so it can be applied to the
  ``central-deploy`` component's ``auto_update_enabled``. The operator value is
  the historically authoritative switch for the plane's own self-update, so it
  wins over any per-component value on that row.
* The legacy ``caretaker_self_update_enabled`` key is removed from every file it
  appears in, so a config still carrying it is migrated rather than silently
  ignored.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LEGACY_SELF_UPDATE_KEY = "caretaker_self_update_enabled"
_LEGACY_COMPONENT_KEY = "caretaker_auto_update"
_UNIFIED_KEY = "auto_update_enabled"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "auto-update migration: could not parse %s — skipped (%s)", path, exc
        )
        return {}
    if not isinstance(data, dict):
        logger.warning("auto-update migration: %s is not an object — skipped", path)
        return {}
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _migrate_component_auto_update(raw: dict[str, Any]) -> bool:
    """Map ``caretaker_auto_update`` -> ``auto_update_enabled`` on every row.

    Returns True if any row changed. ``auto_update_enabled`` already present on
    a row wins (an explicit value is never overwritten); otherwise the legacy
    value is copied when it is a bool.
    """
    changed = False
    for row in raw.values():
        if not isinstance(row, dict):
            continue
        if _LEGACY_COMPONENT_KEY not in row:
            continue
        legacy = row.get(_LEGACY_COMPONENT_KEY)
        if _UNIFIED_KEY not in row and isinstance(legacy, bool):
            row[_UNIFIED_KEY] = legacy
        del row[_LEGACY_COMPONENT_KEY]
        changed = True
    return changed


def _legacy_self_update_value(
    config_raw: dict[str, Any], settings_raw: dict[str, Any]
) -> bool | None:
    """Resolve the operator's legacy ``caretaker_self_update_enabled`` value.

    The settings store wins over config.json, mirroring the settings-overlay
    precedence (an entry in the store represents a deliberate operator choice).
    """
    for source in (settings_raw, config_raw):
        value = source.get(_LEGACY_SELF_UPDATE_KEY)
        if isinstance(value, bool):
            return value
    return None


def migrate_legacy_auto_update_settings(
    component_config_path: Path,
    system_settings_path: Path,
    config_path: Path | None,
) -> bool | None:
    """Run the one-time migration and return the resolved operator self-update value.

    * Maps ``caretaker_auto_update`` onto ``auto_update_enabled`` in the
      persisted component configs.
    * Removes the legacy ``caretaker_self_update_enabled`` key from the settings
      store and config.json.
    * Returns the resolved legacy operator value (``bool``), or ``None`` when no
      legacy value was present, so the caller can apply it to the
      ``central-deploy`` component's ``auto_update_enabled``.
    """
    component_raw = _read_json(component_config_path)
    settings_raw = _read_json(system_settings_path)
    config_raw = _read_json(config_path) if config_path is not None else {}

    if _migrate_component_auto_update(component_raw):
        _write_json(component_config_path, component_raw)

    legacy_self = _legacy_self_update_value(config_raw, settings_raw)

    for raw, path in (
        (settings_raw, system_settings_path),
        (config_raw, config_path),
    ):
        if path is None:
            continue
        if _LEGACY_SELF_UPDATE_KEY in raw:
            del raw[_LEGACY_SELF_UPDATE_KEY]
            _write_json(path, raw)

    if legacy_self is not None:
        logger.info(
            "auto-update migration: migrated caretaker_self_update_enabled=%s onto "
            "the central-deploy component's auto_update_enabled flag",
            legacy_self,
        )
    return legacy_self
