# Onboard

The onboard subsystem (`src/robotsix_central_deploy/onboard/`) provides a two-phase
workflow for adding new services to the central-deploy control plane. It fetches a
service repository via shallow git clone, parses its `deploy/docker-compose.yml`
against the central-deploy contract, and returns a `DerivedSpec` that can be
persisted as a `ComponentConfig`.

## Architecture

| File | Purpose |
| ------ | --------- |
| `models.py` | Data types — `DerivedSpec` (primary parsed output), `SiblingDerivedSpec` (non-primary services), and error types (`FetchError`, `ParseError`, `ConfigParseError`). |
| `fetcher.py` | Git-fetch layer — `fetch_repo_files()` shallow-clones an HTTPS repo, reads `deploy/docker-compose.yml`, optionally grabs `config/config.json`, and falls back to `config.example.json` or label-declared templates. `fetch_compose_bytes()` is a convenience wrapper. When a GitHub App installation token is provided, the clone URL is rewritten with `x-access-token` authentication to support private repos. |
| `parser.py` | Core compose parser — `parse_compose()` validates a docker-compose.yml against the deploy contract and returns a `DerivedSpec`. Also exports `parse_config_json()` for companion config files. |
| `port_utils.py` | Preflight helpers — `collect_occupied_host_ports()` scans deployed components for claimed ports; `find_free_host_port()` finds the lowest available port in a range. |

## Two-Phase Workflow

1. **Preflight** (`POST /onboard/preflight`) — clones the repo, parses the compose
   file, and returns a `DerivedSpec` (including any sibling services) without
   persisting anything. The caller can review the derived spec before confirming.
   When the GitHub App is configured (`github_app_id` / `github_app_private_key`)
   and the git URL points to GitHub, an installation token is minted and injected
   into the clone command so private repos clone successfully. Token retrieval
   failures fall back to unauthenticated clone (public repos only).

2. **Confirm** (`POST /onboard/confirm`) — accepts the `DerivedSpec` (possibly
   modified by the caller), persists a `ComponentConfig`, and deploys the primary
   and any sibling services.

## Deploy Contract

The parser enforces a contract on the service's `deploy/docker-compose.yml`:

- A top-level `x-robotsix` header with required metadata fields.
- One or more service definitions with container image, ports, environment variables,
  volumes, and health checks.
- Optional `config-target` and `config-assist` labels for settings integration.

### Chat-Access Compliance Advisories

During parsing, the parser generates **non-blocking advisories** about
chat-access compliance — these are surfaced in `DerivedSpec.advisories` but
do **not** cause a `ParseError`:

| Condition | Advisory |
|-----------|----------|
| `robotsix.deploy.chat-access` is not set to `"true"` | Component will be invisible to the chat assistant's roster |
| `chat-access` is enabled but `robotsix.deploy.chat-skill` is empty or missing | Component should expose a `GET /chat-skill` endpoint or provide a static skill description via the label |

Both labels (`robotsix.deploy.chat-skill` and `robotsix.deploy.chat-skill-endpoint`)
are parsed when present in the service labels, with `chat-skill-endpoint`
defaulting to `/chat-skill` when absent.

## API

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/onboard/preflight` | Yes | Clone repo, parse compose, return `DerivedSpec` |
| POST | `/onboard/confirm` | Yes | Persist config and deploy primary + siblings |
