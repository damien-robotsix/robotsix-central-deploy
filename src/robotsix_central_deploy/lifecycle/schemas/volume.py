"""Volume browser / prune / relocate schemas for lifecycle endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from robotsix_central_deploy.lifecycle.models import VolumeEntryType


class VolumeEntry(BaseModel):
    """One filesystem entry (file or directory) in a volume listing."""

    name: str = Field(description="Entry name relative to the listed directory")
    type: VolumeEntryType = Field(description="Entry type: file or dir")
    size_bytes: int = Field(
        description="Entry size in bytes; for a directory, its recursive size"
    )


class VolumeListResponse(BaseModel):
    """Directory listing returned by GET /volumes/{name}/ls."""

    entries: list[VolumeEntry] = Field(description="Volume entries for the component")


class VolumeFileResponse(BaseModel):
    """File content (or binary/truncation flags) from a volume file read."""

    size_bytes: int = Field(description="Total file size in bytes")
    content: str | None = Field(
        default=None,
        description="File content as UTF-8 text; None when binary or too large",
    )
    binary: bool = Field(
        description="True when the file is detected as binary (not valid UTF-8)"
    )
    truncated: bool = Field(
        description="True when content exceeds the display limit and was cut"
    )


class VolumeFileWriteRequest(BaseModel):
    """Request body for creating or overwriting a file inside a volume."""

    path: str = Field(
        description="File path relative to the volume root (no traversal, no leading '/')"
    )
    content: str = Field(description="File content as a UTF-8 string")
    overwrite: bool = Field(
        default=False,
        description="Replace an existing file; when False an existing path returns 409",
    )


class VolumeFileWriteResponse(BaseModel):
    """Result of a successful volume file write."""

    volume: str = Field(description="Named volume the file was written to")
    path: str = Field(description="Normalised path relative to the volume root")
    size_bytes: int = Field(description="Number of content bytes written")


# ---------------------------------------------------------------------------
# Orphan-volume prune models
# ---------------------------------------------------------------------------


class OrphanVolume(BaseModel):
    """A Docker volume owned by no registered component and not in use."""

    name: str = Field(description="Docker volume name")
    size_bytes: int = Field(
        default=0, description="Disk usage in bytes; 0 when unknown"
    )


class OrphanVolumesResponse(BaseModel):
    """Orphan-volume candidates returned by GET /volumes/orphans."""

    volumes: list[OrphanVolume] = Field(
        default=[], description="List of orphan Docker volume candidates"
    )
    total_bytes: int = Field(
        default=0, description="Sum of all orphan volume sizes in bytes"
    )


class PruneVolumesRequest(BaseModel):
    """Request body for POST /volumes/prune — which orphans to remove."""

    names: list[str] | None = Field(
        default=None,
        description="Volume names to prune; None means prune every orphan candidate",
    )


class PruneVolumesResponse(BaseModel):
    """Per-volume outcome of a POST /volumes/prune run."""

    removed: list[str] = Field(
        default=[], description="Volumes confirmed gone after the prune"
    )
    skipped: list[str] = Field(
        default=[], description="Requested names that were not eligible orphans"
    )
    failed: list[str] = Field(
        default=[],
        description="Eligible orphans that were still present after the prune attempt",
    )
    space_reclaimed_bytes: int = Field(
        default=0, description="Total bytes freed by successfully removed volumes"
    )


# ---------------------------------------------------------------------------
# Volume relocate models
# ---------------------------------------------------------------------------


class RelocateVolumeRequest(BaseModel):
    """Request body for POST /volumes/{name}/relocate."""

    target_disk: str = Field(
        ...,
        min_length=1,
        description=(
            "Target disk identifier — a device path (e.g. /dev/sdb1), "
            "mount point (e.g. /mnt/data), or filesystem label."
        ),
    )


class RelocateVolumeResponse(BaseModel):
    """Outcome of a volume relocation operation."""

    status: Literal["ok"] = Field(description="Relocation outcome")
    detail: str = Field(description="Human-readable status detail")
    volume_name: str = Field(description="The relocated volume name")
    component_id: str = Field(description="The component that owns this volume")
    source_disk: str = Field(
        default="", description="Previous disk location (mount point or 'default')"
    )
    target_disk: str = Field(description="New disk mount point")


# ---------------------------------------------------------------------------
# Config endpoint models
# ---------------------------------------------------------------------------
