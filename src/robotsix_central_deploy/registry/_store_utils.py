"""Shared JSON file persistence helpers for registry stores.

All file-backed registry stores use the same tmp-file-rename pattern
for atomic writes, and the same existence-check for loads.  Extracting
these avoids duplicated boilerplate across ``ConfigYamlStore``,
``EnvStore``, and ``DeployHistoryStore``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


async def async_read_json(path: Path) -> dict[str, Any]:
    """Read *path* as JSON, returning ``{}`` when the file is missing or empty."""
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    data: dict[str, Any] = json.loads(raw)
    return data


async def async_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write *data* as indented JSON via a temporary file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.rename(path)
