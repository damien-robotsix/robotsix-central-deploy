# central-deploy Docker Compose Contract

> Version 1 — 2026-06-27

This document is the authoritative specification for the `docker-compose.yml`
shape required in any service repository managed by
[central-deploy](https://deploy.robotsix.net).  Service repositories **MUST**
conform to this contract before the UI onboarding flow will accept them.

---

## § 0  Repository layout

centrally-deploy expects the following files in a conforming service repo:

| Path | Role |
|------|------|
| `deploy/docker-compose.yml` | **Deploy compose** (this contract). Required. |
| `config/config.yaml` | Runtime config schema (§ 8). Optional. |
| `docker-compose.yml` (repo root) | Dev compose — **ignored by central-deploy**. |

The root `docker-compose.yml` is every repo's local-dev compose (may contain
`build:`, host bind-mounts, env-var ports). central-deploy never reads it.
Service repos keep it untouched; only `deploy/docker-compose.yml` must be
contract-compliant.

---

## § 1  Purpose and versioning

Every conforming compose file **must** include a machine-readable version
header as a line (conventionally the first line):

```yaml
# central-deploy-contract-version: 1
```

**Versioning semantics**

- The integer increments only when the contract gains a **breaking change**
  (a field that was previously ignored becomes required, a previously valid
  value becomes a parse error, etc.).
- Additions that are backward-compatible (new optional fields, new labels)
  do **not** require a version bump.
- **Unknown versions** (e.g. `# central-deploy-contract-version: 99` when the
  parser only knows `1`) cause a **parse error** — the exact v1 header line
  must be present.
- Missing version header → **parse error**.

---

## § 2  Structural rules

1. **One or more services.**  A compose file MUST contain at least one
   `services:` entry.  When exactly one service is present it is implicitly
   the **primary** — no label is required.  When two or more services are
   present, exactly one service MUST carry the label
   `robotsix.deploy.primary: "true"`; zero or more than one primary label
   is a **parse error**.

2. **Sibling container naming.** For each non-primary service the derived
   container name is `<component-id>-<service-key>` (e.g. component
   `auto-mail`, service key `ingester` → container `auto-mail-ingester`).
   A `container_name:` override on the service replaces this default. The
   primary service container name defaults to the component id
   (user-supplied `name`) or its own `container_name:` override.

3. **Service key = component id.**  The primary service key (e.g. `chat`,
   `cost-monitor`) becomes the component `id` and is used as the default
   `container_name`.  Must match: `^[a-z0-9][a-z0-9-]*$` (same constraint as
   `ComponentConfig.id`).  All sibling service keys must also match this
   pattern.

4. **container_name override.**  If the compose also declares
   `container_name:` on the service, **that value** overrides the default
   `container_name`; the service key remains the component `id`.

5. **Permitted top-level keys.**  `version` (optional, silently ignored),
   `services`, `volumes`.  All other top-level keys are silently ignored.

---

## § 3  Required fields

| Compose path            | Rule |
|-------------------------|------|
| `services.<name>.image` | **Required.** Must be a non-empty string (a GHCR image ref such as `ghcr.io/damien-robotsix/<repo>:<tag>` is recommended, but the parser accepts any image reference).  Central-deploy pulls this ref verbatim; no local build is performed.  Missing or blank values are a **parse error**. |

---

## § 4  Optional fields

### `services.<name>.ports`

- **Short syntax:** `"<host>:<container>"` or `"<host>:<container>/<proto>"`
  (proto: `tcp` | `udp`; default `tcp`).
- **Long syntax** (`target:`, `published:`, `protocol:`) is also accepted.
- Host port is recorded as-is; uniqueness across managed components is
  enforced at **onboarding time**, not at parse time.
- Maps to `ComponentConfig.ports` → `list[PortMapping(host, container,
  protocol)]`.

### `services.<name>.volumes` (service-level)

- Permitted syntax: `<volume-name>:<container-path>` or
  `<volume-name>:<container-path>:ro`.
- **Named volumes only.**  Any entry whose source begins with `.`, `/`, or
  `~` is a **parse error** — host bind-mounts are not permitted except via the
  `robotsix.deploy.claude-mount` label (§ 5).
- Each named volume referenced here MUST also appear in the top-level
  `volumes:` section; absence is a **parse error**.
- Maps to `ComponentConfig.mounts` →
  `list[VolumeMount(host=<volume-name>, container=<path>, read_only=<bool>)]`.

### `services.<name>.environment`

- Key-value pairs.  Empty-value entries (e.g. `KEY=` or `{KEY: null}`) are
  **secret slots** that the operator fills in the review UI.
- Non-empty values (e.g. `KEY=default`, `{KEY: "default"}`) are **pre-filled
  defaults** shown in the UI and may be overridden.
- Maps to `ComponentConfig.env` key set (values are filled at onboarding /
  deploy time from the UI).
- After onboarding, operators can view and edit env vars and secrets via
  the **dashboard env/secrets config modal** (Config button per component
  row → `GET/PUT/DELETE /services/{name}/env`).  Plaintext env values are
  shown in editable inputs; secret keys show `***` with a "Set new value"
  password field.

### `services.<name>.healthcheck`

- Standard Docker Compose healthcheck block: `test`, `interval`, `timeout`,
  `retries`, `start_period`.
- Duration strings use **Go format**: `30s`, `1m30s`, etc.
- The `test` list must use CMD form: `["CMD", …]` or `["CMD-SHELL", "…"]`.
  `NONE` (disabling an inherited healthcheck) is silently treated as no
  healthcheck.
- Omitting `healthcheck` entirely is permitted; the component deploys
  without a Docker-level healthcheck.
- Maps to `ComponentConfig.health_check` →
  `HealthCheck(test, interval_seconds, timeout_seconds, retries,
  start_period_seconds)`.  Durations are converted from Go duration strings
  to **integer seconds** by the parser.

### `services.<name>.container_name`

- Optional override; see § 2 for semantics.

### `services.<name>.labels`

- All labels outside the `robotsix.deploy.*` namespace are silently ignored.
- `robotsix.deploy.*` labels are defined in § 5.

---

## § 5  Extension labels (`robotsix.deploy.*`)

### `robotsix.deploy.primary: "true"` (service-level)

Required when the compose file contains more than one service. Designates
this service as the primary: its first host port receives path-based gateway
traffic (`deploy.robotsix.net/<name>/*`), and its Docker health state is the
component health reported in the dashboard. Exactly one service may carry
this label; presence on multiple services or absence when N>1 are **parse
errors**. Silently ignored when the compose file has only one service.

### `robotsix.deploy.claude-mount: "true"` (service-level)

This is the **single permitted host bind-mount** in the contract.

- When present with value `"true"`, central-deploy injects an additional
  bind-mount at run time:
  - **Host path:** `~/.claude` (resolved relative to the server user running
    central-deploy)
  - **Container path:** `/root/.claude`
  - **Mode:** read-write (Claude Code writes state to this directory)
- This mount does **not** appear in the compose `volumes:` list and MUST NOT
  be declared in the top-level `volumes:` section.
- At onboarding, the UI MUST display a confirmation toggle:

  > ☑ Mount Claude configuration directory (`~/.claude` → `/root/.claude`)

  The checkbox is **pre-checked** to the value declared in the compose label,
  but the operator may override it before saving.  The toggled value is stored
  in central-deploy's persisted component spec and takes effect on the next
  start.

### `robotsix.deploy.config-target` (service-level)

Full in-container path to the config file the app reads (e.g.
`/home/mailbot/config/config.yaml`). **Required** when the repo contains
`config/config.yaml`. The dirname must match the `container:` path of a
named-volume mount in the same service — central-deploy resolves the volume
name from that mount and writes the merged config into it before starting the
container. Preflight returns an error if this label is missing.

Example:
```yaml
services:
  mailbot:
    labels:
      robotsix.deploy.config-target: "/home/mailbot/config/config.yaml"
    volumes:
      - mailbot-config:/home/mailbot/config
```

---

## § 6  Volume declarations and stateful-volume flagging

Top-level `volumes:` section (**required** when any named volume is
referenced by the service):

```yaml
volumes:
  my-data:                     # volume name referenced in services.<name>.volumes
    driver: local              # optional; only "local" is supported; default if omitted
    labels:
      robotsix.deploy.stateful: "true"   # marks this volume as containing persistent state
```

- Each named volume referenced by the contract service MUST be declared here;
  absence is a **parse error**.
- `driver` (when present) **must** be `"local"`; any other value is a **parse
  error**.  Omitting `driver` defaults to `local`.
- `driver_opts` and `external` are silently ignored.

### Stateful-volume flag

- The optional label `robotsix.deploy.stateful: "true"` on a **volume
  definition** (not the service) tells central-deploy that this volume
  contains persistent data that cannot be recreated from the image (e.g. a
  database, Radicale calendar data, uploaded files).
- At onboarding, for **every** volume carrying this label, the UI MUST show a
  **blocking confirmation** before proceeding:

  > ⚠ Volume `<name>` is marked stateful. It will start **EMPTY** on first
  > deploy. Migrate existing data before proceeding, or confirm you accept
  > starting fresh.

  The operator must explicitly acknowledge each such warning; the "Deploy"
  button remains **disabled** until all stateful-volume warnings are
  dismissed.
- Volumes **without** the stateful label are treated as ephemeral caches —
  safe to create empty with no warning.

---

## § 7  Ignored and prohibited fields

| Compose field                                  | Parser behaviour |
|------------------------------------------------|------------------|
| `services.<name>.restart`                      | Silently ignored.  Central-deploy always applies `RestartPolicy: unless-stopped`. |
| `services.<name>.build`                        | **Parse error.** Only pre-built images are supported (`BUILD=0` on socket-proxy). |
| `services.<name>.depends_on`                   | Silently ignored. |
| `services.<name>.networks`                     | Silently ignored.  Central-deploy manages container networking. |
| `services.<name>.command` / `entrypoint`       | Silently ignored.  Image CMD/entrypoint is used as-is. |
| N>1 services, no service has `robotsix.deploy.primary: "true"` | **Parse error.** |
| N>1 services, multiple services have `robotsix.deploy.primary: "true"` | **Parse error.** |
| Host bind-mount in `volumes` (without `claude-mount` label) | **Parse error.** |
| Top-level keys other than `version`, `services`, `volumes` | Silently ignored. |
| `version` (top-level compose version string)   | Silently ignored. |
| Labels outside `robotsix.deploy.*` namespace   | Silently ignored. |

> **"Silently ignored"** means: parsed but not stored; no warning to the user.
>
> **"Parse error"** means: onboarding is blocked and the error message is
> surfaced in the UI.

---

## § 8  config/config.yaml — unified configuration schema

### Presence
Optional. If `config/config.yaml` exists at the repo root, central-deploy fetches it at
`POST /onboard/preflight` (alongside `deploy/docker-compose.yml`) and returns the parsed schema
in the preflight response.

### Structure
Must be a YAML mapping (dict) at the top level. Nested mappings become UI sections.
Scalar leaf values are runtime defaults.

### Secret-field convention
A leaf with value `""` (empty string) or `null`/`~` is a **secret field**:
- Rendered as a masked password input in the configuration UI
- Stored in central-deploy's data volume; never echoed back in GET responses
  (masked as `"***"` in the `current` dict)
- Preserved on save if the submitted value is the sentinel `"***"` (unchanged)

### Example `config/config.yaml`

```yaml
server:
  host: localhost
  port: 8080
smtp:
  host: smtp.example.com
  user: ""      # secret — blank → masked input
  password: "" # secret — blank → masked input
log_level: info
```

### Deploy compose requirement
The deploy compose MUST include the `robotsix.deploy.config-target` label (see §5) on the
primary service when `config/config.yaml` is present. The label value is the full
in-container path of the config file (e.g. `/home/mailbot/config/config.yaml`). The
dirname must match the container-side path of exactly one named-volume mount in that
service. Central-deploy resolves the volume name from this mount and writes
`config.yaml` into it before starting the container. Preflight returns an error if
the label is missing or mismatched.

### Example deploy compose snippet

```yaml
services:
  myapp:
    image: ghcr.io/org/myapp:latest
    labels:
      robotsix.deploy.config-target: "/app/config/config.yaml"
    volumes:
      - myapp-config:/app/config
volumes:
  myapp-config:
    labels:
      robotsix.deploy.stateful: "true"
```

### Round-trip guarantee
At onboard: template defaults written to volume.
On each config save: merged values (defaults + user edits) re-written to volume.
Service reads only from the mounted volume; central-deploy never uses host bind-mounts.

---

## § 9  Field → ComponentConfig mapping table

Reference: `src/robotsix_central_deploy/registry/models.py`

| Compose field | `ComponentConfig` field | Conversion notes |
|---|---|---|
| service key | `id: str` | Must match `^[a-z0-9][a-z0-9-]*$`. |
| `container_name` (or service key) | `container_name: str` | Defaults to service key if absent. |
| `services.<name>.image` | `image: str` | Verbatim GHCR ref. |
| `services.<name>.ports[*]` | `ports: list[PortMapping]` | Short/long syntax → `PortMapping(host=<published>, container=<target>, protocol=<tcp\|udp>)`. |
| `services.<name>.volumes[*]` (named) | `mounts: list[VolumeMount]` | `VolumeMount(host=<volume-name>, container=<path>, read_only=<bool>)`.  Host bind-mounts are rejected unless via `claude-mount` label. |
| `services.<name>.environment` keys | `env: dict[str, str]` | Values stored as `""` until set via UI. |
| `services.<name>.healthcheck` | `health_check: Optional[HealthCheck]` | Durations (Go strings) → integer seconds.  `HealthCheck(test, interval_seconds, timeout_seconds, retries, start_period_seconds)`. |
| `labels.robotsix.deploy.claude-mount: "true"` | *(runtime injection only)* | Added at deploy time as `VolumeMount(host="~/.claude", container="/root/.claude", read_only=False)`.  **Not** stored in `ComponentConfig.mounts`. |
| `labels.robotsix.deploy.primary: "true"` | *(parser gate)* | Designates primary service. Not stored in `ComponentConfig`; drives the sibling split. |
| `labels.robotsix.deploy.config-target` | `ComponentConfig.config_volume` | Full in-container path to config.yaml. Resolved to the named-volume name from the matching volume mount. Required when `config/config.yaml` is present. |
| Non-primary service entire block | `ComponentConfig.siblings[*]` (`ServiceConfig`) | One `ServiceConfig` per sibling. |
| `volumes.<name>.labels.robotsix.deploy.stateful: "true"` | *(onboarding gate)* | Triggers blocking UI warning per volume.  Stored on the component spec as a per-volume flag. |

---

## § 10  Annotated examples

### Example A — Stateless service with Claude mount (cost-monitor)

```yaml
# central-deploy-contract-version: 1
services:
  cost-monitor:
    image: ghcr.io/damien-robotsix/cost-monitor:main
    labels:
      robotsix.deploy.claude-mount: "true"
    ports:
      - "8200:8200"
    volumes:
      - cost-data:/data
    environment:
      - OPENAI_API_KEY=
      - DATABASE_URL=
      - DEBUG=false
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8200/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
volumes:
  cost-data:
    labels:
      robotsix.deploy.stateful: "true"
```

### Example B — Stateful service with Claude host mount (chat)

```yaml
# central-deploy-contract-version: 1
services:
  chat:
    image: ghcr.io/damien-robotsix/chat:main
    ports:
      - "3000:3000"
    volumes:
      # Named volume only — no ./ or / host paths permitted here
      - chat-data:/app/data
    environment:
      ANTHROPIC_API_KEY: ""
      AUTH_SECRET: ""
    labels:
      # Enables ~/.claude:/root/.claude:rw bind-mount at run time
      # (the single permitted host bind-mount exception)
      robotsix.deploy.claude-mount: "true"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 20s

volumes:
  chat-data:
    driver: local   # only supported driver
    labels:
      # Marks this volume as containing persistent state.
      # central-deploy will show a blocking warning at onboarding:
      # "Volume chat-data will start EMPTY — migrate existing data."
      robotsix.deploy.stateful: "true"
```

### Example C — Two-service compose with primary label (auto-mail)

```yaml
# central-deploy-contract-version: 1
services:
  board:
    image: ghcr.io/damien-robotsix/auto-mail:main
    labels:
      robotsix.deploy.primary: "true"
    ports:
      - "8202:8080"
    environment:
      SMTP_HOST: ""
      AUTH_TOKEN: ""
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s

  ingester:
    image: ghcr.io/damien-robotsix/auto-mail-ingester:main
    environment:
      BOARD_URL: ""
      IMAP_PASSWORD: ""
    volumes:
      - mail-spool:/data

volumes:
  mail-spool:
    labels:
      robotsix.deploy.stateful: "true"
```

In this example:
- Component id: `auto-mail` (user-supplied `name` from the preflight request).
- Primary container: `auto-mail` (or overridden by `container_name:` on `board`).
- Sibling container: `auto-mail-ingester` (derived from `<name>-ingester`).
- Gateway route: `deploy.robotsix.net/auto-mail/*` → primary's port 8202.
- `mail-spool` volume declared at top level and flagged stateful → UI warning at onboard.

---

## Appendix A — Quick reference

> **File location:** `deploy/docker-compose.yml` inside the service repo
> (the repo root `docker-compose.yml` is the dev compose and is ignored).

### Valid compose skeleton

```yaml
# central-deploy-contract-version: 1
services:
  <id>:
    image: ghcr.io/damien-robotsix/<repo>:<tag>
    labels:                             # optional
      robotsix.deploy.primary: "true"   # required when N>1 services
      robotsix.deploy.claude-mount: "true"
    container_name: <override>          # optional
    ports:                              # optional
      - "<host>:<container>"
    volumes:                            # optional (named volumes only)
      - <volume-name>:<path>
    environment:                        # optional (keys only)
      <KEY>: ""
    healthcheck:                        # optional
      test: ["CMD", "..."]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

  <sibling>:                            # optional (any number of siblings)
    image: ghcr.io/damien-robotsix/<sibling-repo>:<tag>
    # ... same optional fields as primary (ports, volumes, env, etc.)

volumes:                                # required iff any service has named volumes
  <volume-name>:
    driver: local
    labels:
      robotsix.deploy.stateful: "true"  # optional
```

### Error classification

| Condition | Result |
|-----------|--------|
| Missing `# central-deploy-contract-version` header | Parse error |
| Unknown contract version | Parse error |
| N>1 services, no `robotsix.deploy.primary: "true"` | Parse error |
| N>1 services, multiple `robotsix.deploy.primary: "true"` | Parse error |
| `services.<name>.image` missing or blank | Parse error |
| `services.<name>.build` present | Parse error |
| Host bind-mount in `services.<name>.volumes` (path starts with `.`, `/`, or `~`) | Parse error |
| Named volume in service `volumes:` not declared in top-level `volumes:` | Parse error |
| `volumes.<name>.driver` present and not `"local"` | Parse error |
| Unsupported top-level keys (`networks:`, `configs:`, `secrets:`, etc.) | Silently ignored |
| Extra labels (Docker, Traefik, custom, etc.) | Silently ignored |
