# AGENT.md — robotsix-central-deploy Lifecycle Server

> Repo conventions (tooling, CI gates, packaging, deploy contracts) follow the
> shared [robotsix-standards](https://github.com/damien-robotsix/robotsix-standards).

## Overview

`robotsix-central-deploy` is a **FastAPI** lifecycle server that manages Docker containers for the robotsix fleet. It acts as a single control plane to start, stop, restart, deploy, rollback, and inspect every managed component. It also provides a **reverse-proxy gateway** so each component is reachable at a well-known URL under the deploy domain, an **onboarding pipeline** for adding new services from docker-compose repos, a **settings API** for operator runtime configuration, and a **registry checker** that monitors GHCR for newer image versions.

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
| Langfuse project credentials | `LifecycleConfig.langfuse_projects` (dict of alias → `{public_key, secret_key}`) in `config/config.json` |
| Chat-agent mutation permissions | `ComponentConfig.chat_agent_mutatable` — set per-component via the `robotsix.deploy.chat-agent-mutatable` compose label at onboard time |
| Gateway routes / TLS | Derived automatically from onboarded component ids + `gateway_base_domain` — no per-service routing rules exist |

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
| GET | `/services/{name}/config` | Config schema and current values |
| PUT | `/services/{name}/config` | Merge and save config.yaml values |
| GET | `/services/{name}/env` | Env and secrets (secrets masked) |
| PUT | `/services/{name}/env` | Upsert env and secrets |
| DELETE | `/services/{name}/env/{key}` | Remove a single env key or secret |

### Settings API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/settings` | Current system settings |
| PUT | `/settings` | Update settings (hot-applies where possible) |

### Gateway Proxy

Registered **last** on the FastAPI app. Routes by Host subdomain: `{name}.{gateway_base_domain}` maps to the managed container (`gateway_base_domain` must be configured). HTTP requests proxied via `httpx.AsyncClient`, WebSocket via bidirectional relay. Legacy path-prefix URLs (`/{name}/{path}`) are **not proxied** — they 307-redirect to the component subdomain (path-prefix proxying broke apps serving absolute asset URLs).

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
├── gateway/          # Reverse proxy (HTTP + WebSocket relay)
├── lifecycle/        # FastAPI app, state machine, backends, auth
├── onboard/          # Git clone + docker-compose parsing
├── registry/         # Component config, env/secrets, settings stores
├── registry_check/   # GHCR digest polling
├── ui/               # Dashboard HTML + router
├── caretaker/        # Background maintenance agent
│   └── volume_audit/ # Named-volume growth scanner (caretaker sub-package)
```

**Rule:** Test files for module X belong under `tests/X/`, never at the `tests/` root. Every module already follows this convention (lifecycle, gateway, registry, ui, registry_check, caretaker, onboard). Do not create new test files at the `tests/` root — place them in the corresponding `tests/<module>/` directory.

## Documentation

**Rule:** When adding a new public `.py` module (not private, not `__init__.py`), add a corresponding `::: robotsix_central_deploy.<module_path>` mkdocstrings directive to `docs/api.md` under the appropriate section. The section headers in `docs/api.md` mirror the `src/robotsix_central_deploy/` directory structure — new modules should be listed alongside their sibling modules in the matching section.

## Code Gotchas

1. **Sibling fan-out is best-effort** — failures are logged but don't fail the primary operation.
2. **Gateway router must be registered LAST** — it's a catch-all that would shadow specific API routes.
3. **Registry check interval changes require restart** — captured at startup.
4. **Fernet key loss is irrecoverable** — secrets must be re-entered if `secrets.key` is deleted.
5. **Reserved names** (`ui`, `health`, `services`, `onboard`, `docs`, `openapi.json`, `redoc`, `disk`, `settings`, `help`, `volumes`, `login`, `logout`) cannot be used as component slugs — see `RESERVED_NAMES` in `gateway/router.py`.
6. **`NoopBackend` always reports `sha256:noop`** — never use in production.
