# robotsix-central-deploy

> **📖 Documentation:** [robotsix.net/central-deploy](https://robotsix.net/central-deploy/)
>
> **📐 Conventions:** this repo follows the shared
> [robotsix-standards](https://github.com/damien-robotsix/robotsix-standards).

Central deployment & lifecycle server for the robotsix suite — a single place to
start, stop, restart, deploy, rollback, and inspect the status of each deployed
component.

## Installation

```bash
git clone https://github.com/damien-robotsix/robotsix-central-deploy.git
cd robotsix-central-deploy
uv sync --frozen
```

> **Note:** [uv](https://docs.astral.sh/uv/) is required — plain `pip install`
> is not supported because some dependencies are resolved from git sources
> pinned in `uv.lock`.

## Usage

```bash
uv run robotsix-lifecycle
```

The server starts on `http://0.0.0.0:8100` by default.  See the
[documentation](https://robotsix.net/central-deploy/) for the full API
reference and dashboard UI.

## Configuration

All settings are loaded from environment variables (or a `.env.lifecycle` file).
Key variables:

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `ROBOTSIX_LIFECYCLE_HOST` | `0.0.0.0` | Server bind host |
| `ROBOTSIX_LIFECYCLE_PORT` | `8100` | Server bind port |
| `ROBOTSIX_LIFECYCLE_STORE_BACKEND` | `memory` | `memory` or `file` |
| `ROBOTSIX_LIFECYCLE_EXECUTION_BACKEND` | `docker_sdk` | `docker_sdk`, `docker`, or `noop` |
| `ROBOTSIX_LIFECYCLE_LOG_LEVEL` | `INFO` | Root logger level |

Authentication is configured via `ROBOTSIX_LIFECYCLE_API_KEY` or
`ROBOTSIX_LIFECYCLE_AUTH_USERNAME` / `ROBOTSIX_LIFECYCLE_AUTH_PASSWORD`.
See the [Configuration docs](https://robotsix.net/central-deploy/configuration/)
for full details.

## Development / Contributing

```bash
uv sync                 # Install dev dependencies (pytest, ruff, mypy, …)
pre-commit install      # Install git pre-commit hooks (lint, format, type-check)
uv run pytest           # Run the test suite
ruff check .            # Lint
ruff format . --check   # Check formatting
uv run mypy src/        # Type check
```

For a detailed walkthrough of the codebase, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Deploy UI scope

The dashboard UI at `/ui` surfaces **only** deploy-plane-allowlisted settings:
image/tag, volumes, ports, restart policy, resource limits, and the
`/services/{name}/env` surface for environment variables and secrets.
Component-internal settings (anything defined in a component's own
`config/config.schema.json`) are **not** rendered or editable in the deploy UI;
they are managed through each component's own `/config` HTTP surface. This
follows the [config-ownership standard](
  https://damien-robotsix.github.io/robotsix-standards/config-ownership/
) "Two invariants": deploy-plane exclusivity and cross-UI uniformity.

### Config-ownership migration

The config-ownership migration (robotsix-standards) is **complete**: all five
components now operate on their self-owned settings. Runtime component config
(`config/config.json` keys) is fully component-owned and edited through each
component's own Settings API.

The former config endpoints (`PUT /services/{name}/config`,
`PUT /chat/config/{name}`, `GET /services/{name}/config`,
`GET /services/{name}/config/export`, `/import`, `/refresh-schema`, `/assist`,
and config rollback) have been **removed** from the deploy plane. The deploy
plane retains Docker-boundary settings only (image, ports, mounts,
env/secrets), which are **not** deprecated.

See the [config-ownership standard](
  https://damien-robotsix.github.io/robotsix-standards/config-ownership/
) and `docs/ARCHITECTURE.md` for the full boundary split.

Contributions welcome — see the [documentation](https://robotsix.net/central-deploy/).
