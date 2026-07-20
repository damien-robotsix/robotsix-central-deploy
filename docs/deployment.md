# Deployment

How central-deploy itself is deployed on `server.robotsix.net`.

!!! note "nginx / TLS / DNS are server-specific"
    Everything below the compose section describes infrastructure that lives
    **on the server**, not in this repo. It is documented here as a
    reference so the setup is reproducible, but no tooling installs or syncs
    it — the live server configuration is authoritative.
    [`nginx-deploy.conf`](nginx-deploy.conf) mirrors the deployed vhosts.

## Application (docker compose)

The service runs from the repo's `docker-compose.yml`, pulling the image
published to GHCR (`ghcr.io/damien-robotsix/robotsix-central-deploy:main`,
built by `.github/workflows/release.yml` on every push to main):

```bash
git clone https://github.com/damien-robotsix/robotsix-central-deploy.git
cd robotsix-central-deploy
docker compose pull
docker compose up -d
```

To update: `docker compose pull && docker compose up -d` (add `--build` only
for local development builds from the checkout).

!!! warning "One-time migration: seed `/data/config.json`"
    Since the robotsix_config migration the server reads **all** of its own
    configuration from one JSON file — `ROBOTSIX_LIFECYCLE_*` environment
    variables are ignored. The compose file points `ROBOTSIX_CONFIG_FILE` at
    `/data/config.json`; seed it on the data volume before the first start of
    a post-migration image (values mirror the old env vars — full field list
    in the committed `config/config.json`):

```bash
    docker run --rm -i -v central_deploy_data:/data alpine sh -c \
      'cat > /data/config.json && chmod 600 /data/config.json && chown 1000:1000 /data/config.json' << 'EOF'
    {
      "auth_username": "admin",
      "auth_password": "...",
      "store_backend": "file",
      "store_path": "/data/lifecycle_state.yaml",
      "component_config_store_path": "/data/component_configs.json",
      "docker_socket_url": "tcp://socket-proxy:2375",
      "env_store_path": "/data/component_env.json",
      "secret_key_path": "/data/secrets.key",
      "config_yaml_store_path": "/data/component_config_yaml.json",
      "system_settings_path": "/data/system_settings.json",
      "disk_path": "/host_root"
    }
    EOF
    ```

    Without this file the baked-in defaults apply (unix docker socket,
    in-memory store) and startup fails against the socket proxy.

!!! warning "One-time migration: `/data` volume ownership"
    The container now runs as a non-root user (uid 1000). A
    `central_deploy_data` volume created by an older root-running deployment
    holds root-owned files the new image cannot write. Before the first
    non-root start, run:

```bash
    docker compose down
    docker run --rm -v central_deploy_data:/data alpine chown -R 1000:1000 /data
    ```

This starts two containers:

- **socket-proxy** — [tecnativa/docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy)
  with only the API scopes central-deploy needs (see [index](index.md)).
- **central-deploy** — the lifecycle server, bound to `127.0.0.1:8100`
  (only reachable through nginx).

State (component configs, env/secrets, Fernet key, settings) persists in the
`central_deploy_data` named volume.

## DNS

Two records in the `robotsix.net` zone (OVH), both pointing at the server:

| Record | Type | Purpose |
|--------|------|---------|
| `deploy.robotsix.net` | A | Dashboard (legacy `/<name>/…` URLs redirect to subdomains) |
| `*.deploy.robotsix.net` | A (wildcard) | Subdomain-based gateway — every component, present and future |

Because of the wildcard record and the wildcard vhost below, **onboarding a
new component requires no DNS or nginx change**: the gateway resolves
`<name>.deploy.robotsix.net` from the `Host` header at runtime
(`gateway_base_domain` in `/data/config.json`, also settable from the
dashboard settings).

## nginx

Deployed files (see [`nginx-deploy.conf`](nginx-deploy.conf) for contents):

| File | Role |
| ------ | ------ |
| `/etc/nginx/conf.d/websocket-upgrade.conf` | `map $http_upgrade $connection_upgrade` — WebSocket upgrade support for the gateway relay |
| `/etc/nginx/sites-available/deploy.robotsix.net` | Main vhost: dashboard, `/health` open |
| `/etc/nginx/sites-available/wildcard.deploy.robotsix.net` | Catch-all `*.deploy.robotsix.net` vhost for component subdomains |
| `/etc/nginx/htpasswd/deploy.robotsix.net` | Basic-auth credentials (defense-in-depth in front of the app's own auth) |

```bash
htpasswd -c /etc/nginx/htpasswd/deploy.robotsix.net <username>
ln -s /etc/nginx/sites-available/deploy.robotsix.net /etc/nginx/sites-enabled/
ln -s /etc/nginx/sites-available/wildcard.deploy.robotsix.net /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

## TLS certificates

Two certbot certificates:

**Base domain** (`deploy.robotsix.net`) — standard HTTP-01 via the nginx
authenticator:

```bash
certbot --nginx -d deploy.robotsix.net
```

**Wildcard** (`*.deploy.robotsix.net`) — wildcards require the **DNS-01**
challenge, done with the OVH plugin:

```bash
apt-get install python3-certbot-dns-ovh

# OVH API token created at https://www.ovh.com/auth/api/createToken
# with GET/PUT/POST/DELETE rights on /domain/zone/robotsix.net/*
cat > /root/.secrets/certbot/ovh.ini <<'INI'
dns_ovh_endpoint = ovh-eu
dns_ovh_application_key = <application key>
dns_ovh_application_secret = <application secret>
dns_ovh_consumer_key = <consumer key>
INI
chmod 600 /root/.secrets/certbot/ovh.ini

certbot certonly --dns-ovh \
  --dns-ovh-credentials /root/.secrets/certbot/ovh.ini \
  --cert-name wildcard.deploy.robotsix.net \
  -d '*.deploy.robotsix.net'
```

Renewal is automatic for both (certbot systemd timer); the DNS-01 renewal
reuses the credentials file.

## Request flow summary

```
browser ── https ──> nginx (basic auth, TLS, WS upgrade)
                        │ proxy_pass 127.0.0.1:8100
                        ▼
                central-deploy (session/API auth)
                        │ Host-based (subdomain) routing
                        ▼
                managed component containers
```

## Claude authentication

Components that set `claude_mount: true` mount the `claude-auth` Docker named
volume at `/home/app/.claude` inside the container (read-write).  The volume
holds Anthropic OAuth credentials (`.credentials.json`) that allow the
component to make authenticated Claude API calls.

### Provisioning credentials

Credentials are managed from the **Claude auth** panel on the central-deploy
dashboard (`/ui` → "Claude Auth" section).  Two methods are available:

1. **Interactive OAuth login** (recommended).  Click "Log in with Claude".
   The server generates a PKCE challenge and the panel shows an OAuth
   authorization URL — open it, authorize, and Anthropic's callback page
   displays an authorization code.  Paste that code back into the panel;
   central-deploy exchanges it for OAuth tokens and writes
   `.credentials.json` into the `claude-auth` volume (ownership
   `1000:1000`, mode `0600`).  The whole flow runs inside central-deploy —
   no helper container is involved.  A redirect straight back to the
   dashboard is not possible: the OAuth client only whitelists Anthropic's
   own callback page.

2. **Paste credentials JSON** (fallback).  Expand "Paste credentials JSON"
   and paste the contents of a `.credentials.json` file obtained elsewhere
   (e.g. from `claude setup-token` or a login on a developer machine).  The
   file is written into the volume with ownership `1000:1000` and
   permissions `0600`.

### OAuth refresh-token rotation caveat

Anthropic OAuth credentials include a refresh token that can be rotated by
the server at any time (e.g. after a password change or security event).
When this happens the stored `.credentials.json` becomes invalid and the
component will report "Not logged in".  **There is no automatic refresh** —
the operator must re-run the login flow through the Claude auth panel to
provision fresh credentials.  This is an expected maintenance task; the
dashboard status panel shows the current authentication state so the
operator can detect the issue before end users report it.

## Chat access

Components can opt in to being reachable by the chat agent by setting the
`allow_chat_access` flag.  When enabled, the component must expose a
`GET /chat-skill` endpoint that returns a Markdown body describing how the
chat agent should interact with it (the *skill*).

The chat agent discovers reachable components by calling the lifecycle API
at `GET /chat/components` (authentication required).  The response is a JSON
array of `{id, base_url, skill}` objects — one per component that has
`allow_chat_access = true` **and** whose skill probe returned 200.  Skill
bodies are cached for 60 seconds; a component whose probe fails is silently
omitted from the roster (sibling resilience — one failing component does not
block the whole list).

`base_url` is derived from the component's container name and first
container port (`http://<container_name>:<container_port>`), which is the
same derivation used by the caretaker's mill client.

### Enabling chat access

- **At onboard time:** check "Allow chat agent access" in the onboard modal
  (default from the compose label `robotsix.deploy.chat-access`, which
  accepts `"true"`, `"1"`, or `"yes"`).
- **Post-onboard:** open the component's Config panel (the "Config" button
  on the dashboard row), then toggle the "Allow chat agent access" checkbox
  under the Chat Access section and click Save.

The flag is stored on the component's `ComponentConfig` and persists across
redeploys.
