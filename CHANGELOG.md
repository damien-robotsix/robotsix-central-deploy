# Changelog

All notable changes to robotsix-central-deploy.

## 0.0.0 (unreleased)

- **Enable `VOLUMES=1` on socket-proxy** — named-volume create/list/inspect/remove
  calls now pass through the Docker socket proxy, in preparation for
  managed-service volume creation in the self-service deploy flow.
- **Dashboard "Up to date" column** — the live dashboard now displays an
  "Up to date" column showing each component's update state as a colored
  badge (green "up to date", amber "update available" with digest tooltip,
  grey "unknown").  Column sits between Health and Actions.
- **Registry image-update detection** — new `registry_check` subpackage with
  `RegistryChecker` that polls the GHCR registry for the latest manifest
  digest and compares against the deployed image digest.
  - `GET /services/{name}` now returns `update_available` (bool),
    `running_digest`, and `latest_digest` fields.
  - `GET /services` list items include `update_available`.
  - New config options: `ROBOTSIX_LIFECYCLE_GHCR_TOKEN`,
    `ROBOTSIX_LIFECYCLE_REGISTRY_CHECK_TTL` (cache seconds, default 300),
    `ROBOTSIX_LIFECYCLE_REGISTRY_CHECK_INTERVAL` (background poll seconds;
    0 disables).
  - `deployed_image_digest` now stores the manifest digest (from
    `RepoDigests`) instead of the image config digest, making it
    comparable to the registry's `Docker-Content-Digest` header.
  - Background task updates all service records periodically when
    `registry_check_interval > 0`; shuts down cleanly on server exit.

- **Added `follow` query param to logs endpoint** — `GET /services/{name}/logs`
  now accepts `follow=bool` (default `false`).  When `follow=true`, the
  `DockerSdkBackend` passes `follow=True` to the Docker SDK and iterates log
  chunks via `run_in_executor` instead of a bare synchronous `for` loop,
  preventing event-loop blocking.  `NoopBackend` and `DockerBackend` stubs
  accept the new parameter.  Client disconnects trigger `log_iter.close()` via
  `asyncio.CancelledError` handling.

- **Removed legacy `auth_username`/`auth_password` fields** from
  `LifecycleConfig` — these env vars were no longer consulted by
  `verify_auth`, which matches passwords against `api_key` alone.
- **Removed dead backward-compat alias** — `verify_api_key = verify_auth`
  alias removed from `src/robotsix_central_deploy/lifecycle/auth.py`.  All
  callers already use `verify_auth` directly.
- **Monitoring UI dashboard** — new `GET /ui` endpoint serves a self-contained
  HTML dashboard at `/ui` showing live component status, image revision (first 12
  chars), health, and start/stop/restart controls.  Auth-gated via `verify_auth`;
  auto-refreshes every 30 s.  Logs column placeholder present for future
  logs-viewer ticket.  No external CDN dependencies (vanilla JS + CSS).
- **Log viewer modal** — clicking "Logs" for any component opens a dark-themed
  modal that streams container logs via `fetch` + `ReadableStream` (no page
  reload).  Shows last 200 lines then live-follows new output.  Close via × button
  or Escape key aborts the fetch cleanly.  Auth credentials included
  (`credentials: 'same-origin'`); 401 errors surface in the status bar.
- **Unified auth** — `verify_auth` now accepts `Authorization: Basic` where the
  password equals `ROBOTSIX_LIFECYCLE_API_KEY` (username is ignored).  Added
  `verify_api_key = verify_auth` alias for backward compatibility.  New private
  helper `_decode_basic_auth`.  Realm changed to `"Robotsix Central Deploy"`.
  Existing `X-API-Key` header path unchanged.

- **Docker socket proxy** — added `docker_socket_url` config field (`ROBOTSIX_LIFECYCLE_DOCKER_SOCKET_URL`, default `unix:///var/run/docker.sock`) and a `docker-compose.yml` with `tecnativa/docker-socket-proxy` sidecar scoped to `CONTAINERS`, `POST`, `DELETE`, and `IMAGES` API paths. Raw socket mounted read-only into proxy only; central-deploy talks TCP.
- **Logs streaming endpoint** — `GET /services/{name}/logs` returns container
  logs as `text/plain` (auth-gated).  Supports `tail` (1–10000, default 100)
  and `since` (ISO 8601 or Unix timestamp) query parameters.  Implemented for
  `DockerSdkBackend` (real logs via Docker SDK), `NoopBackend` (stub), and
  `DockerBackend` (CLI stub).
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
