"""Host-port collision helpers for onboard preflight."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..registry.config_store import ComponentConfigStore


def collect_occupied_host_ports(
    component_config_store: ComponentConfigStore,
    lifecycle_port: int,
) -> set[int]:
    """All host ports claimed by deployed components + central-deploy's own port."""
    occupied: set[int] = {lifecycle_port}
    for cfg in component_config_store.all():
        for pm in cfg.ports:
            occupied.add(pm.host)
        for sib in cfg.siblings:
            for pm in sib.ports:
                occupied.add(pm.host)
    return occupied


def find_free_host_port(
    occupied: set[int], start: int = 10000, end: int = 20000
) -> int:
    """Lowest port in [start, end) not in occupied. Raises RuntimeError when exhausted."""
    for port in range(start, end):
        if port not in occupied:
            return port
    raise RuntimeError(f"No free host port in [{start}, {end})")
