# Configuration

The lifecycle server is configured via environment variables, all prefixed with `ROBOTSIX_LIFECYCLE_`.

## Environment Variables

### Server

| Variable | Default | Description |
| --- | --- | --- |
| `ROBOTSIX_LIFECYCLE_HOST` | `0.0.0.0` | IP address the lifecycle server binds to. |
| `ROBOTSIX_LIFECYCLE_PORT` | `8100` | TCP port the lifecycle server listens on. |

### Persistence

| Variable | Default | Allowed Values | Description |
| --- | --- | --- | --- |
| `ROBOTSIX_LIFECYCLE_STORE_BACKEND` | `memory` | `memory`, `file` | State store backend. `memory` keeps state in-process; `file` persists to disk. |
| `ROBOTSIX_LIFECYCLE_STORE_PATH` | `lifecycle_state.yaml` | — | File path for the state store when `STORE_BACKEND=file`. |
| `ROBOTSIX_LIFECYCLE_COMPONENT_CONFIG_STORE_PATH` | `data/component_configs.json` | — | File path for the dynamic component configuration store. |
| `ROBOTSIX_LIFECYCLE_ENV_STORE_PATH` | `component_env.json` | — | File path for the per-component environment variable store. |
| `ROBOTSIX_LIFECYCLE_SECRET_KEY_PATH` | `secrets.key` | — | File path for the secret key store. |
| `ROBOTSIX_LIFECYCLE_CONFIG_YAML_STORE_PATH` | `data/component_config_yaml.json` | — | File path for the per-component `config.yaml` store. |
| `ROBOTSIX_LIFECYCLE_SYSTEM_SETTINGS_PATH` | `data/system_settings.json` | — | File path for the system settings store. |

### Self-Contract

| Variable | Default | Description |
| --- | --- | --- |
| `ROBOTSIX_LIFECYCLE_SELF_CONTRACT_PATH` | `deploy/docker-compose.yml` | Path to central-deploy's own deploy contract. Read at startup to seed system settings from contract labels (`robotsix.deploy.settings.*`). |

### Execution

| Variable | Default | Allowed Values | Description |
|---|---|---|---|
| `ROBOTSIX_LIFECYCLE_EXECUTION_BACKEND` | `docker_sdk` | `docker_sdk`, `docker`, `noop` | Backend used to execute lifecycle operations. `docker_sdk` uses the Docker SDK for Python; `docker` uses the CLI; `noop` is a dry-run backend that performs no real work. |

### Auth

central-deploy ships **no** authentication of its own — no login page, no
session store, no API key, no HTTP Basic credentials. The fleet edge
(Traefik + tinyauth) is the only gate; see [The edge](../edge.md).
`verify_auth` on the JSON API is a deliberate no-op stub, kept only as an
interception point, and `tests/lifecycle/test_app.py` fails if any route ever
grows a real credential dependency.

### Docker

| Variable | Default | Description |
| --- | --- | --- |
| `ROBOTSIX_LIFECYCLE_DOCKER_SOCKET_URL` | `unix:///var/run/docker.sock` | Docker socket URL for connecting to the Docker daemon. Set to `tcp://socket-proxy:2375` in production when running behind a Docker socket proxy. |
| `ROBOTSIX_LIFECYCLE_DOCKER_SDK_TIMEOUT` | `120` | Timeout in seconds for all Docker SDK operations (image pull, container create/start/stop/remove, volume create/remove, etc.). Prevents indefinite blocking when the Docker daemon is slow or unresponsive. |

### Disk

| Variable | Default | Description |
| --- | --- | --- |
| `ROBOTSIX_LIFECYCLE_DISK_PATH` | `/` | Filesystem path to monitor for disk usage. Set to `/host_root` when running containerised with a host-root bind mount. |
| `ROBOTSIX_LIFECYCLE_DISK_WARN_PCT` | `10.0` (10%) | Disk usage warning threshold as a percentage of total disk. A warning is emitted when free space drops below this percentage. |

### Registry

| Variable | Default | Description |
| --- | --- | --- |
| `ROBOTSIX_LIFECYCLE_REGISTRY_CHECK_TTL` | `300` | Cache TTL in seconds for registry availability checks. |
| `ROBOTSIX_LIFECYCLE_REGISTRY_CHECK_INTERVAL` | `300` | Background registry check interval in seconds. Set to `0` to disable periodic checks. |

### Self-update

| Variable | Default | Description |
| --- | --- | --- |
| `ROBOTSIX_LIFECYCLE_SELF_UPDATE_WATCHTOWER_IMAGE` | `containrrr/watchtower:1.7.1` | One-shot updater image launched by `POST /system/update` to pull the newest server image and recreate the central-deploy container. |
| `ROBOTSIX_LIFECYCLE_SELF_UPDATE_DOCKER_API_VERSION` | `1.44` | `DOCKER_API_VERSION` exported to the one-shot updater. Watchtower 1.7.1's client defaults to API 1.25, below modern daemons' minimum, and crashes without it. Raise if a future daemon drops 1.44. |

### Logging

| Variable | Default | Description |
|---|---|---|
| `ROBOTSIX_LIFECYCLE_LOG_LEVEL` | `INFO` | Log level for the lifecycle server. |

### Edge

| Variable | Default | Description |
|---|---|---|
| `ROBOTSIX_LIFECYCLE_GATEWAY_BASE_DOMAIN` | `""` | Fleet base domain. Components are published at `<id>.<base-domain>`; must match `GATEWAY_BASE_DOMAIN` in the edge's `.env`. |

### Volume Audit

| Variable | Default | Description |
| --- | --- | --- |
| `ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_ENABLED` | `false` | Master on/off switch for the volume audit background scanner. |
| `ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_INTERVAL_SECONDS` | `3600` | Interval in seconds between volume audit scan passes. |
| `ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_SNAPSHOT_PATH` | `data/volume_audit_snapshots.json` | File path for persisted volume size snapshots. |
| `ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_FINDINGS_PATH` | `data/volume_audit_findings.json` | File path for persisted audit findings. |
| `ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_GROWTH_THRESHOLD_PCT` | `10.0` | Growth percentage threshold — a finding is emitted when a volume grows more than this percentage between scans. |
| `ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_MIN_DELTA_BYTES` | `10485760` (10 MiB) | Minimum absolute growth in bytes before a finding is emitted. |

### Rate Limiting

| Variable | Default | Description |
| --- | --- | --- |
| `ROBOTSIX_LIFECYCLE_RATE_LIMIT_API_PER_HOUR` | `20000` | Max requests per IP per hour to central-deploy's own JSON API (`/services`, `/volumes`, `/onboard`, `/chat`, …). Exceeding this returns HTTP 429. |

`rate_limit_api_per_hour` is the **only** rate limit central-deploy enforces.
It is applied by `RateLimitMiddleware` (`lifecycle/rate_limiter.py`) to
central-deploy's own traffic; component traffic is served by the Traefik edge
and never reaches this middleware.

There are no login rate limits, because there is no login: the per-minute,
max-attempts, and lockout settings that used to sit in this table were part of
the component-level auth that was removed, and nothing has read them since.

### Chat Agent

| Variable | Default | Description |
| --- | --- | --- |
| `ROBOTSIX_LIFECYCLE_CHAT_AGENT_DEPLOYABLE_COMPONENTS` | `[]` | Comma-separated list of component names the chat agent may deploy via `POST /chat/deploy`. Each entry must match `^[a-z0-9][a-z0-9-]*$`. This is a server-level allowlist for components that may not have a persisted `ComponentConfig` yet (distinct from the per-component `chat_agent_mutatable` flag). |

### Board Integration

| Variable | Default | Description |
| --- | --- | --- |
| `ROBOTSIX_LIFECYCLE_BOARD_API_URL` | `""` | Base URL for the robotsix board API. When empty, board integration is disabled. |
| `ROBOTSIX_LIFECYCLE_BOARD_API_TOKEN` | `""` | API token for authenticating with the robotsix board. |
| `ROBOTSIX_LIFECYCLE_BOARD_REPO_ID` | `""` | Repository ID on the robotsix board where tickets are filed. |
