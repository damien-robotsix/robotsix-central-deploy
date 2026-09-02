"""Volume path utilities — validation, browsability checks, orphan computation."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from ...registry.config_store import ComponentConfigStore
from ..backends import ExecutionBackend
from ..models import VolumeStat

#: Maximum bytes returned by ``GET /volumes/{name}/cat`` (1 MiB).
VOLUME_CAT_MAX_BYTES: int = 1_048_576


def _validate_volume_path(rel_path: str) -> str:
    """Normalise and validate a volume-relative path.

    Returns the normalised form (leading ``/`` stripped, ``.`` → ``""``).
    Raises ``HTTPException(400)`` on traversal / NUL.
    """
    if "\x00" in rel_path:
        raise HTTPException(status_code=400, detail="Path contains NUL byte")
    # Strip a single leading slash so callers can pass "/" or "/foo".
    rel_path = rel_path.removeprefix("/")
    # Collapse to a clean relative path.
    norm = str(Path(rel_path))
    if norm == ".":
        norm = ""
    if ".." in Path(norm).parts:
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    return norm


def _assert_volume_browsable(name: str, store: ComponentConfigStore) -> None:
    """Raise 404 if *name* is not in any component's ``named_volumes``."""
    allowed: set[str] = set()
    for cfg in store.all():
        allowed.update(cfg.named_volumes)
    if name not in allowed:
        raise HTTPException(
            status_code=404,
            detail=f"Volume '{name}' not found or not browsable",
        )


async def _compute_orphan_volumes(
    backend: ExecutionBackend, store: ComponentConfigStore
) -> list[VolumeStat]:
    """Return Docker volumes safe to prune: owned by no registered component
    AND not currently attached to any container.

    A volume declared in some component's ``named_volumes`` is deliberately
    excluded even when the component is stopped — its data must survive. A
    volume attached to a container (``in_use``) is excluded because Docker
    would refuse to remove it anyway and it is clearly still needed.
    """
    owned: set[str] = set()
    for cfg in store.all():
        owned.update(cfg.named_volumes)
    # The claude-auth volume is owned by the engine itself (OAuth credential
    # + SDK transcripts) and is only mounted transiently, so it shows up as
    # unattached — without this guard a blanket POST /volumes/prune would
    # delete the fleet's Claude subscription credential (observed listed as
    # a prune-safe orphan on 2026-09-02).
    from ..models import CLAUDE_AUTH_VOLUME

    owned.add(CLAUDE_AUTH_VOLUME)
    df = await backend.disk_df()
    return [v for v in df.volumes if v.name and v.name not in owned and not v.in_use]
