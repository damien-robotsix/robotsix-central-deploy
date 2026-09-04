"""Render Traefik file-provider fragments for remote-host components.

A component deployed on a remote Docker host (``ComponentConfig.host``
non-empty) cannot be discovered through container labels — the local
Traefik edge watches only the local Docker API. Instead, central-deploy
writes one YAML fragment per remote component into a directory Traefik
watches via its file provider (``providers.file.directory``), and the
edge dials the port the component publishes on the remote host's tunnel
address.

The fragment mirrors :func:`..traefik_labels.traefik_labels` exactly —
same three routers (health / bearer / browser), same priorities, same
middlewares — so a component behaves identically whether it runs locally
or remotely.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import ComponentConfig
from .traefik_labels import (
    _BEARER_PRIORITY,
    _BROWSER_PRIORITY,
    _HEALTH_PRIORITY,
    BEARER_MIDDLEWARE,
    BROWSER_MIDDLEWARE,
    ENTRYPOINT,
)

#: Filename prefix for fragments owned by this module — removal on
#: component teardown only ever touches files matching this prefix.
_FRAGMENT_PREFIX = "remote-"


def fragment_path(dynamic_dir: str, component_id: str) -> Path:
    """The fragment file for *component_id* under *dynamic_dir*."""
    return Path(dynamic_dir) / f"{_FRAGMENT_PREFIX}{component_id}.yml"


def render_remote_dynamic_config(
    config: ComponentConfig,
    base_domain: str,
    reach_host: str,
) -> dict[str, Any]:
    """Return the Traefik dynamic-config mapping routing *config*.

    Mirrors the label-based routing (see module docstring). Returns an
    empty dict when the component must not be routed — no ``base_domain``,
    no exposed port, or ``routable`` false — the same normal states as
    :func:`..traefik_labels.traefik_labels`.

    Args:
        config: The remote component. Reads ``id`` and the first ``ports``
            entry (whose ``host`` side is the port published on the remote
            host's reach address).
        base_domain: Fleet base domain, e.g. ``deploy.robotsix.net``.
        reach_host: Address the edge dials — the remote host's tunnel IP.
    """
    if not base_domain or not config.ports or not config.routable or not reach_host:
        return {}

    name = config.id
    host_rule = f"Host(`{name}.{base_domain}`)"
    port = config.ports[0].host

    routers: dict[str, Any] = {}
    for router, priority, rule, middleware in (
        (f"{name}-health", _HEALTH_PRIORITY, f"{host_rule} && Path(`/health`)", None),
        (
            f"{name}-bearer",
            _BEARER_PRIORITY,
            f"{host_rule} && HeadersRegexp(`Authorization`, `^Bearer .+`)",
            BEARER_MIDDLEWARE,
        ),
        (name, _BROWSER_PRIORITY, host_rule, BROWSER_MIDDLEWARE),
    ):
        entry: dict[str, Any] = {
            "rule": rule,
            "priority": priority,
            "entryPoints": [ENTRYPOINT],
            "service": name,
        }
        if middleware is not None:
            entry["middlewares"] = [middleware]
        routers[router] = entry

    return {
        "http": {
            "routers": routers,
            "services": {
                name: {
                    "loadBalancer": {
                        "servers": [{"url": f"http://{reach_host}:{port}"}]
                    }
                }
            },
        }
    }


def write_fragment(
    dynamic_dir: str,
    config: ComponentConfig,
    base_domain: str,
    reach_host: str,
) -> Path | None:
    """Write (or refresh) the fragment for *config*; return its path.

    Returns ``None`` — and removes any stale fragment — when the component
    is not routable, so a config change from routable to non-routable
    converges instead of leaving the old route live.
    """
    rendered = render_remote_dynamic_config(config, base_domain, reach_host)
    path = fragment_path(dynamic_dir, config.id)
    if not rendered:
        path.unlink(missing_ok=True)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(rendered, sort_keys=True), encoding="utf-8")
    # Atomic replace: Traefik watches the directory and must never read a
    # half-written fragment.
    tmp.replace(path)
    return path


def remove_fragment(dynamic_dir: str, component_id: str) -> None:
    """Remove the fragment for *component_id*, if present."""
    fragment_path(dynamic_dir, component_id).unlink(missing_ok=True)
