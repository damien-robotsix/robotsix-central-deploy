# Component Registry

The registry module (`src/robotsix_central_deploy/registry/`) is the persistence
and in-memory indexing layer for managed Docker components. Every component
declaration, environment override, config value, deploy history entry, and chat
audit record flows through the stores in this module.

## Architecture

```
models.py  (Pydantic schemas)
   │
   ├── loader.py → ComponentRegistry   (static YAML, in-memory index)
   ├── config_store.py → ComponentConfigStore   (dynamic JSON, onboarded entries)
   ├── settings_store.py → SystemSettingsStore  (global operator settings)
   │
   ├── secret_key.py → SecretKeyManager   (Fernet encryption for secrets)
   │
   └── _store_utils.py → JsonFileStore   (atomic write + asyncio.Lock base)
          ├── env_store.py → EnvStore
          ├── config_yaml_store.py → ConfigYamlStore
          ├── deploy_history_store.py → DeployHistoryStore
          └── chat_agent_audit_store.py → ChatAgentAuditStore
```

## Key Models

**`models.py`** defines the core Pydantic data types:

- **`ComponentConfig`** — the central component declaration: `id`, `image`,
  `container_name`, `ports`, `mounts`, `env`, `health_check`, `claude_mount`,
  `named_volumes`, and `siblings` (list of `ServiceConfig` for multi-service
  components).
- **`ServiceConfig`** — a sibling service within a multi-service component,
  sharing the same image but with its own ports, env, and container name.
- **`PortMapping`** — `host:container` port binding with optional protocol.
- **`VolumeMount`** — host path or named volume mapped to a container path,
  optionally read-only.
- **`HealthCheck`** — mirrors Docker's health-check specification: test
  command, interval, timeout, retries, start period.

## Component Index

**`loader.py`** provides `ComponentRegistry`, an in-memory dictionary index of
`ComponentConfig` objects. It is populated at startup via `from_yaml(path)`,
which loads and validates a static YAML component manifest. On validation
failure it raises `RegistryLoadError`.

**`config_store.py`** provides `ComponentConfigStore`, the JSON-file-backed
store for dynamically onboarded components. Entries are added during the
two-phase onboarding pipeline (`/onboard/preflight` → `/onboard/confirm`)
and consumed by the Docker execution backend at deploy/start time. A
synchronous `register()` bootstrap path runs before the async server loop.

## Store Layer

All per-component stores extend **`JsonFileStore`** (`_store_utils.py`), which
provides atomic writes (write to tmp file, then rename) guarded by an
`asyncio.Lock`.

| Store | Purpose | Key methods |
|-------|---------|-------------|
| `EnvStore` | Per-component env overrides and encrypted secrets | `get(name)`, `upsert(name, env, secrets)`, `delete_key(name, key)` |
| `ConfigYamlStore` | Per-component `config.yaml` schema + current values + rollback snapshots | `get_current(name)`, `update_current(name, vals)`, `save_previous(name)`, `get_previous(name)` |
| `DeployHistoryStore` | Per-component deploy history (most-recent-first, capped at 20) | `append(name, entry)`, `list(name)` |
| `ChatAgentAuditStore` | Global audit log for chat-agent mutations (capped at 200) | `append(entry)`, `list(limit, component)` |
| `SystemSettingsStore` | Operator-configurable global settings | `get()`, `put(settings)`, `overlay(lifecycle_config)` |

## Secrets

**`secret_key.py`** provides `SecretKeyManager`, a Fernet-based symmetric
encryption helper for secrets. On first boot it auto-generates a key file;
subsequent boots load the existing key. `EnvStore` uses this to encrypt
secret values before persisting them to disk and to decrypt them when
building the merged environment for a container.

> **Warning:** Fernet key loss is irrecoverable — all stored secrets must
> be re-entered if `secrets.key` is deleted.
