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
