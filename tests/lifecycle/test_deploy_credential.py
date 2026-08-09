"""Tests for deploy-plane credential provisioning.

The credential goes into the component's own config file, never into its
environment — an env-injected copy was both a config-standard violation and
unread by every component it reached.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from robotsix_central_deploy.lifecycle._deploy_credential import (
    _rebuild_deploy_credential,
    reconcile_deploy_credential,
)
from robotsix_central_deploy.registry.models import ComponentConfig

KEY = "deploy-api-key"


def _component(
    id: str,
    *,
    chat_agent_mutatable: bool = False,
    allow_chat_access: bool = False,
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
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self._read_error = read_error
        self._write_error = write_error

    async def read_config_from_volume(self, volume: str) -> Any:
        if self._read_error is not None:
            raise self._read_error
        return self.volumes.get(volume, {})

    async def write_config_to_volume(self, volume: str, config: dict[str, Any]) -> None:
        if self._write_error is not None:
            raise self._write_error
        self.writes.append((volume, config))
        self.volumes[volume] = config


class _FakeSchemaStore:
    def __init__(self, schema: dict[str, Any] | None = None) -> None:
        self._schema = schema

    async def get_template(self, name: str) -> dict[str, Any] | None:
        return self._schema


def _token(backend: _FakeBackend, volume: str) -> str:
    return backend.volumes[volume]["central_deploy"]["api_token"]


# ---------------------------------------------------------------------------
# _rebuild_deploy_credential
# ---------------------------------------------------------------------------


async def test_token_is_written_to_chat_agent_components() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config")
    )
    backend = _FakeBackend()

    await _rebuild_deploy_credential(store, backend, KEY)

    assert _token(backend, "chat-config") == KEY


async def test_components_that_merely_allow_chat_access_are_left_alone() -> None:
    """The key is for components that *call* the plane, not ones chat calls."""
    store = _FakeStore(
        _component("invest", allow_chat_access=True, config_volume="invest-config"),
        _component("mill", allow_chat_access=True, config_volume="mill-config"),
    )
    backend = _FakeBackend()

    await _rebuild_deploy_credential(store, backend, KEY)

    assert backend.writes == []


async def test_surrounding_config_survives_the_write() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config")
    )
    backend = _FakeBackend(
        {
            "chat-config": {
                "central_deploy": {
                    "url": "http://deploy:8100",
                    "roster_cache_ttl": 300,
                },
                "server_port": 8000,
            }
        }
    )

    await _rebuild_deploy_credential(store, backend, KEY)

    written = backend.volumes["chat-config"]
    assert written["central_deploy"]["url"] == "http://deploy:8100"
    assert written["central_deploy"]["roster_cache_ttl"] == 300
    assert written["server_port"] == 8000
    assert written["central_deploy"]["api_token"] == KEY


async def test_no_write_when_the_token_already_matches() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config")
    )
    backend = _FakeBackend({"chat-config": {"central_deploy": {"api_token": KEY}}})

    await _rebuild_deploy_credential(store, backend, KEY)

    assert backend.writes == [], "this runs on every startup — it must no-op"


async def test_a_rotated_key_replaces_the_stored_one() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config")
    )
    backend = _FakeBackend({"chat-config": {"central_deploy": {"api_token": "old"}}})

    await _rebuild_deploy_credential(store, backend, KEY)

    assert _token(backend, "chat-config") == KEY


async def test_empty_api_key_writes_nothing() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config")
    )
    backend = _FakeBackend()

    await _rebuild_deploy_credential(store, backend, "")

    assert backend.writes == []


async def test_component_without_a_config_volume_is_skipped() -> None:
    """central-deploy is chat-agent-flagged and has nowhere to write."""
    store = _FakeStore(_component("central-deploy", chat_agent_mutatable=True))
    backend = _FakeBackend()

    await _rebuild_deploy_credential(store, backend, KEY)

    assert backend.writes == []


async def test_malformed_block_is_replaced() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config")
    )
    backend = _FakeBackend({"chat-config": {"central_deploy": "not-a-mapping"}})

    await _rebuild_deploy_credential(store, backend, KEY)

    assert _token(backend, "chat-config") == KEY


async def test_unreadable_volume_is_treated_as_empty() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config")
    )
    backend = _FakeBackend(read_error=RuntimeError("volume gone"))

    await _rebuild_deploy_credential(store, backend, KEY)

    assert backend.writes == [("chat-config", {"central_deploy": {"api_token": KEY}})]


async def test_write_failure_does_not_stop_the_other_components() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config"),
        _component("other", chat_agent_mutatable=True, config_volume="other-config"),
    )
    backend = _FakeBackend(write_error=RuntimeError("read-only"))

    await _rebuild_deploy_credential(store, backend, KEY)


async def test_component_whose_schema_rejects_the_key_is_skipped() -> None:
    """Keeps the engine generic: fill the block only where it is declared."""
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config")
    )
    backend = _FakeBackend()
    schema_store = _FakeSchemaStore(
        {"type": "object", "additionalProperties": False, "properties": {}}
    )

    await _rebuild_deploy_credential(store, backend, KEY, schema_store)

    assert backend.writes == []


async def test_declared_schema_takes_the_checked_write() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config")
    )
    backend = _FakeBackend()
    schema_store = _FakeSchemaStore(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"central_deploy": {"type": "object"}},
        }
    )

    await _rebuild_deploy_credential(store, backend, KEY, schema_store)

    assert _token(backend, "chat-config") == KEY


# ---------------------------------------------------------------------------
# reconcile_deploy_credential — the route/startup wrapper
# ---------------------------------------------------------------------------


class _FakeSecret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


async def test_wrapper_provisions_from_request_state() -> None:
    store = _FakeStore(
        _component("chat", chat_agent_mutatable=True, config_volume="chat-config")
    )
    backend = _FakeBackend()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(api_key=_FakeSecret(KEY)),
                backend=backend,
                config_yaml_store=None,
            )
        )
    )

    await reconcile_deploy_credential(store, request)

    assert _token(backend, "chat-config") == KEY


async def test_wrapper_swallows_failures() -> None:
    """Its callers are toggle routes and startup — neither may fail on this."""
    broken = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    await reconcile_deploy_credential(_FakeStore(), broken)
