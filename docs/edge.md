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
                     ┌─────────────┴──────────────┐
              forwardAuth                     (no auth)
               → tinyauth                       /health
                     └─────────────┬──────────────┘
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
| 10 | `<id>` | everything else | tinyauth SSO via `forwardAuth` |

### Why there is no machine door

There was one, briefly: a higher-priority router matching `Authorization: Basic`
and validated against an htpasswd file, so that scripts and fleet services could
reach components without following an SSO redirect.

It was removed because it did not work as a second lock. The htpasswd was copied
from the ingress it replaced, so every browser that had ever authenticated
against the old setup replayed the same credential automatically. The access log
showed ordinary browser sessions being served by the machine router, having never
seen a login page — the SSO gate was optional for anyone holding a password that
was already saved in their browser. A second door keyed to something every client
already has is not defence in depth.

Machine callers reach components over the internal Docker network
(`http://<container>:<port>`), which never passes through the edge and therefore
needs no edge credential at all.

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

Two files never enter git, because this repo is public: the SSO secret and
operator login (`deploy/traefik/tinyauth.env`), and the OVH API credentials
(`.env`).

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
| auth gates | tinyauth only | A second HTTP Basic door let every browser holding the old ingress credential skip SSO entirely |
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

## Redeploying a component briefly 404s it

Routes live on the container, so changing one means recreating the container.
Between the old container going away and Traefik seeing the new one, that
hostname has no router and the edge answers **404** — a few seconds, once per
redeploy.

This is inherent to label-based routing, not a fault, and it is worth knowing
because a 404 immediately after a deploy looks identical to a route that failed
to register. Retry before diagnosing: if it is still 404 after ten seconds or
so, the labels really are missing, and
`docker inspect <container> --format '{{json .Config.Labels}}'` will say so.

It also means the relabel pass that follows an edge change — redeploying every
component so it picks up new labels — takes each component offline for a moment
in turn. Nothing else is affected while it happens.

## Services that are not fleet components

A machine can host things the deployment system knows nothing about — the
previous ingress on this host served a CalDAV server, a file-sync GUI and a
password vault from its own config, none of them managed components. They have
no container labels and central-deploy will never emit routes for them.

Their routes live in `/etc/traefik-host/`, mounted into the dynamic-config
directory. That path is deliberately **outside this repo**: naming a specific
host or service here would break the repo-agnostic rule, and these routes
belong to the machine rather than to the fleet.

Two things bite when moving such a service off a host-based ingress:

- **It may be bound to `127.0.0.1`.** That was reachable from an ingress
  running on the host; it is not reachable from a container. Either rebind the
  service, or forward its port onto the Docker bridge.
- **It may check the `Host` header.** A bare `proxy_pass` in nginx sends the
  *upstream's* host by default, not the client's, and an application that
  validates `Host` will have been relying on that. Traefik preserves the
  client's `Host`, so such a service needs a `customRequestHeaders` middleware
  restoring the value it expects.

When taking over an existing ingress, enumerate what it served with
`grep -rE 'server_name' /etc/nginx/` — the enabled-sites directory is not the
whole story, as server blocks can live in the main config file too.

## Operating it

| Task | Where |
|---|---|
| Change the auth gates | `deploy/traefik/dynamic.yml` — watched, applies live |
| Change entrypoints, TLS, or providers | the `command:` block in `docker-compose.yml` — needs a Traefik restart |
| Add an operator login | `htpasswd -nbB <user> '<password>'` → `deploy/traefik/tinyauth.env` |
| See why a route is missing | `docker inspect <container> --format '{{json .Config.Labels}}'` |

Traefik reads the Docker API through its own read-only socket proxy
(`CONTAINERS` and `EVENTS` only). It never receives the write scopes
central-deploy needs, so a compromised edge cannot start, stop, or replace a
container.
