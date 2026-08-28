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

All settings live in **one JSON file**, `config/config.json`, located by the
single environment variable `ROBOTSIX_CONFIG_FILE` — which only *locates* the
file and never carries a value. There is no environment overlay.

The schema is the `LifecycleConfig` pydantic model in
`src/robotsix_central_deploy/lifecycle/config.py`, reflected into the committed
`config/config.schema.json` that the deploy UI renders. Read the field
reference there rather than in a table that can drift from it.

Eighteen operator-facing keys are additionally editable at runtime through
`GET`/`PUT /settings` and the dashboard's Settings panel, which overlay
`data/system_settings.json` onto the config without a redeploy.

central-deploy ships no authentication of its own — the fleet edge is the only
gate. See the
[Configuration docs](https://robotsix.net/central-deploy/lifecycle/configuration/)
for the layering rules and the overlay gotcha.

## Development / Contributing

```bash
uv sync                 # Install dev dependencies (pytest, ruff, mypy, …)
pre-commit install      # Install git pre-commit hooks (lint, format, type-check)
python _gen_robotsix_ui_assets.py  # Fetch the gitignored robotsix-ui assets for /ui and /ui/settings
uv run pytest           # Run the test suite
ruff check .            # Lint
ruff format . --check   # Check formatting
uv run mypy src/        # Type check
```

> **Note:** the robotsix-ui assets under
> `src/robotsix_central_deploy/ui/static/` (`robotsix-ui.css` and
> `robotsix-ui-vanilla.js`) are gitignored and fetched at Docker build time,
> so running from source serves 404s for both until you fetch them with
> `python _gen_robotsix_ui_assets.py`. Without the JS the Settings panel at
> `/ui/settings` has nothing to mount.

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

Config-write endpoints (`PUT /services/{name}/config` and `PUT /chat/config/{name}`)
are **deprecated** and return 410 Gone with `Deprecation` / `Sunset` headers.
A migration-only `GET /services/{name}/config/export` endpoint (localhost +
API-key restricted) returns the full config with unmasked secrets so components
can import their config exactly once. Docker-boundary settings (image, ports,
mounts, env/secrets) remain in the deploy plane and are **not** deprecated.

See the [config-ownership standard](
  https://damien-robotsix.github.io/robotsix-standards/config-ownership/
) and `docs/ARCHITECTURE.md` for the full boundary split.

Contributions welcome — see the [documentation](https://robotsix.net/central-deploy/).
