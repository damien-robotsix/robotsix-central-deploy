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

### Execution

| Variable | Default | Allowed Values | Description |
|---|---|---|---|
| `ROBOTSIX_LIFECYCLE_EXECUTION_BACKEND` | `docker_sdk` | `docker_sdk`, `docker`, `noop` | Backend used to execute lifecycle operations. `docker_sdk` uses the Docker SDK for Python; `docker` uses the CLI; `noop` is a dry-run backend that performs no real work. |
