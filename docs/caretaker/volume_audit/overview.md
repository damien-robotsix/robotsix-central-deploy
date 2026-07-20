# Volume Audit

The volume audit subsystem (`src/robotsix_central_deploy/caretaker/volume_audit/`)
tracks Docker named-volume growth over time, raising findings when volumes
exceed configurable thresholds. Results are surfaced via `GET /volumes/audit`
and can optionally file tickets on the robotsix board.

## Architecture

```
VolumeAuditScheduler (scheduler.py)
  │
  ├─ 1. Collect (component_id, volume_name) pairs from ComponentConfigStore
  ├─ 2. Measure each volume via backend.measure_volume_bytes(vol_name)
  ├─ 3. Load previous snapshot from disk, call compute_growth_records()
  ├─ 4. For each threshold breach → report_finding()
  │      ├─ logs WARNING
  │      ├─ appends to findings JSON file (capped at 100 entries)
  │      └─ BoardClient.create_ticket()  (if board integration configured)
  └─ 5. Save new snapshot to disk
```

- **`models.py`** — Pydantic data types: `VolumeSizeSnapshot`, `VolumeGrowthRecord`,
  `AuditFinding`, `VolumeAuditResponse`.
- **`growth.py`** — Pure function `compute_growth_records(current, previous, …)` that
  compares two snapshot dicts and returns `(records, findings)`.
- **`scheduler.py`** — `VolumeAuditScheduler`: long-running background orchestrator
  that runs the scan loop, persists snapshots, and exposes the read path.
- **`reporter.py`** — `report_finding()`: output seam that logs, persists, and
  optionally files board tickets.

## Threshold Model

A `VolumeGrowthRecord` is flagged as an `AuditFinding` only when **both** guards
are breached:

1. `delta_bytes > min_delta_bytes` — absolute-size guard, prevents false
   positives on tiny volumes.
2. `growth_pct > growth_threshold_pct` — percent-growth guard, prevents
   flagging large absolute deltas that are proportionally small.

Both must be true. Neither alone is sufficient.

## Configuration

All settings are loaded via environment variables (prefix `ROBOTSIX_LIFECYCLE_`).

| Variable | Type | Default | Description |
| ---------- | ------ | --------- | ------------- |
| `VOLUME_AUDIT_ENABLED` | `bool` | `False` | Master switch for the background loop |
| `VOLUME_AUDIT_INTERVAL_SECONDS` | `int` | `3600` | Seconds between scan passes |
| `VOLUME_AUDIT_SNAPSHOT_PATH` | `str` | `data/volume_audit_snapshots.json` | Snapshot persistence path |
| `VOLUME_AUDIT_FINDINGS_PATH` | `str` | `data/volume_audit_findings.json` | Findings persistence path |
| `VOLUME_AUDIT_GROWTH_THRESHOLD_PCT` | `float` | `10.0` | Percent-growth guard threshold |
| `VOLUME_AUDIT_MIN_DELTA_BYTES` | `int` | `10_485_760` (10 MiB) | Minimum absolute byte delta to flag |

## API

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/volumes/audit` | Yes | Returns `VolumeAuditResponse`: enabled flag, last scan time, per-volume growth records, and recent findings |

## Reporting

Each finding is always **logged at WARNING** level and appended to the findings
JSON file (capped at the most recent 100 entries). When the board integration
is configured (`BOARD_API_URL`, `BOARD_API_TOKEN`, `BOARD_REPO_ID`), each
finding also files a task ticket on the robotsix board with a structured
markdown description including volume name, component, size, delta, and growth
percentage.
