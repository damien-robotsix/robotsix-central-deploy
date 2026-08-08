"""Tests for fleet-auth hostname reconciliation.

The reconciler writes into a *component's* config volume, so its two
interesting properties are what it puts there (auto-managed gateway hosts,
without clobbering operator-managed ones) and how quietly it gives up when
the volume, the schema, or the backend says no — a raise here would fail a
toggle route or abort startup.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from robotsix_central_deploy.lifecycle._fleet_auth import (
    _rebuild_fleet_auth_hosts,
    reconcile_fleet_auth_hosts,
)
from robotsix_central_deploy.registry.models import ComponentConfig

DOMAIN = "deploy.example.com"


def _component(
    id: str,
    *,
    allow_chat_access: bool = False,
    chat_agent_mutatable: bool = False,
    config_volume: str | None = None,
) -> ComponentConfig:
    return ComponentConfig(
        id=id,
        image=f"ghcr.io/example/{id}:main",
        container_name=id,
        allow_chat_access=allow_chat_access,
        chat_agent_mutatable=chat_agent_mutatable,
        config_volume=config_volume,
    )


class _FakeStore:
    """Stands in for ``ComponentConfigStore`` — only ``all()`` is used."""

    def __init__(self, *components: ComponentConfig) -> None:
        self._components = list(components)

    def all(self) -> list[ComponentConfig]:
        return list(self._components)


class _FakeBackend:
    def __init__(
        self,
        volumes: dict[str, Any] | None = None,
        *,
        read_error: Exception | None = None,
        write_error: Exception | None = None,
    ) -> None:
        self.volumes: dict[str, Any] = volumes or {}
        self.reads: list[str] = []
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self._read_error = read_error
        self._write_error = write_error

    async def read_config_from_volume(self, volume: str) -> Any:
        self.reads.append(volume)
        if self._read_error is not None:
            raise self._read_error
        return self.volumes.get(volume, {})

    async def write_config_to_volume(self, volume: str, config: dict[str, Any]) -> None:
        if self._write_error is not None:
            raise self._write_error
        self.writes.append((volume, config))
        self.volumes[volume] = config


class _FakeSchemaStore:
    """Stands in for ``ConfigYamlStore`` in the checked-write path."""

    def __init__(self, schema: dict[str, Any] | None = None) -> None:
        self._schema = schema

    async def get_template(self, name: str) -> dict[str, Any] | None:
        return self._schema


def _hosts(backend: _FakeBackend, volume: str) -> list[str]:
    return backend.volumes[volume]["fleet_auth"]["auth_hosts"]


# ---------------------------------------------------------------------------
# _rebuild_fleet_auth_hosts — what lands in the volume
# ---------------------------------------------------------------------------


async def test_hosts_are_built_from_every_chat_accessible_component() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
        _component("invest", allow_chat_access=True),
        _component("private"),
    )
    backend = _FakeBackend()

    await _rebuild_fleet_auth_hosts(store, backend, DOMAIN)

    assert _hosts(backend, "chat-config") == [
        f"chat.{DOMAIN}",
        f"invest.{DOMAIN}",
    ], "both toggles grant a host; an untoggled component gets none"


async def test_manual_entries_are_preserved_ahead_of_managed_ones() -> None:
    """Anything outside the gateway domain is operator-managed — never dropped."""
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
    )
    backend = _FakeBackend(
        {
            "chat-config": {
                "fleet_auth": {"auth_hosts": ["intranet.corp.example", "old.host"]},
                "server_port": 8000,
            }
        }
    )

    await _rebuild_fleet_auth_hosts(store, backend, DOMAIN)

    assert _hosts(backend, "chat-config") == [
        "intranet.corp.example",
        "old.host",
        f"chat.{DOMAIN}",
    ]
    assert backend.volumes["chat-config"]["server_port"] == 8000, (
        "the rest of the component's config must survive the write"
    )


async def test_stale_gateway_host_is_removed_when_the_toggle_goes_off() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
        _component("invest"),  # access revoked since the last reconcile
    )
    backend = _FakeBackend(
        {
            "chat-config": {
                "fleet_auth": {
                    "auth_hosts": [f"chat.{DOMAIN}", f"invest.{DOMAIN}"],
                }
            }
        }
    )

    await _rebuild_fleet_auth_hosts(store, backend, DOMAIN)

    assert _hosts(backend, "chat-config") == [f"chat.{DOMAIN}"]


async def test_no_write_when_the_host_list_already_matches() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
    )
    backend = _FakeBackend(
        {"chat-config": {"fleet_auth": {"auth_hosts": [f"chat.{DOMAIN}"]}}}
    )

    await _rebuild_fleet_auth_hosts(store, backend, DOMAIN)

    assert backend.writes == [], "reconciliation runs on every startup — it must no-op"


async def test_repeated_runs_do_not_duplicate_entries() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
    )
    backend = _FakeBackend({"chat-config": {"fleet_auth": {"auth_hosts": ["manual"]}}})

    await _rebuild_fleet_auth_hosts(store, backend, DOMAIN)
    await _rebuild_fleet_auth_hosts(store, backend, DOMAIN)

    assert len(backend.writes) == 1
    assert _hosts(backend, "chat-config") == ["manual", f"chat.{DOMAIN}"]


async def test_only_chat_agent_components_are_written_to() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
        _component("invest", allow_chat_access=True, config_volume="invest-config"),
    )
    backend = _FakeBackend()

    await _rebuild_fleet_auth_hosts(store, backend, DOMAIN)

    assert [v for v, _ in backend.writes] == ["chat-config"]


async def test_malformed_existing_config_is_replaced_not_trusted() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
    )
    backend = _FakeBackend(
        {"chat-config": {"fleet_auth": {"auth_hosts": "not-a-list"}}}
    )

    await _rebuild_fleet_auth_hosts(store, backend, DOMAIN)

    assert _hosts(backend, "chat-config") == [f"chat.{DOMAIN}"]


async def test_non_dict_volume_document_does_not_crash() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
    )
    backend = _FakeBackend({"chat-config": ["not", "a", "mapping"]})

    await _rebuild_fleet_auth_hosts(store, backend, DOMAIN)

    assert _hosts(backend, "chat-config") == [f"chat.{DOMAIN}"]


# ---------------------------------------------------------------------------
# _rebuild_fleet_auth_hosts — degrading quietly
# ---------------------------------------------------------------------------


async def test_missing_gateway_domain_skips_everything() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
    )
    backend = _FakeBackend()

    await _rebuild_fleet_auth_hosts(store, backend, "")

    assert backend.reads == []
    assert backend.writes == []


async def test_component_without_a_config_volume_is_not_written() -> None:
    """central-deploy itself is such a component — it has nowhere to write."""
    store = _FakeStore(_component("deploy", chat_agent_mutatable=True))
    backend = _FakeBackend()

    await _rebuild_fleet_auth_hosts(store, backend, DOMAIN)

    assert backend.reads == []
    assert backend.writes == []


async def test_unreadable_volume_is_treated_as_empty_config() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
    )
    backend = _FakeBackend(read_error=RuntimeError("volume gone"))

    await _rebuild_fleet_auth_hosts(store, backend, DOMAIN)

    assert backend.writes == [
        ("chat-config", {"fleet_auth": {"auth_hosts": [f"chat.{DOMAIN}"]}})
    ]


async def test_write_failure_does_not_propagate() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
        _component("other", chat_agent_mutatable=True, config_volume="other-config"),
    )
    backend = _FakeBackend(write_error=RuntimeError("volume read-only"))

    await _rebuild_fleet_auth_hosts(store, backend, DOMAIN)

    assert backend.reads == ["chat-config", "other-config"], (
        "a failed write must not abort the remaining components"
    )


async def test_schema_rejection_leaves_the_volume_untouched() -> None:
    """The 2026-08-08 crash loop: writing a key the component forbids."""
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
    )
    backend = _FakeBackend()
    schema_store = _FakeSchemaStore(
        {"type": "object", "additionalProperties": False, "properties": {}}
    )

    await _rebuild_fleet_auth_hosts(store, backend, DOMAIN, schema_store)

    assert backend.writes == []


async def test_checked_write_is_used_when_a_schema_store_is_given() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
    )
    backend = _FakeBackend()
    schema_store = _FakeSchemaStore(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"fleet_auth": {"type": "object"}},
        }
    )

    await _rebuild_fleet_auth_hosts(store, backend, DOMAIN, schema_store)

    assert _hosts(backend, "chat-config") == [f"chat.{DOMAIN}"]


# ---------------------------------------------------------------------------
# reconcile_fleet_auth_hosts — the route/startup wrapper
# ---------------------------------------------------------------------------


def _request(backend: Any, *, domain: str = DOMAIN, yaml_store: Any = None) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(gateway_base_domain=domain),
                backend=backend,
                config_yaml_store=yaml_store,
            )
        )
    )


async def test_wrapper_reconciles_from_request_state() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
    )
    backend = _FakeBackend()

    await reconcile_fleet_auth_hosts(store, _request(backend))

    assert _hosts(backend, "chat-config") == [f"chat.{DOMAIN}"]


async def test_wrapper_swallows_failures() -> None:
    """Callers are toggle routes and startup — neither may fail on this."""
    broken = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    await reconcile_fleet_auth_hosts(_FakeStore(), broken)


async def test_wrapper_tolerates_a_config_without_a_gateway_domain() -> None:
    backend = _FakeBackend()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(),
                backend=backend,
                config_yaml_store=None,
            )
        )
    )

    await reconcile_fleet_auth_hosts(
        _FakeStore(
            _component("chat", chat_agent_mutatable=True, config_volume="chat-config")
        ),
        request,
    )

    assert backend.writes == []
