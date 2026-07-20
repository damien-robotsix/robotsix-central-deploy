# Gateway

The gateway (`src/robotsix_central_deploy/gateway/`) is a reverse proxy that
routes incoming requests to managed container services. It resolves the target
component from the `Host` header subdomain and proxies HTTP and WebSocket
traffic transparently.

## Architecture

```
Incoming request (Host: <name>.deploy.robotsix.net)
  │
  ├─ gateway_router (router.py)
  │     │
  │     ├─ _extract_subdomain_name() — parse Host header → component name
  │     ├─ _resolve() — ComponentConfigStore lookup, 404/503 on miss
  │     │
  │     ├─ HTTP routes → http_proxy() (proxy.py)
  │     │     └─ StreamingResponse via httpx.AsyncClient
  │     │
  │     ├─ WebSocket route → ws_proxy() (proxy.py)
  │     │     └─ Bidirectional asyncio relay between client and container
  │     │
  │     └─ Legacy path-prefix → 307 redirect to subdomain
  │
  └─ PROXY_NETWORK — shared Docker bridge network
```

- **`router.py`** — FastAPI `APIRouter` with three route handlers. Resolves
  component names from the Host header using the configured `gateway_base_domain`,
  then delegates to `proxy.py`. Legacy path-prefix URLs
  (`deploy.robotsix.net/<name>/...`) issue a 307 redirect to the component
  subdomain rather than proxying in-place (path-prefix proxying broke apps
  that served absolute asset URLs).
- **`proxy.py`** — Low-level HTTP and WebSocket relay logic. Strips hop-by-hop
  headers, forwards requests via `httpx.AsyncClient`, and streams the response
  back. The WebSocket relay spawns two asyncio tasks that ferry messages
  bidirectionally and cancel the slower side when one closes.

## Routing Modes

| Mode | Example | Behaviour |
| ------ | --------- | ----------- |
| Subdomain HTTP | `mail.deploy.robotsix.net/api/v1/users` | Proxy to `mail` container |
| Subdomain WebSocket | `ws://mail.deploy.robotsix.net/ws` | Bidirectional relay to `mail` container |
| Legacy path-prefix | `deploy.robotsix.net/mail/api/v1/users` | 307 redirect to `mail.deploy.robotsix.net/api/v1/users` |

## Reserved Names

Component slugs that shadow built-in central-deploy endpoints are rejected by
the router (`RESERVED_NAMES`): `ui`, `health`, `services`, `onboard`, `docs`,
`openapi.json`, `redoc`, `disk`, `settings`, `help`, `volumes`, `login`, `logout`.

## Configuration

All settings are loaded via environment variables (prefix `ROBOTSIX_LIFECYCLE_`).

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `GATEWAY_BASE_DOMAIN` | `str` | (none) | Base domain for subdomain routing (e.g. `deploy.robotsix.net`) |

## Registration

The gateway router must be registered **last** on the FastAPI app because its
catch-all routes would otherwise shadow specific API endpoints.
