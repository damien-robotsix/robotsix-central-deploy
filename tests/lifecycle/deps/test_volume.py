"""Direct tests for volume-path validation and orphan-computation helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from robotsix_central_deploy.lifecycle.backends import NoopBackend
from robotsix_central_deploy.lifecycle.deps.volume import (
    VOLUME_CAT_MAX_BYTES,
    _assert_volume_browsable,
    _compute_orphan_volumes,
    _validate_volume_path,
)
from robotsix_central_deploy.lifecycle.models import DockerDfStats, VolumeStat
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.models import ComponentConfig


class TestValidateVolumePath:
    """Tests for ``_validate_volume_path`` — path-traversal guard."""

    def test_empty_path_returns_empty(self) -> None:
        """Empty string returns empty string."""
        assert _validate_volume_path("") == ""

    def test_dot_returns_empty(self) -> None:
        """'.' normalises to empty string."""
        assert _validate_volume_path(".") == ""

    def test_slash_returns_empty(self) -> None:
        """'/' normalises to empty string."""
        assert _validate_volume_path("/") == ""

    def test_leading_slash_stripped(self) -> None:
        """A leading '/' is stripped."""
        assert _validate_volume_path("/foo") == "foo"

    def test_simple_path(self) -> None:
        """A simple relative path is returned as-is."""
        assert _validate_volume_path("config/config.json") == "config/config.json"

    def test_traversal_dotdot_rejected(self) -> None:
        """'..' in path raises HTTPException 400."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_volume_path("../etc/passwd")
        assert exc_info.value.status_code == 400
        assert "traversal" in exc_info.value.detail.lower()

    def test_traversal_dotdot_mid_path_rejected(self) -> None:
        """'..' embedded mid-path raises HTTPException 400."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_volume_path("foo/../../bar")
        assert exc_info.value.status_code == 400
        assert "traversal" in exc_info.value.detail.lower()

    def test_nul_byte_rejected(self) -> None:
        """NUL byte raises HTTPException 400."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_volume_path("foo\x00bar")
        assert exc_info.value.status_code == 400
        assert "NUL" in exc_info.value.detail

    def test_traversal_encoded_not_rejected(self) -> None:
        """Percent-encoded traversal is NOT rejected (Path doesn't decode it)."""
        # Path treats %2e%2e/ as literal chars, so it passes.
        result = _validate_volume_path("foo/%2e%2e/bar")
        assert result == "foo/%2e%2e/bar"

    def test_double_slash_normalised(self) -> None:
        """Double slashes are normalised to single."""
        result = _validate_volume_path("foo//bar")
        assert result == "foo/bar"


class TestAssertVolumeBrowsable:
    """Tests for ``_assert_volume_browsable``."""

    def test_volume_in_named_volumes_passes(self, tmp_path) -> None:
        """No exception when the volume name is in a component's named_volumes."""
        store = ComponentConfigStore(tmp_path / "config.json")
        store.register(
            ComponentConfig(
                id="mail",
                image="mail:latest",
                container_name="mail",
                named_volumes=["mail-data", "mail-logs"],
            )
        )
        # Should not raise.
        _assert_volume_browsable("mail-data", store)

    def test_volume_not_found_raises_404(self, tmp_path) -> None:
        """HTTPException 404 when volume name is not in any component."""
        store = ComponentConfigStore(tmp_path / "config.json")
        store.register(
            ComponentConfig(
                id="mail",
                image="mail:latest",
                container_name="mail",
                named_volumes=["mail-data"],
            )
        )
        with pytest.raises(HTTPException) as exc_info:
            _assert_volume_browsable("other-volume", store)
        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail.lower()

    def test_no_components_raises_404(self, tmp_path) -> None:
        """HTTPException 404 when the store has no components."""
        store = ComponentConfigStore(tmp_path / "config.json")
        with pytest.raises(HTTPException) as exc_info:
            _assert_volume_browsable("anything", store)
        assert exc_info.value.status_code == 404


class TestComputeOrphanVolumes:
    """Tests for ``_compute_orphan_volumes``."""

    async def test_orphan_volume_returned(self, tmp_path) -> None:
        """A volume not owned by any component and not in-use is returned."""
        backend = NoopBackend()
        backend.disk_df = AsyncMock(
            return_value=DockerDfStats(
                volumes=[
                    VolumeStat(name="orphan-vol", size_bytes=1024, in_use=False),
                ]
            )
        )
        store = ComponentConfigStore(tmp_path / "config.json")
        store.register(
            ComponentConfig(
                id="mail",
                image="mail:latest",
                container_name="mail",
                named_volumes=["mail-data"],
            )
        )

        orphans = await _compute_orphan_volumes(backend, store)
        assert len(orphans) == 1
        assert orphans[0].name == "orphan-vol"

    async def test_owned_volume_excluded(self, tmp_path) -> None:
        """A volume owned by a component is NOT returned."""
        backend = NoopBackend()
        backend.disk_df = AsyncMock(
            return_value=DockerDfStats(
                volumes=[
                    VolumeStat(name="mail-data", size_bytes=2048, in_use=False),
                ]
            )
        )
        store = ComponentConfigStore(tmp_path / "config.json")
        store.register(
            ComponentConfig(
                id="mail",
                image="mail:latest",
                container_name="mail",
                named_volumes=["mail-data"],
            )
        )

        orphans = await _compute_orphan_volumes(backend, store)
        assert len(orphans) == 0

    async def test_in_use_volume_excluded(self, tmp_path) -> None:
        """A volume that is in-use (attached to a container) is excluded."""
        backend = NoopBackend()
        backend.disk_df = AsyncMock(
            return_value=DockerDfStats(
                volumes=[
                    VolumeStat(name="orphan-but-busy", size_bytes=4096, in_use=True),
                ]
            )
        )
        store = ComponentConfigStore(tmp_path / "config.json")
        store.register(
            ComponentConfig(
                id="mail",
                image="mail:latest",
                container_name="mail",
                named_volumes=["mail-data"],
            )
        )

        orphans = await _compute_orphan_volumes(backend, store)
        assert len(orphans) == 0

    async def test_volume_without_name_excluded(self, tmp_path) -> None:
        """A VolumeStat with an empty name is excluded."""
        backend = NoopBackend()
        backend.disk_df = AsyncMock(
            return_value=DockerDfStats(
                volumes=[
                    VolumeStat(name="", size_bytes=512, in_use=False),
                ]
            )
        )
        store = ComponentConfigStore(tmp_path / "config.json")

        orphans = await _compute_orphan_volumes(backend, store)
        assert len(orphans) == 0

    async def test_mixed_volumes(self, tmp_path) -> None:
        """Only orphan, not-in-use, named volumes are returned."""
        backend = NoopBackend()
        backend.disk_df = AsyncMock(
            return_value=DockerDfStats(
                volumes=[
                    VolumeStat(name="orphan-1", size_bytes=100, in_use=False),
                    VolumeStat(name="owned-vol", size_bytes=200, in_use=False),
                    VolumeStat(name="orphan-2", size_bytes=300, in_use=False),
                    VolumeStat(name="busy-vol", size_bytes=400, in_use=True),
                ]
            )
        )
        store = ComponentConfigStore(tmp_path / "config.json")
        store.register(
            ComponentConfig(
                id="mail",
                image="mail:latest",
                container_name="mail",
                named_volumes=["owned-vol"],
            )
        )

        orphans = await _compute_orphan_volumes(backend, store)
        assert len(orphans) == 2
        names = {v.name for v in orphans}
        assert names == {"orphan-1", "orphan-2"}


class TestVolumeCatMaxBytes:
    """Trivial test to lock in the constant value."""

    def test_constant_is_1_mib(self) -> None:
        """VOLUME_CAT_MAX_BYTES is exactly 1 MiB."""
        assert VOLUME_CAT_MAX_BYTES == 1_048_576
