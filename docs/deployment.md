# Deployment

How central-deploy itself is deployed on `server.robotsix.net`.

!!! note "nginx / TLS / DNS are server-specific"
    Everything below the compose section describes infrastructure that lives
    **on the server**, not in this repo. It is documented here as a
    reference so the setup is reproducible, but no tooling installs or syncs
    it — the live server configuration is authoritative.
    [`nginx-deploy.conf`](nginx-deploy.conf) mirrors the deployed vhosts.

## Application (docker compose)

The service runs from the repo's `docker-compose.yml`:

```bash
git clone https://github.com/damien-robotsix/robotsix-central-deploy.git
cd robotsix-central-deploy
ROBOTSIX_LIFECYCLE_AUTH_USERNAME=admin \
ROBOTSIX_LIFECYCLE_AUTH_PASSWORD=... \
docker compose up -d --build
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
| `deploy.robotsix.net` | A | Dashboard + path-based gateway |
| `*.deploy.robotsix.net` | A (wildcard) | Subdomain-based gateway — every component, present and future |

Because of the wildcard record and the wildcard vhost below, **onboarding a
new component requires no DNS or nginx change**: the gateway resolves
`<name>.deploy.robotsix.net` from the `Host` header at runtime
(`ROBOTSIX_LIFECYCLE_GATEWAY_BASE_DOMAIN=deploy.robotsix.net`, also settable
from the dashboard settings).

## nginx

Deployed files (see [`nginx-deploy.conf`](nginx-deploy.conf) for contents):

| File | Role |
|------|------|
| `/etc/nginx/conf.d/websocket-upgrade.conf` | `map $http_upgrade $connection_upgrade` — WebSocket upgrade support for the gateway relay |
| `/etc/nginx/sites-available/deploy.robotsix.net` | Main vhost: dashboard, `/health` open, path-based gateway |
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
                        │ path- or Host-based routing
                        ▼
                managed component containers
```
