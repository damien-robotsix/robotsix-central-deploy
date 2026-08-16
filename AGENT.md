# AGENT.md — robotsix-central-deploy Lifecycle Server

> Repo conventions (tooling, CI gates, packaging, deploy contracts) follow the
> shared [robotsix-standards](https://github.com/damien-robotsix/robotsix-standards).

## Overview

`robotsix-central-deploy` is a **FastAPI** lifecycle server that manages Docker containers for the robotsix fleet. It acts as a single control plane to start, stop, restart, deploy, rollback, and inspect every managed component. Component traffic is carried by a **separate Traefik edge** which central-deploy programs by stamping routing labels onto each container it creates (`registry/traefik_labels.py`) — the control plane is never on the request path. It also provides an **onboarding pipeline** for adding new services from docker-compose repos, a **settings API** for operator runtime configuration, and a **registry checker** that monitors GHCR for newer image versions.

## Repo-Agnostic Rule (CRITICAL)

`central-deploy` is a **generic deployment engine**.  Its source code MUST NOT contain
any service-specific or repo-specific references:

- No hard-coded service names (e.g. `"chat"`, `"cognee"`, `"mill"`)
- No per-service allowlists, project aliases, or special-case branches
- No hard-coded hostnames or deployment-specific URLs as defaults

Service definitions belong in **declarative data**, never in engine code:

| Data plane | Location |
| --- | --- |
| Component configs (per-service Docker/image/port/volume specs) | `component_config_store` — persisted at `data/component_configs.json` and populated via the onboarding API (`POST /onboard/preflight` + `POST /onboard/confirm`) or the seed module at startup |
| Virtual components (non-Docker chat-accessible services) | `LifecycleConfig.virtual_components` in `config/config.json` |
| Langfuse project credentials | Each component owns them in its own standardized config under the canonical block `langfuse.host` + `langfuse.projects.<project-name>` → `{public_key, secret_key, project_id}`, read by the single-source-of-truth helper `lifecycle/_langfuse_config.py`. Key pairs are **auto-discovered** from any service whose `allow_chat_access`/`chat_agent_mutatable` toggle is enabled and reconciled as chat-proxy aliases (no operator key-pasting); `GET /fleet/langfuse` serves the same data to fleet consumers. `LifecycleConfig.langfuse_projects` in `config/config.json` remains the operator-configured override. **No** fallback to `LANGFUSE_*` env vars or pre-standard shapes |
| Chat-agent mutation permissions | `ComponentConfig.chat_agent_mutatable` — set per-component via the `robotsix.deploy.chat-agent-mutatable` compose label at onboard time |
| Edge routes / TLS | Derived automatically from onboarded component ids + `gateway_base_domain` by `registry/traefik_labels.py`, emitted as Docker labels — no per-service routing rules exist |

The rule covers the **edge configuration** too, not only Python. `deploy/traefik/*.yml`
must contain no component name and no per-component hostname: routes come from the
labels `registry/traefik_labels.py` derives, and a middleware or router naming a
specific service is the same violation as a hard-coded branch in the engine. If a
component needs edge behaviour nothing else needs, that belongs in the component's
own deploy contract, not here.

**When adding a new managed service:** onboard it via the self-service API
(or declarative manifest), never by editing `central-deploy` engine code.

## Key Concepts

### Service State Machine

Every managed service follows a strict state machine with seven states:

- **STOPPED** / **STARTING** / **RUNNING** / **STOPPING** / **RESTARTING** / **FAILED** / **UNKNOWN**

Allowed transitions are defined in `lifecycle/models.py`:

- STOPPED → STARTING
- STARTING → RUNNING | FAILED
- RUNNING → STOPPING | RESTARTING
- STOPPING → STOPPED | FAILED
- RESTARTING → STOPPING
- FAILED → STARTING
- UNKNOWN → STARTING | STOPPING

Endpoints enforce these transitions via `can_transition()` and return **409 Conflict** on invalid requests. All mutating endpoints are **idempotent**: if the service is already in the requested state (or mid-transition toward it), the endpoint returns success without action.

### Component Model

A **component** is a managed service defined by a `ComponentConfig` (`registry/models.py`):

- `id` — stable slug matching `^[a-z0-9][a-z0-9-]*$`
- `image` — container image reference (e.g. `ghcr.io/org/service:main`)
- `container_name` — Docker container name
- `ports` — `PortMapping` list (host, container, protocol)
- `mounts` — `VolumeMount` list (host path or named volume, container path, read-only flag)
- `env` — static key/value environment variables
- `health_check` — optional `HealthCheck` mirroring Docker's spec
- `claude_mount` — if true, mounts `~/.claude` → `/root/.claude`
- `named_volumes` — volume names to pre-create at deploy time
- `siblings` — list of `ServiceConfig` for multi-service components (see below)

Components can be **single-service** (no siblings) or **multi-service** (one primary + one or more sibling services). Sibling records are named `{component_id}-{service_key}`.

### Multi-Service Components (Siblings)

When a component has siblings, lifecycle actions (start/stop/restart/deploy/rollback/delete) **fan out** to sibling services automatically on a best-effort basis. If a sibling action fails, the primary still succeeds but the failure is logged.

> **Rule:** Sibling fan-out for lifecycle actions (start/stop/restart/deploy/update) must reuse the shared helpers in `lifecycle/routers/_sibling_utils.py` (`_fanout_siblings_best_effort`, `_fanout_siblings_deploy_best_effort`) — never duplicate the per-sibling loop or `model_copy` sibling-config build inline in a router.

## API Endpoints

### Health & System

| Method | Path | Auth | Description |
| -------- | ------ | ------ | ------------- |
| GET | `/health` | **No** | Liveness probe |
| GET | `/disk` | Yes | Host disk usage + Docker storage breakdown |
| GET | `/system/update` | Yes | Is a newer server image available on the registry? |
| POST | `/system/update` | Yes | Self-update: one-shot watchtower container pulls the new image and recreates the server container |
| POST | `/chat/services/central-deploy/restart` | Yes | Chat-agent allowlisted: restart central-deploy itself (returns 202, async restart via Docker daemon) |
| POST | `/chat/services/central-deploy/update` | Yes | Chat-agent allowlisted: pull latest image + recreate central-deploy via watchtower (returns 202, async) |
| GET | `/ui` | Yes | HTML monitoring dashboard |
| GET | `/help/deploy-contract` | No | Rendered DEPLOY_CONTRACT.md |

### Service Management

All service endpoints require auth when configured.

| Method | Path | Description |
| -------- | ------ | ------------- |
| GET | `/services` | List all managed services |
| GET | `/services/{name}` | Full status — state, image, health, digests |
| GET | `/services/{name}/health` | Health status string |
| GET | `/services/{name}/logs?tail=100&since=&follow=` | Stream container logs |
| POST | `/services/{name}/start` | Start a service (idempotent) |
| POST | `/services/{name}/stop` | Stop a service (idempotent) |
| POST | `/services/{name}/restart` | Restart a service (idempotent) |
| POST | `/services/{name}/deploy` | Deploy a new image version |
| POST | `/services/{name}/rollback` | Roll back to prior image digest |
| POST | `/services/{name}/refresh-contract` | Re-fetch deploy/docker-compose.yml from the repo and update stored contract settings |
| DELETE | `/services/{name}?stop_container=true` | Remove an onboarded component |

### Onboarding

Two-phase process:

1. **`POST /onboard/preflight`** — clone repo, parse `docker-compose.yml`, return `DerivedSpec`
2. **`POST /onboard/confirm`** — persist `ComponentConfig`, deploy primary + siblings

### Config & Environment

| Method | Path | Description |
| -------- | ------ | ------------- |
| GET | `/services/{name}/config` | Config schema and current values (secrets masked) |
| GET | `/services/{name}/config/export` | **[Migration]** Full config with unmasked secrets (localhost + API-key only) |
| GET | `/services/{name}/env` | Env and secrets (secrets masked) |
| PUT | `/services/{name}/env` | Upsert env and secrets |
| DELETE | `/services/{name}/env/{key}` | Remove a single env key or secret |

> **Deprecated** (config ownership is moving to each component):
> `PUT /services/{name}/config`, `POST /services/{name}/config/import`,
> `POST /services/{name}/config/refresh-schema`, `POST /services/{name}/config/assist`
> — all return **410 Gone** with `Deprecation` / `Sunset` headers.
> Use `GET /services/{name}/config/export` to retrieve config for migration.
> Docker-boundary settings (image, ports, mounts, env/secrets) are **not** deprecated.

### Git clone → parse → deploy → monitor → volume audit

## Execution Backends

| Backend | Config value | Description |
| --------- | ------------- | ------------- |
| `DockerSdkBackend` | `docker_sdk` | Uses `docker` Python SDK (default). Full deploy/rollback/log streaming. |
| `DockerBackend` | `docker` | Uses `docker` CLI via subprocess. Limited — deploy/rollback raise `NotImplementedError`. |
| `NoopBackend` | `noop` | All ops succeed silently. No Docker required. For testing. |

## Authentication

Configured via environment variables (`ROBOTSIX_LIFECYCLE_` prefix):

- `API_KEY` — `X-API-Key` header
- `AUTH_USERNAME` + `AUTH_PASSWORD` — HTTP Basic Auth

Auth is **off** when no credentials are configured (dev mode). `/health` is always open.

**There is no login page and no session store.** Operator authentication happens once at the fleet edge (Traefik + tinyauth); `verify_auth` above is defence-in-depth on the JSON API against a caller already on the internal Docker network. Do not re-introduce per-app login — see the [component standard](https://damien-robotsix.github.io/robotsix-standards/component-standard/).

## Configuration

All settings loaded via `pydantic-settings` from environment or `.env.lifecycle`. Key variables:

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `HOST` | `0.0.0.0` | Server bind host |
| `PORT` | `8100` | Server bind port |
| `STORE_BACKEND` | `memory` | `memory` or `file` |
| `EXECUTION_BACKEND` | `docker_sdk` | `docker_sdk`, `docker`, or `noop` |
| `DOCKER_SOCKET_URL` | `unix:///var/run/docker.sock` | Docker daemon URL |
| `REGISTRY_CHECK_INTERVAL` | `300` | Background check interval (0=disabled) |
| `LOG_LEVEL` | `INFO` | Root logger level |

## File Structure

```
src/robotsix_central_deploy/
├── lifecycle/        # FastAPI app, state machine, backends, auth
├── onboard/          # Git clone + docker-compose parsing
├── registry/         # Component config, env/secrets, settings stores
├── registry_check/   # GHCR digest polling
├── ui/               # Dashboard HTML + router
├── caretaker/        # Background maintenance agent
│   └── volume_audit/ # Named-volume growth scanner (caretaker sub-package)
```

**Rule:** Test files for module X belong under `tests/X/`, never at the `tests/` root. Every module already follows this convention (lifecycle, registry, ui, registry_check, caretaker, onboard). Do not create new test files at the `tests/` root — place them in the corresponding `tests/<module>/` directory.

## Documentation

**Rule:** When adding a new public `.py` module (not private, not `__init__.py`), add a corresponding `::: robotsix_central_deploy.<module_path>` mkdocstrings directive to `docs/lifecycle/api.md` under the appropriate section. The section headers in `docs/lifecycle/api.md` mirror the `src/robotsix_central_deploy/` directory structure — new modules should be listed alongside their sibling modules in the matching section.

**Rule:** When adding, renaming, or deleting a public FastAPI route or Pydantic response model, regenerate `docs/lifecycle/openapi.json` in the SAME commit via `uv run python _gen_openapi.py` — never rely on the `openapi-drift` CI gate to catch the drift.

## Code Gotchas

1. **Sibling fan-out is best-effort** — failures are logged but don't fail the primary operation.
2. **Registry check interval changes require restart** — captured at startup.
3. **Fernet key loss is irrecoverable** — secrets must be re-entered if `secrets.key` is deleted.
4. **Reserved names** (`traefik`, `tinyauth`, `auth`, `deploy`) cannot be used as component slugs — they would shadow the fleet's own edge hostnames. See `RESERVED_NAMES` in `registry/constants.py`.
5. **Siblings must stay unroutable** — `_sibling_utils.py` sets `routable=False` on the sibling config copy. Dropping it would publish a component's database at `<component>-db.<base-domain>`.
6. **`NoopBackend` always reports `sha256:noop`** — never use in production.
7. **No catch-all routes** — a `/{path:path}` route shadows every endpoint after it. `tests/lifecycle/test_app.py` guards this.
8. **Never add a static per-component route file to `deploy/traefik/`** (e.g. `file-hub.yml`). Once a component is onboarded via the self-service API, its routing labels are stamped automatically by `traefik_labels()` at deploy time — a static file's router/service names collide with the Docker-label routes, causing Traefik to drop the colliding routers and return proxy 404. Remove any bootstrap route file (and its `docker-compose.yml` mount) as soon as the component is registered.
