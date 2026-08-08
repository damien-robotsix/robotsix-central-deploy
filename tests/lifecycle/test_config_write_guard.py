"""Tests for the guarded config-volume write.

The deploy plane does not own component configs. A component whose config
file does not load simply crash-loops, so a bad write succeeds and the
breakage surfaces later, somewhere else. These cover the refusal.
"""

from __future__ import annotations

from typing import Any

import pytest

from robotsix_central_deploy.lifecycle._config_utils import (
    ConfigWriteRejected,
    config_schema_violation,
    write_config_to_volume_checked,
)

# The shape that broke chat on 2026-08-08: fleet_auth is declared nested,
# never at the top level, and unknown top-level keys are forbidden.
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "server_port": {"type": "integer"},
        "http_probe": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"fleet_auth": {"type": "object"}},
        },
    },
}


class _FakeBackend:
    def __init__(self) -> None:
        self.writes: list[tuple[str, dict[str, Any]]] = []

    async def write_config_to_volume(self, volume: str, config: dict[str, Any]) -> None:
        self.writes.append((volume, config))


class _FakeSchemaStore:
    def __init__(self, schema: dict[str, Any] | None) -> None:
        self._schema = schema

    async def get_template(self, name: str) -> dict[str, Any] | None:
        return self._schema


# ---------------------------------------------------------------------------
# config_schema_violation
# ---------------------------------------------------------------------------


def test_violation_none_when_valid() -> None:
    assert config_schema_violation(_SCHEMA, {"server_port": 8080}) is None


def test_violation_reports_unknown_top_level_key() -> None:
    msg = config_schema_violation(_SCHEMA, {"fleet_auth": {"auth_hosts": []}})
    assert msg is not None
    assert "fleet_auth" in msg


def test_violation_reports_the_path() -> None:
    msg = config_schema_violation(_SCHEMA, {"http_probe": {"nope": 1}})
    assert msg is not None
    assert "http_probe" in msg


# ---------------------------------------------------------------------------
# write_config_to_volume_checked
# ---------------------------------------------------------------------------


async def test_valid_config_is_written() -> None:
    backend = _FakeBackend()
    await write_config_to_volume_checked(
        backend, _FakeSchemaStore(_SCHEMA), "chat", "chat-config", {"server_port": 1}
    )
    assert backend.writes == [("chat-config", {"server_port": 1})]


async def test_top_level_key_the_component_forbids_is_refused() -> None:
    """The 2026-08-08 regression: this write crash-looped chat 13 times."""
    backend = _FakeBackend()
    with pytest.raises(ConfigWriteRejected) as exc:
        await write_config_to_volume_checked(
            backend,
            _FakeSchemaStore(_SCHEMA),
            "chat",
            "chat-config",
            {"server_port": 1, "fleet_auth": {"auth_hosts": ["a.example.com"]}},
        )
    assert "chat" in str(exc.value)
    assert backend.writes == [], "nothing may be written when validation fails"


async def test_no_stored_schema_writes_unchecked() -> None:
    """Refusing here would break onboarding — there is nothing to check against."""
    backend = _FakeBackend()
    await write_config_to_volume_checked(
        backend, _FakeSchemaStore(None), "new-svc", "vol", {"anything": True}
    )
    assert backend.writes == [("vol", {"anything": True})]
