# Configuration

The lifecycle server is configured via environment variables, all prefixed with `ROBOTSIX_LIFECYCLE_`.

## Environment Variables

### Server

| Variable | Default | Description |
|---|---|---|
| `ROBOTSIX_LIFECYCLE_HOST` | `0.0.0.0` | IP address the lifecycle server binds to. |
| `ROBOTSIX_LIFECYCLE_PORT` | `8100` | TCP port the lifecycle server listens on. |
| `ROBOTSIX_LIFECYCLE_API_KEY` | `""` | Static API key for bearer-token authentication. When empty, authentication is disabled (unless `AUTH_USERNAME`/`AUTH_PASSWORD` are both set). |

### Persistence

| Variable | Default | Allowed Values | Description |
|---|---|---|---|
| `ROBOTSIX_LIFECYCLE_STORE_BACKEND` | `memory` | `memory`, `file` | State store backend. `memory` keeps state in-process; `file` persists to disk. |
| `ROBOTSIX_LIFECYCLE_STORE_PATH` | `lifecycle_state.yaml` | — | File path for the state store when `STORE_BACKEND=file`. |
| `ROBOTSIX_LIFECYCLE_COMPONENT_CONFIG_STORE_PATH` | `data/component_configs.json` | — | File path for the dynamic component configuration store. |
| `ROBOTSIX_LIFECYCLE_ENV_STORE_PATH` | `component_env.json` | — | File path for the per-component environment variable store. |
| `ROBOTSIX_LIFECYCLE_SECRET_KEY_PATH` | `secrets.key` | — | File path for the secret key store. |
| `ROBOTSIX_LIFECYCLE_CONFIG_YAML_STORE_PATH` | `data/component_config_yaml.json` | — | File path for the per-component `config.yaml` store. |
| `ROBOTSIX_LIFECYCLE_SYSTEM_SETTINGS_PATH` | `data/system_settings.json` | — | File path for the system settings store. |

### Execution

| Variable | Default | Allowed Values | Description |
|---|---|---|---|
| `ROBOTSIX_LIFECYCLE_EXECUTION_BACKEND` | `docker_sdk` | `docker_sdk`, `docker`, `noop` | Backend used to execute lifecycle operations. `docker_sdk` uses the Docker SDK for Python; `docker` uses the CLI; `noop` is a dry-run backend that performs no real work. |

### Auth

| Variable | Default | Description |
|---|---|---|
| `ROBOTSIX_LIFECYCLE_AUTH_USERNAME` | `""` | HTTP Basic auth username. Authentication is enforced when both `AUTH_USERNAME` and `AUTH_PASSWORD` are non-empty, or when `API_KEY` is set. |
| `ROBOTSIX_LIFECYCLE_AUTH_PASSWORD` | `""` | HTTP Basic auth password. |

### Docker

| Variable | Default | Description |
|---|---|---|
| `ROBOTSIX_LIFECYCLE_DOCKER_SOCKET_URL` | `unix:///var/run/docker.sock` | Docker socket URL for connecting to the Docker daemon. Set to `tcp://socket-proxy:2375` in production when running behind a Docker socket proxy. |

### Disk

| Variable | Default | Description |
|---|---|---|
| `ROBOTSIX_LIFECYCLE_DISK_PATH` | `/` | Filesystem path to monitor for disk usage. Set to `/host_root` when running containerised with a host-root bind mount. |
| `ROBOTSIX_LIFECYCLE_DISK_WARN_BYTES` | `5368709120` (5 GiB) | Disk usage warning threshold in bytes. A warning is emitted when free space on `DISK_PATH` drops below this value. |

### Registry

| Variable | Default | Description |
|---|---|---|
| `ROBOTSIX_LIFECYCLE_GHCR_TOKEN` | `""` | GitHub Container Registry token for authenticated registry checks. |
| `ROBOTSIX_LIFECYCLE_REGISTRY_CHECK_TTL` | `300` | Cache TTL in seconds for registry availability checks. |
| `ROBOTSIX_LIFECYCLE_REGISTRY_CHECK_INTERVAL` | `300` | Background registry check interval in seconds. Set to `0` to disable periodic checks. |

### Logging

| Variable | Default | Description |
|---|---|---|
| `ROBOTSIX_LIFECYCLE_LOG_LEVEL` | `INFO` | Log level for the lifecycle server. |

### Gateway

| Variable | Default | Description |
|---|---|---|
| `ROBOTSIX_LIFECYCLE_GATEWAY_BASE_DOMAIN` | `""` | Base domain for gateway integration. |

### Claude Integration

| Variable | Default | Description |
|---|---|---|
| `ROBOTSIX_LIFECYCLE_CLAUDE_HOST_MOUNT_PATH` | `""` | Host mount path for Claude Desktop integration. |
