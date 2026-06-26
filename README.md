# robotsix-central-deploy

Central deployment & lifecycle server for the robotsix suite.

This repository hosts the deployment/lifecycle control plane for the robotsix
agents and services — a single place to start, stop, restart, and inspect the
status of each deployed component, perform versioned deploys and rollbacks, and
register the supervision/monitoring agent with the agent-comm broker and
Langfuse.

## Status

Initial seed commit. Implementation is tracked on the robotsix-mill board under
the "Central deployment & lifecycle server" epic. The first feature landing
here is the **lifecycle API** (start / stop / restart / status).

## Planned scope

- **Lifecycle API** — start / stop / restart / status for deployed components.
- **Versioned deploy & rollback** — promote a build, roll back to a prior one.
- **Broker + Langfuse registration** — register the supervision agent on the
  agent-comm broker and wire up tracing.
- **Supervision / monitoring agent** — watch deployed components and react.

## Security / Credentials

The lifecycle server supports two authentication mechanisms,
either of which can be used independently or together:

- **`ROBOTSIX_LIFECYCLE_API_KEY`** — API key accepted via the
  `X-API-Key` header (intended for programmatic clients / scripts).
- **`ROBOTSIX_LIFECYCLE_AUTH_USERNAME`** +
  **`ROBOTSIX_LIFECYCLE_AUTH_PASSWORD`** — HTTP Basic Auth
  credentials (intended for browser / UI access). The server
  responds with `WWW-Authenticate: Basic realm="Central Deploy"`
  on authentication failures so browsers can show a login dialog.

All endpoints except `GET /health` require authentication when
credentials are configured. `GET /health` is always open as a
liveness probe.

**Dev mode:** when *none* of the above environment variables are
set, every endpoint is accessible without credentials — useful for
local development.

### Example `.env.lifecycle` snippet

```ini
ROBOTSIX_LIFECYCLE_API_KEY=changeme
ROBOTSIX_LIFECYCLE_AUTH_USERNAME=admin
ROBOTSIX_LIFECYCLE_AUTH_PASSWORD=secure-password
```
