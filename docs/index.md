# robotsix-central-deploy

Central deployment & lifecycle server for the robotsix suite.

This repository hosts the deployment/lifecycle control plane for the robotsix
agents and services — a single place to start, stop, restart, and inspect the
status of each deployed component, perform versioned deploys and rollbacks, and
register the supervision/monitoring agent with the agent-comm broker and
Langfuse.

## Features

- **Lifecycle API** — start / stop / restart / status for deployed components.
- **Versioned deploy & rollback** — promote a new image, roll back to the
  previously deployed digest.
- **Onboarding pipeline** — add new services from a docker-compose repo
  (preflight parse + confirm deploy) against the
  [deploy contract](ui/DEPLOY_CONTRACT.md).
- **Edge integration** — each component published at
  `<id>.<base-domain>` by the Traefik edge, from routing labels
  central-deploy stamps on its container.
- **Dashboard UI** — live status, logs, and env/secrets management at `/ui`.
  The deploy UI surfaces **only** deploy-plane-allowlisted settings (image/tag,
  volumes, ports, restart policy, resource limits, env/secrets) and no
  component-internal `config.json` schema. Component-owned settings are managed
  through each component's own `/config` surface. See the
  [config-ownership standard](
    https://damien-robotsix.github.io/robotsix-standards/config-ownership/
  ) for the "Two invariants" (deploy-plane exclusivity + cross-UI uniformity).
- **Registry update checks** — polls GHCR for newer image digests.
- **Volume audit** — background growth tracking of managed named volumes.

## Documentation

- **[Architecture](ARCHITECTURE.md)** — system design, subpackage responsibilities,
  data flow, and state machine.
- **Module overviews** — each subsystem is documented in its own
  `docs/<module>/overview.md`: Onboarding, Registry, Registry Check,
  Caretaker (including Volume Audit).

## Security / Credentials

The lifecycle server ships **no authentication of its own** — no login page,
no session store, no API key, no HTTP Basic credentials. The fleet edge
(Traefik + tinyauth) authenticates every request before it reaches any
container, so a component behind it only ever receives authenticated traffic.
See [The edge](edge.md) and [Configuration](lifecycle/configuration.md#auth).

Credentials central-deploy *presents* to services it calls — the GHCR pull
token, the GitHub App key, the board API token — are ordinary config fields,
declared as `SecretStr` and masked in the deploy UI. They are not a gate on
inbound requests.

### GHCR image-pull authentication

Private `ghcr.io` container images can be authenticated using one of two
methods, tried in priority order:

1. **Static PAT (`ghcr_pull_token`)** — a classic GitHub personal access token
   with ``read:packages`` scope, set from the dashboard's **Env & Secrets** tab
   for the ``central-deploy`` service. This token covers ALL private GHCR
   packages fleet-wide — a single PAT works for every managed component. Set
   or rotate it without editing ``config.json`` or restarting the server; the
   new value takes effect on the very next image pull.

2. **GitHub App installation tokens** — short-lived tokens minted by the
   [`robotsix-github-auth`](https://github.com/damien-robotsix/robotsix-github-auth)
   library. A fresh token is generated per pull operation. The three GitHub App
   fields (`github_app_id`, `github_app_private_key`, `installation_id`) in
   `config/config.json` control which App installation is used.

When neither credential is configured, `ghcr.io` pulls fall back to anonymous
access (public images only). A 403 ``denied`` error means the configured
credential lacks ``read:packages`` on the target package — check the PAT scope
or App installation permissions.

## Docker Socket Proxy

The lifecycle server talks to the Docker daemon to manage containers. For
defence-in-depth, production deployments route Docker API calls through a
**[tecnativa/docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy)**
sidecar instead of mounting the raw Docker socket directly into the
central-deploy container. The proxy exposes only the API endpoints that
central-deploy actually needs, blocking everything else at the reverse-proxy
layer.

### Enabled API scopes

| Scope | Env | Reason |
| ------------- | ------------- | -------- |
| CONTAINERS | `CONTAINERS=1` | List, inspect, start, stop, restart, remove, create containers; stream logs |
| POST | `POST=1` | Required for any mutating HTTP method (start/stop/restart/create) |
| DELETE | `DELETE=1` | Required for container removal during deploy |
| IMAGES | `IMAGES=1` | Required for `docker pull` |
| VOLUMES | `VOLUMES=1` | Create, list, inspect, and remove named volumes for managed services |
| BUILD | `BUILD=1` | Required for the dashboard's "Reclaim build cache" action (`POST /build/prune`) — central-deploy never builds images |
| SYSTEM | `SYSTEM=1` | Required for `/system/df` Docker disk-usage reporting |

All other scopes (`EXEC`, `NETWORKS`, `SWARM`, …) are explicitly disabled
(`=0`). `EXEC` remains off — central-deploy pulls pre-built images from GHCR
and never execs into containers.

### Configuration

The endpoint is the `docker_socket_url` config field (see
[Configuration](lifecycle/configuration.md)), not an environment variable.

- **Local dev:** the default `unix:///var/run/docker.sock` works directly
  against the host Docker daemon — no proxy needed.
- **Production:** `/data/config.json` sets
  `"docker_socket_url": "tcp://socket-proxy:2375"` so the lifecycle server
  connects through the proxy sidecar. The raw socket (`/var/run/docker.sock`)
  is mounted **only** into the `socket-proxy` container (read-only) —
  `central-deploy` never touches it.

The scope flags in the table above *are* environment variables, and legitimately
so: `socket-proxy` is a third-party image, and per §5 of the config standard a
sibling you do not control takes its configuration however it takes it.
