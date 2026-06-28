# Changelog

All notable changes to robotsix-central-deploy.

## 0.0.0 (unreleased)

- **Document 16 missing lifecycle environment variables** — added
  `Auth`, `Docker`, `Disk`, `Registry`, `Logging`, `Gateway`, and
  `Claude Integration` sections to `docs/configuration.md`, and extended
  the `Persistence` table with store-path variables. All 22
  `ROBOTSIX_LIFECYCLE_*` env vars defined in `LifecycleConfig` are now
  documented.
- **Enable `env_doc_sync` periodic workflow** — added
  `.robotsix-mill/periodic/env_doc_sync.yaml` stub so that the
  `env_doc_sync` periodic workflow cross-references Pydantic Settings
  env vars against `docs/configuration.md` and files draft tickets for
  documentation gaps.
- **Config-assist API endpoint and dashboard UI** — added
  `POST /services/{name}/config/assist` endpoint that runs a component's
  repo-declared config-assist command in a one-shot container and returns
  auto-filled config values without persisting to the config store. Extended
  `GET /services/{name}/config` to return `config_assist_command` and
  `config_assist_seeds` fields. The config modal now shows an "Auto-detect /
  Assist" button and orange-bordered seed fields for components that declare
  the `robotsix.deploy.config-assist` label.
- **Config-assist seed-value placeholder substitution** — the config-assist
  command template now supports `{dotted.path}` placeholders that are
  substituted with the user's submitted seed values (navigated from the
  form body via dotted-path notation including list indices).  Detected
  output is deep-merged into the submitted config so the assist command
  never clobbers fields the user already entered.

- **Settings page: gateway base domain and Claude mount path** — added
  `gateway_base_domain` and `claude_host_mount_path` to `SystemSettings`,
  `LifecycleConfig`, the settings API (`GET`/`PUT /settings`), and the dashboard
  Settings UI. The `gateway_base_domain` is used at startup to build absolute
  `↗ Open` shortcut URLs for proxied services. The `claude_host_mount_path`
  replaces the hardcoded `~/.claude` default when set (requires service restart).

- **Populated `config/components.yaml`** — pinned all six components to non-`:latest`
  image tags (`:main` for robotsix services, `:3.3.0.0` for radicale), filled every
  `env` block with required environment keys, assigned non-conflicting host ports
  (8200–8202, 8300, 3000, 5232), confirmed named volumes and stateful-volume
  annotations, and removed all 18 `TODO` placeholder markers.

- **Configuration documentation** — added `docs/configuration.md` documenting
  all `ROBOTSIX_LIFECYCLE_*` environment variables (host, port, API key, store
  backend/path, execution backend).

- **Per-component config.yaml support** — central-deploy now fetches and parses
  `config/config.yaml` from onboarded repos at preflight time, persists the schema
  and user-saved values in `ConfigYamlStore`, exposes `GET`/`PUT /services/{name}/config`
  endpoints with secret masking, and writes merged YAML into a `{name}-config` Docker
  named volume at onboard and on every save. The deploy-contract docs are updated with
  the new `config/config.yaml` convention.

- **Config modal UI** — each component card now has a "Configure" button that opens
  an auto-generated form based on the config schema. Supports nested sections,
  secret fields with sentinel preservation (`***`), and safe refresh on save.

- **Fix OpenAPI/runtime mismatch in error responses** — the global
  `http_exception_handler` now returns `ErrorDetail` instances instead of raw dicts,
  ensuring runtime error bodies match the OpenAPI-declared `ErrorDetail` schema
  (`{"error": "...", "detail": "..."}`).

- **`DELETE /services/{name}` endpoint** — removes an onboarded component, its service
  records, environment variables, secrets, and in-memory registry entry. Supports an
  optional `?stop_container=false` query parameter to skip container stop/removal
  (default: `true`). Container stop and remove are best-effort — errors are logged at
  `WARNING` and do not abort the deletion. Sibling services are also stopped, removed,
  and cleaned up.

- **Calendar stack contract-conforming compose files** — authored
  `# central-deploy-contract-version: 1` compose files for `robotsix-calendar-agent`
  and `robotsix-radicale` and pushed them to their respective repos.  The calendar-agent
  compose removes the `build:` block, `restart:`, `extra_hosts:`, and watchtower labels;
  converts env vars to the contract `KEY=default` / `KEY=` syntax; and adds a healthcheck
  matching the Dockerfile (`CMD python /app/healthcheck.py`).  The radicale repo (newly
  created) wraps `tomsquest/docker-radicale` with a `radicale-data` named volume carrying
  the `robotsix.deploy.stateful` label.

- Updated `config/components.yaml` baseline entries for `calendar-agent` and `radicale`
  with correct image refs, env keys, healthcheck commands, and the `stateful_volumes`
  annotation for `radicale-data`.

- **Dashboard Remove button** — added a per-row "Remove" button to the component
  dashboard that calls `DELETE /services/{name}?stop_container=<bool>`. Two
  `window.confirm` prompts guard against accidental removal: the first selects
  whether to also stop/remove the container, the second is a final confirmation.

- **Dashboard env/secrets config modal** — added a per-component "Config" button
  that opens an env/secrets editing modal.  Users can view, add, edit, and delete
  environment variables and secrets from the UI without SSH.  The modal uses the
  existing `GET/PUT/DELETE /services/{name}/env` API endpoints and matches the
  dashboard's dark theme styling.

- **Parse `container_name` from compose YAML** — `parse_compose()` now reads
  `container_name` from the service definition and passes it through `DerivedSpec`.
  `onboard_confirm` uses `spec.container_name or spec.name` to set the container name
  on both `ComponentConfig` and `ServiceRecord`.  Empty string defaults to the component
  name, matching the existing broker (agent-comm) behaviour.

- **Encrypted env secrets storage** — new `SecretKeyManager` (Fernet key generation
  and encrypt/decrypt) and `EnvStore` (JSON persistence for per-component env overrides
  and encrypted secret tokens).  Three new API endpoints:
  `GET /services/{name}/env` (returns plaintext env + masked secrets),
  `PUT /services/{name}/env` (upsert env values and secrets),
  `DELETE /services/{name}/env/{key}` (remove a key from env or secrets).
  Merged env (base YAML + user overrides + decrypted secrets) is injected into
  container creation at `deploy_service()` and `rollback_service()` time.
  New config fields: `env_store_path` (`ROBOTSIX_LIFECYCLE_ENV_STORE_PATH`,
  default `component_env.json`) and `secret_key_path`
  (`ROBOTSIX_LIFECYCLE_SECRET_KEY_PATH`, default `secrets.key`).
  Added `cryptography>=41.0` dependency.

- **Onboard UI modal** — "Add Component" button on the dashboard opens a two-step
  onboard modal: enter git URL + name, fetch+review the derived spec (ports, volumes,
  env, Claude mount), edit secrets, and deploy.  Stateful volumes show an amber
  "starts EMPTY" warning.  Validation errors and 409 conflicts are surfaced inline.
- **Onboard API endpoints** — new `POST /onboard/preflight` (fetch+validate a service
  repo's compose) and `POST /onboard/confirm` (persist config, deploy container,
  register component).  Dynamic `ComponentConfigStore` persists onboarded components
  to JSON and re-loads them on server restart.  `DockerSdkBackend` pre-creates named
  volumes before container creation and injects an optional `~/.claude` host mount.

- **Volume error handling** — named volume creation in `DockerSdkBackend.deploy()`
  is wrapped in try/except with clear error messages for invalid volume names and
  Docker daemon connectivity issues.  Volume-already-exists (HTTP 409) errors are
  handled gracefully with a log message.
  Added `claude_mount`, `named_volumes`, and `stateful_volumes` fields to
  `ComponentConfig`.
- **Onboard-from-git compose parser** — new `onboard` package with
  `fetch_compose_bytes` (shallow git clone + raw bytes) and
  `parse_compose` (validates against the v1 deploy contract and
  returns a `DerivedSpec`).  Contract covers: single-service, no-build,
  named-volume-only, env key extraction, healthcheck Go-duration
  conversion, and `robotsix.deploy.*` extension labels (claude-mount,
  stateful-volume flagging).
  - `docs/deploy-contract.md` updated: missing header = parse error,
    image accepts any non-empty string, env allows preset defaults,
    driver enforcement removed.
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
