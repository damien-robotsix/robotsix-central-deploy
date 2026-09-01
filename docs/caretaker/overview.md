# Caretaker

The caretaker subsystem (`src/robotsix_central_deploy/caretaker/`) is the
background maintenance agent that runs daily passes over managed components:
health checks, image-update detection and applying, volume auditing, and disk
monitoring. Orchestrated by `CaretakerScheduler`; the lifecycle server exposes
status via `GET /caretaker/status`.

## Architecture

```
CaretakerScheduler.loop()  (scheduler.py)
  │
  ├─ 1. Read current settings (caretaker_enabled, caretaker_interval_hours)
  ├─ 2. Sleep caretaker_interval_hours (min 1 hour)
  ├─ 3. run_once():
  │      ├─ phase_update()     — deploy updated images for opted-in components
  │      │   └─ auto-prune dangling images (if image_auto_prune == True)
  │      ├─ phase_health()     — probe all container health states
  │      ├─ phase_volumes()    — volume growth scan + orphan detection + disk check
  │      └─ Record findings locally (WARNING log + caretaker_findings.jsonl)
  └─ 4. Loop back to step 1; respect CancelledError for graceful shutdown
```

## Key Modules

- **`models.py`** — Pydantic data types: `FindingKind` (enum of finding
  categories), `CaretakerFinding` (single issue with component, severity,
  title, detail), `CaretakerReport` (aggregate pass result with timing and
  phases run).
- **`scheduler.py`** — `CaretakerScheduler`: long-running async orchestrator
  that reads settings each iteration, sleeps the configured interval, and
  invokes all three phases via `run_once()`. Exposes `get_status()` for the
  API endpoint.
- **`phases.py`** — Three independent async phase functions:
  `phase_update()` (deploy updated images, auto-prune), `phase_health()`
  (Docker health probe), `phase_volumes()` (volume audit, orphan detection,
  disk threshold check). Each returns `list[CaretakerFinding]`.
- **`mill_client.py`** — `MillClient`: thin async HTTP wrapper for the mill
  component, used ONLY by the onboarding flow (repo registration and the
  one-time port-collision finding) — the periodic caretaker no longer talks
  to the mill.

## Finding Model

A `CaretakerFinding` describes a single issue discovered during a pass:

| Field | Type | Purpose |
| ------- | ------ | --------- |
| `component_id` | `str \| None` | Affected component (or `None` for system-wide) |
| `repo_id` | `str \| None` | Upstream repository identifier (informational) |
| `kind` | `FindingKind` | Category: `UPDATE_APPLIED`, `UPDATE_FAILED`, `HEALTH`, `VOLUME_GROWTH`, `VOLUME_ORPHAN`, `DISK`, `PORT_COLLISION` |
| `title` | `str` | Short human-readable summary |
| `detail` | `str` | Full description |
| `severity` | `Literal["warning", "error"]` | Severity level |

## Configuration

These are ordinary `LifecycleConfig` fields, set in `config/config.json`. All
four are also part of the settings overlay, so they can be changed at runtime
through the dashboard's Settings panel or seeded from the self-contract labels
in `deploy/docker-compose.yml` (prefix `robotsix.deploy.settings.*`) — see
[Configuration](../lifecycle/configuration.md#the-three-layers-in-order).
Self-contract changes take effect on the next server restart.

| Field | Type | Default | Description |
| ---------- | ------ | --------- | ------------- |
| `caretaker_enabled` | `bool` | `False` | Master switch for the caretaker loop |
| `caretaker_interval_hours` | `int` | `24` | Hours between passes (minimum 1) |
| `mill_component_id` | `str` | `"mill"` | Component id of the mill (used by onboarding repo registration) |
| `image_auto_prune` | `bool` | `False` | Whether to prune dangling images after successful updates |
| `disk_warn_pct` | `float` | `10.0` | Percent free disk space that triggers a `DISK` finding |

Additionally, per-component `caretaker_auto_update: bool` (default `True`) in
`ComponentConfig` lets individual services opt out of automatic image updates.

## API

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/caretaker/status` | Yes | Returns `{enabled, last_run_at, last_report}` |

## Reporting

The caretaker never files tickets (operator decision, 2026-09-01: it "just
updates the containers"). Every finding is logged at WARNING level and
appended to a local JSONL file (`caretaker_findings.jsonl`, capped at the
most recent 200 entries), where the operator, the fleet monitor, and the
chat agent can read them.
