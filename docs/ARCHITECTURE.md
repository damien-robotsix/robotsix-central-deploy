# Architecture — robotsix-central-deploy

## High-level overview

`robotsix-central-deploy` is a **FastAPI** lifecycle server that manages
Docker containers for the robotsix fleet. It acts as a single control plane
to start, stop, restart, deploy, rollback, and inspect every managed
component. Component traffic is carried by a **separate Traefik edge**
(see [The fleet edge](edge.md)) which central-deploy programs by stamping
routing labels onto each container it creates, an **onboarding pipeline** for adding
new services from docker-compose repos, a **self-contract** mechanism
that reads system settings from its own `deploy/docker-compose.yml` labels
at startup, a **background registry checker** that polls GHCR
for newer image versions, and a **volume audit subsystem** that tracks
Docker volume growth over time.

### System component diagram

```
                          ┌─────────────────────────┐
                          │     External clients     │
                          └────────────┬────────────┘
                                       │ HTTP / WS
                                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                     FastAPI Application                           │
│                                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐ │
│  │  /health  │  │  /disk   │  │  /ui     │                      │
│  │  (open)   │  │  (auth)  │  │ (edge-   │                      │
│  │           │  │          │  │  authed) │                      │
│  └──────────┘  └──────────┘  └──────────┘                      │
│                                                                  │
│  ┌──────────────────────────────────────────────────────┐        │
│  │              Lifecycle Router                        │        │
│  │  /services, /services/{name}/{start,stop,restart,    │        │
│  │   deploy,rollback,logs,health,config,env}            │        │
│  └──────────┬───────────────────────────────────────────┘        │
│             │                                                     │
│  ┌──────────────────────────────┐                                │
│  │  Onboard Router              │                                │
│  │  /onboard/preflight          │                                │
│  │  /onboard/confirm            │                                │
│  └──────────┬───────────────────┘                                │
└─────────────┼────────────────────────────────────────────────────┘
              │
     ┌────────┴────────┬──────────────────┬──────────────────┐
     ▼                 ▼                  ▼                  ▼
┌─────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ Docker   │   │  Registry    │   │  Registry    │   │  Volume      │
│ Backend  │   │  Checker     │   │  Stores      │   │  Audit       │
│ (SDK or  │   │  (background │   │  (config,    │   │  Scheduler   │
│  CLI)    │   │   polling)   │   │   env,       │   │  (background │
│          │   │              │   │   settings)  │   │   loop)      │
└────┬─────┘   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
     │                │                  │                   │
     ▼                ▼                  ▼                   ▼
┌─────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ Docker   │   │  GHCR /      │   │  JSON files  │   │  audit.json  │
│ daemon   │   │  registries  │   │  on disk     │   │  on disk     │
└─────────┘   └──────────────┘   └──────────────┘   └──────────────┘
```

## Subpackage responsibilities

### `lifecycle/` — Lifecycle control API

| File | Role |
| ------ | ------ |
| `app.py` | FastAPI application factory. Wires routers, middleware, background tasks. |
| `models.py` | **Service state machine** — `ServiceState` enum, `TRANSITIONS` dict, `can_transition()`, plus all API Pydantic schemas (`ServiceStatus`, `ServiceListItem`, …). |
| `backend.py` | **Execution backends** — `DockerSdkBackend` (default, full-featured), `DockerBackend` (CLI subprocess, limited), `NoopBackend` (testing). Each implements the same abstract interface (`start`, `stop`, `restart`, `deploy`, `rollback`, `status`). |
| `store.py` | **Service store** — `InMemoryStore` and `FileStore` backends that persist `ServiceRecord` state. Selected via `STORE_BACKEND` config. |
| `auth.py` | Authentication — API key + HTTP Basic Auth via FastAPI dependencies. |
| `disk.py` | Host disk usage + `docker system df` breakdown (`GET /disk`). |

**Public API surface**: `GET /services`, `GET /services/{name}`, `GET /services/{name}/health`,
`GET /services/{name}/logs`, `POST /services/{name}/start`, `POST /services/{name}/stop`,
`POST /services/{name}/restart`, `POST /services/{name}/deploy`, `POST /services/{name}/rollback`,
`DELETE /services/{name}`, `GET /services/{name}/config` (secrets redacted to `"***"`),
`GET /services/{name}/config/export` (migration-only — full config with unmasked secrets,
localhost + API-key restricted),
`PUT /services/{name}/config` (deprecated — returns 410 Gone),
`POST /services/{name}/config/assist` (deprecated — returns 410 Gone),
`POST /services/{name}/config/import` (deprecated — returns 410 Gone),
`POST /services/{name}/config/refresh-schema` (deprecated — returns 410 Gone),
`GET /services/{name}/env`, `PUT /services/{name}/env`, `DELETE /services/{name}/env/{key}`.

> **Config ownership:** the deploy plane manages **Docker-boundary** settings only
> (image references, port mappings, volume mounts, boot-time env vars and secrets).
> Runtime config (keys in each component's `config/config.json`) is **component-owned**
> and should be edited through the component's own Settings panel.  The
> `GET /services/{name}/config` endpoint is retained as read-only for inspection;
> config-write endpoints (`PUT`, `POST`) are deprecated and return 410 Gone with
> `Deprecation` / `Sunset` headers.  A migration-only `GET /services/{name}/config/export`
> endpoint returns the full config (including plaintext secrets) for components to
> import exactly once.  See the [config-ownership standard](
> https://damien-robotsix.github.io/robotsix-standards/config-ownership/
> ) for the "Two invariants" (deploy-plane exclusivity + cross-UI uniformity).

### `onboard/` — Git-clone ingestion

| File | Role |
|------|------|
| `fetcher.py` | `fetch_repo_files(git_url)` — shallow-clones a service repo, reads `deploy/docker-compose.yml` and optionally `config/config.yaml`. |
| `parser.py` | `parse_compose(compose_bytes, name, git_url)` — validates the compose file against the deploy contract, extracts service definitions, returns a `DerivedSpec`. |
| `models.py` | Pydantic models: `RepoFiles`, `DerivedSpec`, parse-error types. |

### `registry/` — Persistence stores

| File | Store class | Purpose |
| ------ | ------------- | --------- |
| `models.py` | *(data types)* | `ComponentConfig`, `ServiceConfig`, `PortMapping`, `VolumeMount`, `HealthCheck` — Pydantic models defining the shape of a deployed component. |
| `loader.py` | `ComponentRegistry` | Reads **static** YAML manifest (`registry.yaml`) into an in-memory index. |
| `traefik_labels.py` | `traefik_labels()` | Derives the edge's routing labels for a component from its id and primary port. Stamped onto the container at create time. |
| `constants.py` | `PROXY_NETWORK`, `RESERVED_NAMES` | The shared Docker network, and slugs that would shadow the fleet's own edge hostnames. |
| `config_store.py` | `ComponentConfigStore` | JSON store for **dynamically onboarded** components. Async-locked, atomic write (tmp + rename). |
| `env_store.py` | `EnvStore` | JSON store for per-component environment variables and Fernet-encrypted secrets. |
| `config_yaml_store.py` | `ConfigYamlStore` | JSON store for per-component `config.yaml` templates and user-saved values. |
| `settings_store.py` | `SystemSettingsStore` | JSON store for system-wide operator settings (auth, disk warn %, registry check interval, log level, gateway base domain). |
| `secret_key.py` | `SecretKeyManager` | Fernet encryption/decryption wrapper. **Key loss is irrecoverable** — secrets must be re-entered if `secrets.key` is deleted. |

### `registry_check/` — Background registry polling

| File | Role |
|------|------|
| `checker.py` | `RegistryChecker` — fetches OCI/Docker manifest digests from GHCR, caches results with a configurable TTL. Returns `None` for unsupported registries. Authenticates to `ghcr.io` with the shared `GhcrCredentialResolver` (`_ghcr_auth.py`), the same credential the image pull uses, so private packages resolve a digest. |

### `caretaker/volume_audit/` — Volume growth detection (caretaker sub-package)

| File | Role |
| ------ | ------ |
| `models.py` | Pydantic schemas: `VolumeSizeSnapshot`, `VolumeGrowthRecord`, `AuditFinding`, `VolumeAuditResponse`. |
| `growth.py` | `compute_growth_records()` — compares two snapshots; flags a finding when both absolute and percentage thresholds are breached. |
| `reporter.py` | `report_finding()` — logs at WARNING, appends to a local JSON file, optionally creates a board ticket. |
| `scheduler.py` | `VolumeAuditScheduler` — orchestrator. `run_once()` measures every volume, computes deltas, reports findings, persists the new snapshot. `loop()` runs this periodically as a background asyncio Task. |

### `caretaker/` — Background maintenance agent

| File | Role |
| ------ | ------ |
| `models.py` | Domain models: `FindingKind` enum (6 kinds — `UPDATE_APPLIED`, `UPDATE_FAILED`, `HEALTH`, `VOLUME_GROWTH`, `VOLUME_ORPHAN`, `DISK`), `CaretakerFinding` Pydantic model for individual issues, and `CaretakerReport` for aggregate pass results. |
| `mill_client.py` | `MillClient` — async HTTP wrapper for the mill component (`/tickets/ingest`, `/health`, `/repos`). Every method returns `bool` and never raises, so caretaker passes never fail on mill unavailability. |
| `phases.py` | Three independent async phase functions: `phase_update` (deploys updated images for opted-in components), `phase_health` (checks container status), and `phase_volumes` (volume growth, orphan detection, disk usage). Each emits `CaretakerFinding` records. |
| `scheduler.py` | `CaretakerScheduler` — orchestrator that runs the three-phase pass on a configurable `caretaker_interval_hours`. Public methods: `run_once() → CaretakerReport`, `get_status() → dict`, and `loop()` (async infinite loop, cancellable, re-reads settings each iteration). |

**Key behaviours:**

- Findings are reported to the mill when available; otherwise they fall back
  to local JSONL logging (`local_only` flag on the report).
- The caretaker starts on a delay so the Docker backend and stores are fully
  initialised before the first pass.
- Phase functions are independent of the scheduler and mill client — they
  receive all dependencies as parameters.

### `ui/` — Dashboard

| File | Role |
| ------ | ------ |
| `router.py` | Serves `dashboard.html` at `/ui` and `login.html` at `/login`. |
| `dashboard.html` | Single-page HTML dashboard with service status, logs, and action buttons. |
| `login.html` | Login page for session-based auth. |

## Data flow

### git clone → parse → deploy → monitor → volume audit

```
1.  User runs POST /onboard/preflight {git_url}
      │
      ▼
2.  onboard/fetcher.py
      git clone --depth 1 $git_url
      read deploy/docker-compose.yml
      read config/config.yaml (or fallback template)
      → RepoFiles
      │
      ▼
3.  onboard/parser.py
      validate contract header
      parse compose → extract services, ports, volumes, healthchecks
      validate labels, named volumes, no bind-mounts / build:
      → DerivedSpec
      │
      ▼
4.  POST /onboard/confirm {DerivedSpec}
      persist ComponentConfig → ComponentConfigStore
      optionally save config.yaml template → ConfigYamlStore
      │
      ▼
5.  DockerSdkBackend.deploy()
      pull image, create container, start
      persist ServiceRecord → ServiceStore
      │
      ▼
6.  Traefik sees the new container's labels (Docker events)
      publishes <id>.deploy.robotsix.net → container_name:port
      no reload, no config file, no DNS change
      │
      ▼
7.  Background tasks:
      RegistryChecker polls GHCR for newer digests
        → sets update_available on ServiceRecord
      VolumeAuditScheduler measures volume sizes
        → reports growth findings
```

## Service state machine

### ServiceState transitions

```
                    ┌──────────┐
                    │  STOPPED │
                    └────┬─────┘
                         │ start
                         ▼
       ┌────────────────────────────┐
       │         STARTING           │
       └──────┬───────────┬─────────┘
              │ success    │ failure
              ▼            ▼
      ┌──────────┐   ┌──────────┐
      │ RUNNING  │   │  FAILED  │
      └──┬───┬───┘   └────┬─────┘
   stop  │   │ restart     │ start
         ▼   ▼             │
   ┌──────────┐            │
   │ STOPPING │◄───────────┘
   └────┬─────┘      ┌──────────────┐
        │            │  RESTARTING  │
   ┌────┴─────┐      └──────┬───────┘
   │ success  │ failure     │ (always
   ▼          ▼             │  proceeds)
┌──────┐  ┌──────┐          ▼
│STOPPED│ │FAILED│    ┌──────────┐
└──────┘  └──────┘    │ STOPPING │
                      └──────────┘

UNKNOWN ──► STARTING | STOPPING
```

All mutating endpoints are **idempotent**: if the service is already in
the requested state (or mid-transition toward it), the endpoint returns
success without action. Invalid transitions return **409 Conflict**.

### Mermaid diagram

```mermaid
stateDiagram-v2
    [*] --> UNKNOWN
    STOPPED --> STARTING
    STARTING --> RUNNING
    STARTING --> FAILED
    RUNNING --> STOPPING
    RUNNING --> RESTARTING
    STOPPING --> STOPPED
    STOPPING --> FAILED
    RESTARTING --> STOPPING
    FAILED --> STARTING
    UNKNOWN --> STARTING
    UNKNOWN --> STOPPING
```

## Key design decisions

### Why async vs sync stores

All registry stores (`ComponentConfigStore`, `EnvStore`,
`ConfigYamlStore`, `SystemSettingsStore`) are async with
`asyncio.Lock` serialisation. This is **not** because JSON
file I/O benefits from async — `json.loads()` and `Path.read_text()`
are synchronous. The async pattern exists to:

1. **Coexist with the FastAPI event loop** — all store calls happen
   inside async request handlers and background tasks. A synchronous
   file read that blocks the event loop would stall concurrent requests.
2. **Provide serialised access** — `asyncio.Lock` prevents concurrent
   read-modify-write races without needing filesystem-level locking.
3. **Consistency** — every store follows the same `async def get/put/delete`
   interface, making them interchangeable and testable.

### Deploy contract philosophy

The deploy contract (`docs/ui/DEPLOY_CONTRACT.md`) enforces a strict
separation between **development** and **deployment**:

- The repo-root `docker-compose.yml` is for **local development** and
  is **ignored** by the onboarding pipeline.
- The `deploy/docker-compose.yml` is the **production contract** that
  the central-deploy server reads. It must start with
  `# central-deploy-contract-version: 1`.
- Bind-mounts are **prohibited** — only named volumes are allowed
  (with one exception: `claude-mount`, which mounts the managed `claude-auth` named volume at `/home/app/.claude` and
  requires an explicit label).
- `build:` is **prohibited** — only pre-built images are allowed.

This split prevents config drift between dev and prod and ensures the
central-deploy server can reason about every container's volumes,
ports, and health checks without ambiguity.

### Multi-service components (siblings)

Components with multiple services are modelled as one primary plus
N siblings. Sibling services are identified with
`robotsix.deploy.primary: "true"` on the primary (exactly one required
when N>1). Lifecycle actions (start/stop/restart/deploy/rollback/delete)
**fan out** to siblings on a best-effort basis — a sibling failure is
logged but does not fail the primary operation.

### Execution backend abstraction

Three backends exist, selected by `EXECUTION_BACKEND`:

| Backend | When to use |
| --------- | ------------- |
| `docker_sdk` (default) | Production. Full-featured: pull, create, start, stop, logs, health, deploy, rollback. |
| `docker` | Legacy/fallback. Uses `docker` CLI via subprocess. Deploy/rollback raise `NotImplementedError`. |
| `noop` | Testing. All operations succeed silently. Always reports `sha256:noop` digest. |

### Fernet-based secret storage

Component environment secrets are encrypted at rest using Fernet
(symmetric encryption). The Fernet key is stored on disk at a
configured path. **Key loss is irrecoverable** — if the key file is
deleted, all stored secrets become unreadable and must be re-entered
by an operator.

### Background tasks

Two background `asyncio.Task` loops run in the same process:

1. **Registry checker** — polls GHCR for each component's image digest
   at `REGISTRY_CHECK_INTERVAL` (default 300s). Sets `update_available`
   on `ServiceRecord` so the dashboard can surface stale images.

2. **Volume audit** — measures every volume's size, compares against
   the previous snapshot, and reports findings when both absolute and
   percentage growth thresholds are breached. Findings are logged,
   persisted to disk, and optionally filed as board tickets.

Both are started during app startup and run for the lifetime of the
process.
