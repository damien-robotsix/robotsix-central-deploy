# central-deploy Docker Compose Contract

> Version 1 — 2026-06-27

This document is the authoritative specification for the `docker-compose.yml`
shape required in any service repository managed by
[central-deploy](https://deploy.robotsix.net).  Service repositories **MUST**
conform to this contract before the UI onboarding flow will accept them.

---

## § 1  Purpose and versioning

Every conforming compose file **must** include a machine-readable version
header as the very first comment:

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
  parser only knows `1`) cause the parser to emit a **warning** but proceed
  — never a hard error.  New contract versions are always backward-compatible
  with the previous parser.
- Missing version header → proceed with a warning, assuming version `1`.

---

## § 2  Structural rules

1. **Exactly one service.**  The compose file MUST contain exactly **one**
   entry under `services:`.  Multiple service entries are a **parse error**.

2. **Service key = component id.**  The service key (e.g. `chat`,
   `cost-monitor`) becomes the component `id` and is used as the default
   `container_name`.  Must match: `^[a-z0-9][a-z0-9-]*$` (same constraint as
   `ComponentConfig.id`).

3. **container_name override.**  If the compose also declares
   `container_name:` on the service, **that value** overrides the default
   `container_name`; the service key remains the component `id`.

4. **Permitted top-level keys.**  `version` (optional, silently ignored),
   `services`, `volumes`.  All other top-level keys are silently ignored.

---

## § 3  Required fields

| Compose path            | Rule |
|-------------------------|------|
| `services.<name>.image` | **Required.** Must be a GHCR image ref in the form `ghcr.io/damien-robotsix/<repo>:<tag>` (e.g. `ghcr.io/damien-robotsix/chat:main`).  Central-deploy pulls this ref verbatim; no local build is performed.  Missing or non-GHCR values are a **parse error**. |

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

- Key-value pairs where the value MUST be `""` (empty string) or a clearly
  non-secret placeholder (e.g. `"enter-in-ui"`).
- Central-deploy stores **only the keys** from the compose.  Actual secret
  values are entered via the UI and stored in central-deploy's persisted
  secret store (not in the compose file).
- Maps to `ComponentConfig.env` key set (values are filled at onboarding /
  deploy time from the UI).

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
- `driver: local` is the **only** supported driver.  Any other `driver` value
  is a **parse error**.
- `driver_opts` is silently ignored (central-deploy creates volumes with
  default options).
- `external: true` on a volume definition is silently ignored — all volumes
  are managed by central-deploy.

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
| `services.<name>.build`                        | **Parse error.** Only pre-built GHCR images are supported (`BUILD=0` on socket-proxy). |
| `services.<name>.depends_on`                   | Silently ignored. |
| `services.<name>.networks`                     | Silently ignored.  Central-deploy manages container networking. |
| `services.<name>.command` / `entrypoint`       | Silently ignored.  Image CMD/entrypoint is used as-is. |
| Multiple `services:` entries                   | **Parse error.** Exactly one service is required. |
| Host bind-mount in `volumes` (without `claude-mount` label) | **Parse error.** |
| `volumes.<name>.driver` ≠ `local`              | **Parse error.** |
| Top-level keys other than `version`, `services`, `volumes` | Silently ignored. |
| `version` (top-level compose version string)   | Silently ignored. |
| Labels outside `robotsix.deploy.*` namespace   | Silently ignored. |

> **"Silently ignored"** means: parsed but not stored; no warning to the user.
>
> **"Parse error"** means: onboarding is blocked and the error message is
> surfaced in the UI.

---

## § 8  Field → ComponentConfig mapping table

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
| `volumes.<name>.labels.robotsix.deploy.stateful: "true"` | *(onboarding gate)* | Triggers blocking UI warning per volume.  Stored on the component spec as a per-volume flag. |

---

## § 9  Annotated examples

### Example A — Stateless service (cost-monitor)

```yaml
# central-deploy-contract-version: 1
services:
  cost-monitor:
    # image: pulled from GHCR — no local build
    image: ghcr.io/damien-robotsix/cost-monitor:main
    ports:
      # host:container — host port must be unique across all managed services
      - "8200:8200"
    environment:
      # Keys only — values are entered via the central-deploy UI
      OPENAI_API_KEY: ""
      ANTHROPIC_API_KEY: ""
    # healthcheck is optional but strongly recommended
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8200/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
    # No volumes — stateless service; safe to recreate empty
    # No robotsix.deploy.claude-mount label — Claude config not required
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

---

## Appendix A — Quick reference

### Valid compose skeleton

```yaml
# central-deploy-contract-version: 1
services:
  <id>:
    image: ghcr.io/damien-robotsix/<repo>:<tag>
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
    labels:                             # optional (robotsix.deploy.* only)
      robotsix.deploy.claude-mount: "true"

volumes:                                # required iff service has named volumes
  <volume-name>:
    driver: local
    labels:
      robotsix.deploy.stateful: "true"  # optional
```

### Error classification

| Condition | Result |
|-----------|--------|
| Missing `# central-deploy-contract-version` header | Warning, assume v1 |
| Unknown contract version | Warning, proceed |
| Multiple `services:` entries | Parse error |
| `services.<name>.image` missing or non-GHCR | Parse error |
| `services.<name>.build` present | Parse error |
| Host bind-mount in `services.<name>.volumes` (path starts with `.`, `/`, or `~`) | Parse error |
| Named volume in service `volumes:` not declared in top-level `volumes:` | Parse error |
| `volumes.<name>.driver` ≠ `local` | Parse error |
| Unsupported top-level keys (`networks:`, `configs:`, `secrets:`, etc.) | Silently ignored |
| Extra labels (Docker, Traefik, custom, etc.) | Silently ignored |
