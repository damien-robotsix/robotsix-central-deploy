# Changelog

All notable changes to robotsix-central-deploy.

## 0.0.0 (unreleased)

- **HTTP Basic Auth support** — `verify_auth` dependency now accepts either
  `X-API-Key` header or `Authorization: Basic` credentials. New config fields
  `auth_username` / `auth_password` (env: `ROBOTSIX_LIFECYCLE_AUTH_USERNAME`,
  `ROBOTSIX_LIFECYCLE_AUTH_PASSWORD`). Auth failures return `401` with
  `WWW-Authenticate: Basic realm="Central Deploy"` to trigger browser login
  dialogs. `GET /services` and `GET /services/{name}` are now authenticated
  when credentials are configured. `GET /health` remains open.
- **Component registry** — Pydantic models (`ComponentConfig`, `PortMapping`,
  `VolumeMount`, `HealthCheck`) and a YAML loader (`ComponentRegistry.from_yaml`)
  that declares every managed Docker component in a single source of truth.
- **Seed configuration** (`config/components.yaml`) — stub entries for the six
  current server services: cost-monitor, calendar-agent, auto-mail, chat, broker
  (agent-comm), and radicale.
- **Lifecycle config** — added `registry_path` field and `effective_registry_path`
  property to `LifecycleConfig`, overridable via `ROBOTSIX_LIFECYCLE_REGISTRY_PATH`.

- **Docker SDK backend** — new `DockerSdkBackend` talks to the Docker daemon
  directly via the Python Docker SDK (no CLI subprocess).  `status()` returns a
  `ComponentInspect` with image revision and health; `start`/`stop`/`restart`
  use the SDK's container API.  The abstract `ExecutionBackend.status()` now
  returns `ComponentInspect` and the old `DockerBackend` was updated to match.
- Enable periodic analysis workflows: audit, health, test_gap,
  module_curator, completeness_check, copy_paste, state_sync.

## [0.1.0] — 2025-06-25

### Added

- **Lifecycle control API** — REST server for managing suite services.
  - `POST /services/{name}/start` — start a service (idempotent).
  - `POST /services/{name}/stop` — stop a service (idempotent).
  - `POST /services/{name}/restart` — restart a service (idempotent).
  - `GET /services/{name}` — full status for one service.
  - `GET /services` — list all managed services with current state.
  - `GET /health` — liveness probe.
- **Service state machine** — seven states (stopped, starting, running,
  stopping, restarting, failed, unknown) with formal transition rules and
  idempotent operations.
- **Pluggable execution backend** — abstract `ExecutionBackend` with a
  `DockerBackend` (subprocess-driven) and a `NoopBackend` for testing.
- **Persistence layer** — `InMemoryStore` (ephemeral) and `FileStore`
  (YAML-backed, survives restarts).
- **Bearer-token auth guard** — `X-API-Key` header validated against
  `ROBOTSIX_LIFECYCLE_API_KEY`; mutating endpoints are gated, dev mode
  when no key is set.
- **CLI entrypoint** — `robotsix-lifecycle` with `--host`, `--port`,
  `--store-backend`, `--execution-backend`, `--api-key` flags.
- **Test suite** — 78 tests covering models, state machine, store
  implementations, backend, server endpoints, and auth.
- **OpenAPI 3.0 docs** — auto-generated at `/docs` and `/openapi.json`.
