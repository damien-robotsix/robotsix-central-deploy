"""Tests for config drift detection and guarded Save."""

from __future__ import annotations

from typing import Any

from robotsix_central_deploy.lifecycle._config_utils import _canonical_hash


# ---------------------------------------------------------------------------
# _canonical_hash stability
# ---------------------------------------------------------------------------


def test_canonical_hash_stable() -> None:
    """Same content, different insertion order → same hash."""
    d1: dict[str, Any] = {"a": 1, "b": 2, "c": {"x": 10, "y": 20}}
    d2: dict[str, Any] = {"c": {"y": 20, "x": 10}, "b": 2, "a": 1}
    assert _canonical_hash(d1) == _canonical_hash(d2)

    # Different content → different hash
    d3: dict[str, Any] = {"a": 1, "b": 3}
    assert _canonical_hash(d1) != _canonical_hash(d3)
