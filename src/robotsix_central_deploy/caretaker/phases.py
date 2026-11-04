"""Phase functions for the caretaker daily maintenance pass.

Each function is an async callable that returns ``list[CaretakerFinding]``.
They do NOT import the scheduler or mill client — they are pure logic.

The implementations live in per-concern modules; this module re-exports
them so existing ``from .phases import …`` call sites keep working:

- :mod:`.phases_update` — image auto-update and Claude auth phases
- :mod:`.phases_health` — health checks and restart watchdog
- :mod:`.phases_rollback` — auto-rollback
- :mod:`.phases_volumes` — volume retention and disk usage
"""

from __future__ import annotations

from .phases_health import (
    _HEALTH_LOG_TAIL_LINES,
    _crash_loop_finding_detail,
    _crash_loop_reasons,
    _health_finding_detail,
    phase_health,
    phase_restart_watchdog,
)
from .phases_rollback import phase_auto_rollback
from .phases_update import (
    component_auto_update_enabled,
    phase_claude_auth,
    phase_update,
)
from .phases_volumes import _apply_volume_retention, phase_volumes

__all__ = [
    "_HEALTH_LOG_TAIL_LINES",
    "_apply_volume_retention",
    "_crash_loop_finding_detail",
    "_crash_loop_reasons",
    "_health_finding_detail",
    "component_auto_update_enabled",
    "phase_auto_rollback",
    "phase_claude_auth",
    "phase_health",
    "phase_restart_watchdog",
    "phase_update",
    "phase_volumes",
]
