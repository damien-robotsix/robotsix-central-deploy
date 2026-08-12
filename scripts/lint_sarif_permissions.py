#!/usr/bin/env python3
"""Lint that SARIF-producing workflows declare ``security-events: write``.

Reads the ``SARIF_WORKFLOWS`` env var (space-separated list of workflow file
names relative to ``.github/workflows/``) and exits non-zero if any of them
lack the required permission.
"""

import os
import sys
from pathlib import Path

import yaml


def has_security_events_write(permissions) -> bool:
    """Return True if *permissions* grants ``security-events: write``."""
    if permissions is None:
        return False
    if isinstance(permissions, str):
        # e.g.  ``permissions: read-all``
        return permissions in ("write-all",)
    if isinstance(permissions, dict):
        return permissions.get("security-events") == "write"
    return False


def lint_workflow(path: Path) -> bool:
    """Return True if *path* has the required SARIF permission."""
    try:
        doc = yaml.safe_load(path.read_text())
    except Exception as exc:
        print(f"::error file={path}::Failed to parse YAML: {exc}")
        return False

    if not isinstance(doc, dict):
        print(f"::error file={path}::Top-level is not a mapping")
        return False

    ok = True

    # Top-level permissions
    if not has_security_events_write(doc.get("permissions")):
        # Check each job's permissions
        jobs = doc.get("jobs", {})
        if isinstance(jobs, dict):
            for job_name, job in jobs.items():
                if isinstance(job, dict) and has_security_events_write(
                    job.get("permissions")
                ):
                    break
            else:
                print(
                    f"::error file={path}::"
                    f"Missing 'security-events: write' permission "
                    f"(neither at top level nor in any job)"
                )
                ok = False

    return ok


def main() -> int:
    workflows_env = os.environ.get("SARIF_WORKFLOWS", "")
    if not workflows_env.strip():
        print("::notice::SARIF_WORKFLOWS is empty — nothing to check")
        return 0

    workflows_dir = Path(".github/workflows")
    all_ok = True

    for name in workflows_env.split():
        wf_path = workflows_dir / name.strip()
        if not wf_path.is_file():
            print(f"::error file={wf_path}::Workflow file not found")
            all_ok = False
            continue
        if not lint_workflow(wf_path):
            all_ok = False

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
