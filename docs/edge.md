# The fleet edge

Every request to the fleet — the dashboard, every component UI, every scripted
API call — arrives at one place: a **Traefik** container that terminates TLS,
authenticates the caller, and forwards to the right container.

central-deploy is not on that path. It is the **control plane**: it decides what
runs and stamps the routing rules onto containers as Docker labels. Traefik
watches the Docker API and picks them up. The two never share a process.

```
DNS *.<base-domain> ──────▶ :443 Traefik ── TLS: ACME DNS-01 / OVH, one wildcard
                                   │       ── routes: container labels, live
                                   │
                     ┌─────────────┼──────────────┐
              forwardAuth      basicauth        (no auth)
               → tinyauth       machine          /health
                                   │
           ┌─────────┬─────────────┼─────────────┐
         chat      board         mill      central-deploy
```

## Why the proxy is not in central-deploy any more

It used to be. `gateway/router.py` was registered last on the same FastAPI app
that serves the dashboard, and proxied component traffic through `httpx` and a
hand-written WebSocket relay. That coupling caused, in order:

- Every middleware needed a component-subdomain escape hatch — CSRF rejecting
  proxied POSTs, the dashboard's CSP disabling inline handlers in mill, chat and
  board, `/docs` on a component host serving the lifecycle API's destructive
  endpoints. Three "gateway-aware" wrappers existed only to undo the coupling.
- Component slugs needed a reserved-name list, because they shared a router
  table with control-plane paths.
- **Restarting the deployer took every UI in the fleet offline** — and
  self-update via watchtower is a routine operation.

All of it is gone. Traefik restarts independently of central-deploy, so
deploying or self-updating the control plane no longer interrupts anyone.

## How a component gets routed

Nothing is configured per component — not here, not in DNS, not in Traefik.
When central-deploy creates a container it calls
[`traefik_labels()`][robotsix_central_deploy.registry.traefik_labels.traefik_labels],
which derives the labels from the component's `id` and its first container port.
Onboard a component and it is live at `<id>.<base-domain>`.

Three routers are emitted, ordered by priority so the most specific wins:

| Priority | Router | Matches | Authenticated by |
|---|---|---|---|
| 30 | `<id>-health` | `GET /health` | nothing — the [health-endpoint standard](https://damien-robotsix.github.io/robotsix-standards/health-endpoints/) requires an uncredentialed probe |
| 20 | `<id>-machine` | an `Authorization: Basic` header | Traefik's `basicauth` middleware |
| 10 | `<id>` | everything else | tinyauth SSO via `forwardAuth` |

### Why two authenticated routers

tinyauth answers an unauthenticated request with a redirect to a login page.
A browser follows it; a script cannot. The chat agent probes fleet UIs over
their public URLs with server-injected Basic credentials (`fleet_auth.auth_hosts`
in robotsix-chat), and would break against an SSO-only edge.

tinyauth has no bypass mechanism, so machine callers get their own
higher-priority router. That router is **not** a bypass — it carries Traefik's
`basicauth` middleware, so it is a second door with its own lock.

The credential lives in `deploy/traefik/fleet-users`, which is **git-ignored**:
this repo is public, and a committed bcrypt hash is an offline-crackable copy of
the fleet password. Traefik reads the standard htpasswd formats, so the existing
existing nginx htpasswd can be copied across unchanged and the
current password keeps working.

### What does not get routed

`traefik_labels()` returns no labels at all — so Traefik ignores the container —
when the component has no port, when no base domain is configured, or when
`routable` is false. Sibling services set `routable=False`: publishing a
component's database at `<component>-db.<base-domain>` would put it on the
public internet behind nothing but the SSO gate.

## Components ship no auth of their own

This is a fleet-wide rule, not a central-deploy convention — see
[the component standard](https://damien-robotsix.github.io/robotsix-standards/component-standard/).
A component behind the edge only ever receives authenticated requests, so a
second login (or a second bearer token) is just another credential to
provision, rotate, and get wrong.

central-deploy holds itself to the same rule: it has no login page and no
session store. `verify_auth` remains on the JSON API as defence-in-depth
against a caller already on the internal Docker network.

## TLS

One wildcard certificate covers the whole fleet, obtained by Traefik over the
ACME **DNS-01** challenge against OVH — the only challenge type that can issue
a wildcard. It is requested once at the `websecure` entrypoint rather than
per-router, so a day with several onboardings does not turn into several ACME
orders.

Credentials come from the environment (`OVH_*`, see `.env.example`), never from
a committed file. There is no certbot, no renewal cron, and no per-component
certificate.

### Secrets and the two credential files

Three values never enter git, because this repo is public: the machine
credential (`deploy/traefik/fleet-users`), the SSO secret and operator login
(`deploy/traefik/tinyauth.env`), and the OVH API credentials (`.env`).

The SSO file is read with compose's `format: raw`, which disables interpolation.
That is not a stylistic choice. Routing a bcrypt hash through `.env` and
`environment: "${VAR}"` interpolates it **twice** — once reading `.env`, once
resolving the compose file — so `$2y$05$abc` loses `$05` and `$abc` to unset
variables and tinyauth silently receives a truncated hash that no password can
match. With `format: raw` the hash is written exactly as `htpasswd` emits it.

### The base domain

The domain itself comes from `GATEWAY_BASE_DOMAIN` in `.env`. It **must** match
central-deploy's own `gateway_base_domain` setting, since that is what the
routing labels are derived from — if the two disagree, components get routes for
a hostname the certificate does not cover. Compose fails the `up` outright when
the variable is unset rather than starting a certless edge.

Traefik's static configuration lives in the `command:` block of
`docker-compose.yml`, not in a `traefik.yml`. Traefik does not merge
static-config sources — the docs are explicit that mixing a file with `TRAEFIK_*`
environment variables "is not supported and can lead to unexpected behavior" — so
a file would have meant hard-coding the domain in the repo. As `command:` args,
compose interpolates them from `.env` and nothing deployment-specific is
committed. The `OVH_*` credentials remain environment variables because the ACME
provider reads them directly, not through the config parser.

## Version constraint

Traefik must be **v3.6 or newer**; the compose file tracks the current minor
line (v3.7 at the time of writing). Docker Engine 29 dropped support for API
versions below 1.40, and Traefik up to v3.5 asks its docker client for 1.24, so
every provider call comes back `client version 1.24 is too old`. The docker
provider then never loads and no container route is ever published — the edge
answers, but with a default certificate and 404 for everything.

Measured against Docker 29.6.1: v3.3, v3.4 and v3.5 all fail this way; v3.6 and
v3.7 discover containers normally. Neither a socket-proxy setting nor
`DOCKER_API_VERSION` works around it, because the version Traefik requests is
compiled in.

## Constraints this deployment is pinned to

Every value below was established by a failure during the initial cutover.
They are configuration, not folklore — each one is set in `docker-compose.yml`
with a comment pointing back here.

| Setting | Value | Why it is not free to change |
|---|---|---|
| `image` | `traefik:v3.7` | v3.5 and older ask the Docker API for 1.24; Engine 29 rejects anything below 1.40, so the provider never loads and no route is published |
| `traefik-socket-proxy` `VERSION` | `1` | The Docker client negotiates an API version through `/version` and `/_ping` before any other call |
| `tinyauth` app URL | `TINYAUTH_APPURL` | The `TINYAUTH_APP_URL` spelling is silently ignored; tinyauth then crash-loops on an empty URL and the edge 500s |
| SSO credentials | `env_file` + `format: raw` | Routing a bcrypt hash through `.env` interpolates it twice and truncates it |
| `central-deploy-proxy` | driver + name only | Any extra key — even a matching `ipam` subnet — makes compose recreate the network and detach every managed container |
| `GATEWAY_BASE_DOMAIN` | required, no default | Must equal central-deploy's `gateway_base_domain`; unset produces empty `Host()` rules and a certless edge |

### Verifying an edge change

`docker compose config` proves nothing here. It validated cleanly through the
truncated credential, the network recreation, and the dead Docker provider
alike, and it re-escapes `$` as `$$` in both YAML and JSON output so it cannot
even be used to inspect a hash. Two checks that do work:

```bash
# does a value actually reach the container?
docker compose run --rm --entrypoint sh <svc> -c 'echo "$VAR"'

# has the edge actually discovered routes? (a 404 here is a dead provider)
curl -s -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0' \
  -H 'Accept: text/html' https://<component>.<base-domain>/
```

A browser gets `302` to the SSO login. A plain `curl` gets `401` — tinyauth
distinguishes the two, so **`401` is a healthy answer**, not a failure. `404`
means no router exists.

## Operating it

| Task | Where |
|---|---|
| Change the auth gates | `deploy/traefik/dynamic.yml` — watched, applies live |
| Change entrypoints, TLS, or providers | the `command:` block in `docker-compose.yml` — needs a Traefik restart |
| Rotate the machine credential | `htpasswd -B deploy/traefik/fleet-users fleet` (Traefik re-reads it live) |
| Add an operator login | `htpasswd -nbB <user> '<password>'` → `deploy/traefik/tinyauth.env` |
| See why a route is missing | `docker inspect <container> --format '{{json .Config.Labels}}'` |

Traefik reads the Docker API through its own read-only socket proxy
(`CONTAINERS` and `EVENTS` only). It never receives the write scopes
central-deploy needs, so a compromised edge cannot start, stop, or replace a
container.
