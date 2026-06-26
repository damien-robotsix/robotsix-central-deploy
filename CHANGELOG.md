# Changelog

All notable changes to robotsix-central-deploy.

## 0.0.0 (unreleased)

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
