"""Tests for traefik-dynamic mountpoint preparation at startup."""

from __future__ import annotations

from pathlib import Path

from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.deps.lifespan import (
    _ensure_traefik_dynamic_mountpoints,
)


def test_creates_fleet_and_host_mountpoints(tmp_path: Path) -> None:
    """On a fresh install the shared volume is empty; the fleet/ and host/
    mountpoint dirs must exist or Traefik's ro top-level mount fails to
    start (edge outage, 2026-09-04)."""
    cfg = LifecycleConfig(traefik_dynamic_dir=str(tmp_path))
    _ensure_traefik_dynamic_mountpoints(cfg)
    assert (tmp_path / "fleet").is_dir()
    assert (tmp_path / "host").is_dir()
    # Idempotent on an already-prepared volume.
    _ensure_traefik_dynamic_mountpoints(cfg)


def test_noop_when_volume_not_mounted(tmp_path: Path) -> None:
    cfg = LifecycleConfig(traefik_dynamic_dir=str(tmp_path / "absent"))
    _ensure_traefik_dynamic_mountpoints(cfg)
    assert not (tmp_path / "absent").exists()
