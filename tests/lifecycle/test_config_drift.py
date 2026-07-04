"""Tests for config drift detection and guarded Save."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.lifecycle.backends import NoopBackend
from robotsix_central_deploy.lifecycle.deps import _canonical_hash
from robotsix_central_deploy.registry.models import ComponentConfig


class TrackingInMemoryBackend(NoopBackend):
    """NoopBackend variant that stores config writes so tests can mutate volumes."""

    def __init__(self) -> None:
        super().__init__()
        self._volumes: dict[str, dict[str, Any]] = {}

    async def write_config_to_volume(
        self, volume_name: str, config_dict: dict[str, Any]
    ) -> None:
        self._volumes[volume_name] = copy.deepcopy(config_dict)

    async def read_config_from_volume(self, volume_name: str) -> dict[str, Any]:
        return dict(self._volumes.get(volume_name, {}))


@pytest.fixture
def tracking_backend() -> TrackingInMemoryBackend:
    return TrackingInMemoryBackend()


@pytest.fixture
async def client_with_component(
    tracking_backend: TrackingInMemoryBackend,
) -> AsyncClient:
    """Set up a test app with a component that has a config schema and volume."""
    from robotsix_central_deploy.lifecycle.models import ServiceRecord

    store = server_mod.app.state.store
    config_yaml_store = server_mod.app.state.config_yaml_store
    component_config_store = server_mod.app.state.component_config_store

    # Override the backend
    server_mod.app.state.backend = tracking_backend
    server_mod._backend = tracking_backend

    # Register a component with config schema + volume
    comp = ComponentConfig(
        id="test-comp",
        image="ghcr.io/org/test:latest",
        container_name="test-comp",
        has_config_yaml=True,
        config_volume="test-comp-config",
    )
    await component_config_store.put(comp)

    # Create a ServiceRecord so _get_or_create_record succeeds
    await store.put(ServiceRecord(name="test-comp", image="ghcr.io/org/test:latest"))

    # Store a template
    template: dict = {
        "type": "object",
        "properties": {
            "host": {"type": "string"},
            "port": {"type": "integer"},
            "password": {"type": "string", "format": "password", "writeOnly": True},
        },
    }
    await config_yaml_store.save_template("test-comp", template)

    transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


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


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hash_recorded_on_save(client_with_component: AsyncClient) -> None:
    """PUT config → config_yaml_store.get_volume_hash returns a non-None hex string."""
    headers = {"X-API-Key": "test-key"}
    resp = await client_with_component.put(
        "/services/test-comp/config",
        json={"values": {"host": "newhost", "port": 9999}},
        headers=headers,
    )
    assert resp.status_code == 204

    config_yaml_store = server_mod.app.state.config_yaml_store
    h = await config_yaml_store.get_volume_hash("test-comp")
    assert h is not None
    assert len(h) == 64  # SHA-256 hex digest


@pytest.mark.asyncio
async def test_no_drift_after_save(client_with_component: AsyncClient) -> None:
    """PUT then GET → drift: false."""
    headers = {"X-API-Key": "test-key"}
    await client_with_component.put(
        "/services/test-comp/config",
        json={"values": {"host": "newhost"}},
        headers=headers,
    )
    resp = await client_with_component.get(
        "/services/test-comp/config", headers=headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["drift"] is False


@pytest.mark.asyncio
async def test_drift_detected_on_out_of_band_edit(
    client_with_component: AsyncClient,
    tracking_backend: TrackingInMemoryBackend,
) -> None:
    """PUT, mutate volume out-of-band, GET → drift: true."""
    headers = {"X-API-Key": "test-key"}
    await client_with_component.put(
        "/services/test-comp/config",
        json={"values": {"host": "newhost"}},
        headers=headers,
    )
    # Mutate the volume out-of-band
    tracking_backend._volumes["test-comp-config"]["host"] = "oob-edit"

    resp = await client_with_component.get(
        "/services/test-comp/config", headers=headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["drift"] is True


@pytest.mark.asyncio
async def test_save_blocked_on_drift(
    client_with_component: AsyncClient,
    tracking_backend: TrackingInMemoryBackend,
) -> None:
    """PUT (initial), mutate volume, PUT (no force_overwrite) → 409 with diff."""
    headers = {"X-API-Key": "test-key"}
    await client_with_component.put(
        "/services/test-comp/config",
        json={"values": {"host": "newhost", "password": "s3cret"}},
        headers=headers,
    )
    # Mutate volume out-of-band
    tracking_backend._volumes["test-comp-config"]["host"] = "oob-edit"

    resp = await client_with_component.put(
        "/services/test-comp/config",
        json={"values": {"host": "another"}},
        headers=headers,
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["drift"] is True
    assert "live_config" in body
    assert "stored_config" in body
    # Secret fields should be masked
    assert body["live_config"]["password"] == "***"
    assert body["stored_config"]["password"] == "***"


@pytest.mark.asyncio
async def test_force_overwrite_bypasses_guard(
    client_with_component: AsyncClient,
    tracking_backend: TrackingInMemoryBackend,
) -> None:
    """Drift present, PUT with force_overwrite: true → 204; GET → drift: false."""
    headers = {"X-API-Key": "test-key"}
    await client_with_component.put(
        "/services/test-comp/config",
        json={"values": {"host": "newhost"}},
        headers=headers,
    )
    # Mutate volume out-of-band
    tracking_backend._volumes["test-comp-config"]["host"] = "oob-edit"

    # Force overwrite
    resp = await client_with_component.put(
        "/services/test-comp/config",
        json={"values": {"host": "forced"}, "force_overwrite": True},
        headers=headers,
    )
    assert resp.status_code == 204

    # Drift should be cleared
    resp2 = await client_with_component.get(
        "/services/test-comp/config", headers=headers
    )
    assert resp2.status_code == 200
    assert resp2.json()["drift"] is False


@pytest.mark.asyncio
async def test_import_clears_drift(
    client_with_component: AsyncClient,
    tracking_backend: TrackingInMemoryBackend,
) -> None:
    """Drift present, POST /import → 200 with masked current + hash; GET → drift: false."""
    headers = {"X-API-Key": "test-key"}
    await client_with_component.put(
        "/services/test-comp/config",
        json={"values": {"host": "newhost", "password": "s3cret"}},
        headers=headers,
    )
    # Mutate volume out-of-band
    tracking_backend._volumes["test-comp-config"]["host"] = "oob-edit"
    tracking_backend._volumes["test-comp-config"]["password"] = "new-secret"

    # Import
    resp = await client_with_component.post(
        "/services/test-comp/config/import", headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "current" in body
    assert "volume_hash" in body
    assert len(body["volume_hash"]) == 64
    # Secret masked in response
    assert body["current"]["password"] == "***"
    # Live value should be reflected (with masking)
    assert body["current"]["host"] == "oob-edit"

    # Subsequent GET → no drift
    resp2 = await client_with_component.get(
        "/services/test-comp/config", headers=headers
    )
    assert resp2.status_code == 200
    assert resp2.json()["drift"] is False


@pytest.mark.asyncio
async def test_no_hash_no_block(client_with_component: AsyncClient) -> None:
    """Component with no volume_hash in store → PUT proceeds normally (204), hash is set."""
    headers = {"X-API-Key": "test-key"}
    # This is the first PUT, so no hash yet — PUT should succeed
    resp = await client_with_component.put(
        "/services/test-comp/config",
        json={"values": {"host": "first-save"}},
        headers=headers,
    )
    assert resp.status_code == 204

    # Hash should now be set
    config_yaml_store = server_mod.app.state.config_yaml_store
    h = await config_yaml_store.get_volume_hash("test-comp")
    assert h is not None
