# Deployment

How central-deploy itself is deployed on `server.robotsix.net`.

!!! note "The edge is in this repo now"
    TLS, routing, and authentication are handled by the Traefik and tinyauth
    containers in `docker-compose.yml`, configured by `deploy/traefik/*.yml`.
    There is no nginx, no certbot, and no server-side configuration to keep in
    sync — see [The fleet edge](edge.md) for how it works. Only DNS lives
    outside the repo.

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

This starts five containers:

- **central-deploy** — the lifecycle server. Publishes no host port; reachable
  only over the `central-deploy-proxy` network, through Traefik.
- **socket-proxy** — [tecnativa/docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy)
  with only the API scopes central-deploy needs (see [index](index.md)).
- **traefik** — the fleet edge: TLS, routing, and the auth middlewares.
- **traefik-socket-proxy** — a *second*, read-only socket proxy
  (`CONTAINERS` + `EVENTS` only). Traefik must not inherit the deployer's
  write scopes, so it gets its own.
- **tinyauth** — the fleet's single sign-on gate.

State (component configs, env/secrets, Fernet key, settings) persists in the
`central_deploy_data` named volume; certificates in `traefik_letsencrypt` and
SSO sessions in `tinyauth_data`.

## DNS

Two records in the `robotsix.net` zone (OVH), both pointing at the server:

| Record | Type | Purpose |
|--------|------|---------|
| `deploy.robotsix.net` | A | The dashboard, and the ACME account host |
| `*.deploy.robotsix.net` | A (wildcard) | Every component, present and future |

The wildcard is what makes onboarding free of infrastructure work: a new
component is reachable at `<id>.deploy.robotsix.net` with no DNS change, no
certificate request, and no edge configuration — central-deploy stamps the
route onto the container and Traefik picks it up. See
[The fleet edge](edge.md).

## Edge (TLS, routing, auth)

The edge containers come up with the rest of the stack:

```bash
cp .env.example .env      # ACME email, OVH API credentials, tinyauth secret + users
docker compose up -d
```

Traefik obtains a single wildcard certificate over ACME **DNS-01** against OVH
on first start; watch `docker compose logs -f traefik` to confirm. Renewal is
automatic and needs no cron, no systemd timer, and no credentials file on the
host.

The OVH API token needs `GET`/`POST`/`DELETE` on `/domain/zone/robotsix.net/*`
and is created at <https://api.ovh.com/createToken/>.

## Request flow summary

```
browser ─── https ──▶ Traefik ─── forwardAuth ──▶ tinyauth (SSO login)
                         │
script  ─── https ──▶ Traefik ─── basicauth (fleet credential)
                         │
                         ▼  by Host, from container labels
                 component container   /   central-deploy
```

central-deploy publishes **no host port**: it is reachable only over the
`central-deploy-proxy` network, through Traefik, which authenticates first.

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

### Deploy API key provision

A chat-agent component (`chat_agent_mutatable`) calls the deploy API with the
plane's own API key.  That key is a credential of the component's application,
so it is delivered the way the config standard requires — in the component's
**config file**, never as an environment variable
([config-standard §5](https://damien-robotsix.github.io/robotsix-standards/config-standard/#5-what-environment-is-for)
forbids first-party secrets in `environment:`).

The deploy plane writes `central_deploy.api_token` into the component's config
volume at startup and after every chat-access toggle, through the same schema
guard as `fleet_auth.auth_hosts`: a component whose config schema does not
declare the key is skipped and logged, so this stays component-agnostic.  Chat
resolves the token from that key (falling back from any per-component
`central_deploy.component_credentials.<id>.header_token` override), so a
chat-agent component gets working deploy access with no Env & Secrets paste.

> **Removed 2026-08-09.** An earlier revision injected the key as a
> `DEPLOY_API_KEY` environment variable into every component with
> `allow_chat_access`, and into their siblings.  Nothing read it — chat has
> resolved deploy credentials from config since `FeedbackSettings.deploy_api_key`
> replaced the env var — so it spread an unread admin credential across the
> fleet's container environments.  If a component still carries the variable,
> it disappears on the next recreate; env is baked at container create, so a
> restart is not enough.

### Langfuse trace access

Enabling chat access (`allow_chat_access` or `chat_agent_mutatable`) on a
component also **auto-grants** the chat agent read access to that component's
Langfuse traces through the existing `/chat/langfuse/...` proxy.  When the
toggle is on, central-deploy scans the component's standardized config for the
canonical Langfuse block and registers each project as a chat-proxy alias.
Toggling off deregisters them.  Reconciliation is idempotent — re-applying the
toggle or restarting central-deploy converges to the same alias set.

Every component declares its own credentials in one block, keyed by the
Langfuse **project name** (`<repo>` for a component's main LLM function,
`<repo>-<function>` for each additional LLM-generating subsystem — see the
component standard's one-project-per-function rule):

```json
"langfuse": {
  "host": "https://langfuse.robotsix.net",
  "projects": {
    "robotsix-chat":        {"public_key": "pk-lf-…", "secret_key": "sk-lf-…", "project_id": "cm…"},
    "robotsix-chat-cognee": {"public_key": "pk-lf-…", "secret_key": "sk-lf-…", "project_id": "cm…"}
  }
}
```

`lifecycle/_langfuse_config.py` is the single reader for this shape; both the
chat trace proxy and the fleet credential registry (`GET /fleet/langfuse`,
consumed by cost-monitor) go through it, so they cannot drift apart.  There is
**no** fallback to deploy-plane `LANGFUSE_*` environment variables or to any
pre-standard layout — per the config-ownership standard, first-party
credentials live in the component's `config.json` and nowhere else.  A
component that has not migrated reports no projects, which is the intended
visible failure.

No operator key-pasting step is involved: secrets live in each component's
standardized config (or the operator-configured
`LifecycleConfig.langfuse_projects` in `config/config.json`), never transit
through the chat agent, and remain masked in all chat-visible reads.  A
component with chat access enabled but no Langfuse keys in its config
simply grants no aliases (no error).  The reconciled alias set is visible via
`GET /services/central-deploy/config` (`langfuse_projects` schema).

### Generic deploy (server-level allowlist)

Not every deployable component needs an onboarding pipeline or persisted
`ComponentConfig` from the start.  The server supports a **generic deploy**
endpoint (`POST /chat/deploy`) that lets the chat agent pull + recreate any
component whose name appears in the `chat_agent_deployable_components` list
in `config/config.json` (or equivalently the
`ROBOTSIX_LIFECYCLE_CHAT_AGENT_DEPLOYABLE_COMPONENTS` environment variable).

On first deploy, a minimal `ComponentConfig` is derived from the request body
(``name``, ``image``, optional ``container_port``), persisted automatically,
and published at the edge from its container labels.  Subsequent deploys (via the
dashboard or the chat agent) then use the stored config — this makes the
deploy target **portable**: adding a new component requires only appending its
name to the allowlist, with **no engine code change**.

Access is gated by:

1. The server-level `chat_agent_deployable_components` allowlist (403 if absent).
2. The standard `X-API-Key` auth (401 if missing/invalid).
3. Per-component rate limiting (300 s cooldown per deploy).

Health checks are configured automatically when `container_port` is supplied;
operators can customise them through the dashboard after the first deploy.
