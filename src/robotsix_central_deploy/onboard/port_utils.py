"""Host-port collision helpers for onboard preflight and contract refresh."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..registry.config_store import ComponentConfigStore


def collect_occupied_host_ports(
    component_config_store: ComponentConfigStore,
    lifecycle_port: int,
    exclude_id: str = "",
) -> set[int]:
    """All host ports claimed by deployed components + central-deploy's own port.

    *exclude_id* omits one component's own ports — needed by contract refresh,
    where that component's mappings are being recomputed and must not count as
    colliding with themselves.
    """
    occupied: set[int] = {lifecycle_port}
    for cfg in component_config_store.all():
        if exclude_id and cfg.id == exclude_id:
            continue
        for pm in cfg.ports:
            occupied.add(pm.host)
        for sib in cfg.siblings:
            for pm in sib.ports:
                occupied.add(pm.host)
    return occupied


def preserve_host_port_assignments(
    new_ports: list[Any],
    old_ports: list[Any],
    occupied: set[int],
) -> None:
    """Carry existing host-port assignments onto freshly parsed *new_ports*, in place.

    A compose file names a host port the repo author happened to pick; the host
    port a component actually runs on is assigned at onboarding, which shifts it
    when it collides with another component. Re-reading the manifest must not
    throw that assignment away — doing so silently pointed one component at a
    port another already owned.

    Each new mapping keyed by ``(container, protocol)`` inherits the host port
    the old config used. Genuinely new mappings keep their requested port when
    free, otherwise they are shifted to a free one, exactly as onboarding does.
    """
    previous = {(pm.container, pm.protocol): pm.host for pm in old_ports}
    for pm in new_ports:
        inherited = previous.get((pm.container, pm.protocol))
        if inherited is not None:
            pm.host = inherited
        elif pm.host in occupied:
            pm.host = find_free_host_port(occupied)
        occupied.add(pm.host)


def find_free_host_port(
    occupied: set[int], start: int = 10000, end: int = 20000
) -> int:
    """Lowest port in [start, end) not in occupied. Raises RuntimeError when exhausted."""
    for port in range(start, end):
        if port not in occupied:
            return port
    raise RuntimeError(f"No free host port in [{start}, {end})")
