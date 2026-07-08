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
  │      ├─ Probe mill reachability (GET /health)
  │      ├─ phase_update()     — deploy updated images for opted-in components
  │      │   └─ auto-prune dangling images (if image_auto_prune == True)
  │      ├─ phase_health()     — probe all container health states
  │      ├─ phase_volumes()    — volume growth scan + orphan detection + disk check
  │      └─ Report findings → mill (POST /tickets/ingest) or local JSONL fallback
  └─ 4. Loop back to step 1; respect CancelledError for graceful shutdown
```

## Key Modules

- **`models.py`** — Pydantic data types: `FindingKind` (enum of finding
  categories), `CaretakerFinding` (single issue with component, severity,
  title, detail), `CaretakerReport` (aggregate pass result with timing,
  phases run, and mill reporting status).
- **`scheduler.py`** — `CaretakerScheduler`: long-running async orchestrator
  that reads settings each iteration, sleeps the configured interval, and
  invokes all three phases via `run_once()`. Exposes `get_status()` for the
  API endpoint.
- **`phases.py`** — Three independent async phase functions:
  `phase_update()` (deploy updated images, auto-prune), `phase_health()`
  (Docker health probe), `phase_volumes()` (volume audit, orphan detection,
  disk threshold check). Each returns `list[CaretakerFinding]`.
- **`mill_client.py`** — `MillClient`: thin async HTTP wrapper for the mill
  component. Provides `ingest_finding()` (POST findings), `health_check()`
  (reachability probe), and `derive_url_from_registry()` (resolve the mill
  container address from the registry).

## Finding Model

A `CaretakerFinding` describes a single issue discovered during a pass:

| Field | Type | Purpose |
|-------|------|---------|
| `component_id` | `str \| None` | Affected component (or `None` for system-wide) |
| `repo_id` | `str \| None` | Board repo id for ticket filing |
| `kind` | `FindingKind` | Category: `UPDATE_APPLIED`, `UPDATE_FAILED`, `HEALTH`, `VOLUME_GROWTH`, `VOLUME_ORPHAN`, `DISK`, `PORT_COLLISION` |
| `title` | `str` | Short human-readable summary |
| `detail` | `str` | Full description |
| `severity` | `Literal["warning", "error"]` | Severity level |

## Configuration

All settings are managed via the settings API (`PUT /settings`) or
environment variables (prefix `ROBOTSIX_LIFECYCLE_`).

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `caretaker_enabled` | `bool` | `False` | Master switch for the caretaker loop |
| `caretaker_interval_hours` | `int` | `24` | Hours between passes (minimum 1) |
| `mill_component_id` | `str` | `"mill"` | Component id of the mill to report findings to |
| `image_auto_prune` | `bool` | `False` | Whether to prune dangling images after successful updates |
| `disk_warn_pct` | `float` | `10.0` | Percent free disk space that triggers a `DISK` finding |

Additionally, per-component `caretaker_auto_update: bool` (default `True`) in
`ComponentConfig` lets individual services opt out of automatic image updates.

## API

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/caretaker/status` | Yes | Returns `{enabled, last_run_at, mill_reachable, last_report}` |

## Reporting

Findings are sent to the mill component via `POST /tickets/ingest` when
the mill is reachable. If the mill is unreachable or the ingest call fails,
findings are appended to a local JSONL file (`caretaker_findings.jsonl`,
capped at the most recent 200 entries) as a fallback so no finding is lost.
