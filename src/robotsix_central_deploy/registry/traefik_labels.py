"""Derive Traefik routing labels for a managed component.

central-deploy is the fleet's **control plane**: it does not carry component
traffic itself. Instead it stamps the labels below onto each managed container,
and Traefik — watching the Docker API — picks them up and routes
``<id>.<base_domain>`` to the container with no reload and no per-service
configuration anywhere.

Every label is derived from data the registry already holds (``ComponentConfig.id``
and the primary port), so this module holds **no service-specific knowledge** —
adding a component still requires no change here, in Traefik, in DNS, or in any
config file.

Three routers are emitted per component, distinguished by priority so the most
specific rule wins:

===========  ========  =========================  =================================
Router       Priority  Matches                    Authenticated by
===========  ========  =========================  =================================
``-health``  30        ``GET /health``            nothing — the health contract
                                                  requires an auth-exempt probe
``-machine`` 20        an ``Authorization: Basic``  Traefik's ``basicauth``
                       header                     middleware
(base)       10        everything else            tinyauth SSO via ``forwardAuth``
===========  ========  =========================  =================================

TLS is **not** configured here. The ``websecure`` entrypoint carries the
wildcard certificate for the whole base domain (see ``deploy/traefik/traefik.yml``),
so routers inherit it and no per-component certificate is ever requested.
"""

from __future__ import annotations

from .models import ComponentConfig

#: Traefik middleware protecting browser traffic — tinyauth forward-auth.
BROWSER_MIDDLEWARE: str = "tinyauth@file"

#: Traefik middleware protecting machine traffic — HTTP Basic against the
#: fleet credential.
MACHINE_MIDDLEWARE: str = "fleet-basicauth@file"

#: Entrypoint every component router binds to (TLS, port 443).
ENTRYPOINT: str = "websecure"

_HEALTH_PRIORITY = 30
_MACHINE_PRIORITY = 20
_BROWSER_PRIORITY = 10


def traefik_labels(
    config: ComponentConfig,
    base_domain: str,
    network: str,
) -> dict[str, str]:
    """Return the Traefik labels routing *config* at ``<id>.<base_domain>``.

    Returns an empty dict when the component must not be routed — no
    ``base_domain`` configured, no exposed port, or ``routable`` false (the
    sibling case: a database published at ``<component>-db.<base_domain>``
    would be internet-facing behind nothing but the SSO gate). All three are
    normal states, not errors: the container is simply created without Traefik
    labels and Traefik ignores it.

    Args:
        config: The component to route. Only ``id`` and the first entry of
            ``ports`` are read.
        base_domain: Fleet base domain, e.g. ``deploy.robotsix.net``. The
            component is served at ``<config.id>.<base_domain>``.
        network: Docker network Traefik should reach the container on. Required
            because managed containers may sit on more than one network, and
            Traefik must be told which address to dial.

    Returns:
        A ``{label: value}`` mapping ready to pass to Docker's container-create
        API, or ``{}`` if the component is not routable.
    """
    if not base_domain or not config.ports or not config.routable:
        return {}

    name = config.id
    host_rule = f"Host(`{name}.{base_domain}`)"
    port = str(config.ports[0].container)

    labels = {
        "traefik.enable": "true",
        # Managed containers can join several networks; name the one Traefik
        # shares with them or it may dial an address it cannot reach.
        "traefik.docker.network": network,
        f"traefik.http.services.{name}.loadbalancer.server.port": port,
    }

    for router, priority, rule, middleware in (
        (f"{name}-health", _HEALTH_PRIORITY, f"{host_rule} && Path(`/health`)", None),
        (
            f"{name}-machine",
            _MACHINE_PRIORITY,
            f"{host_rule} && HeaderRegexp(`Authorization`, `^Basic `)",
            MACHINE_MIDDLEWARE,
        ),
        (name, _BROWSER_PRIORITY, host_rule, BROWSER_MIDDLEWARE),
    ):
        prefix = f"traefik.http.routers.{router}"
        labels[f"{prefix}.rule"] = rule
        labels[f"{prefix}.priority"] = str(priority)
        labels[f"{prefix}.entrypoints"] = ENTRYPOINT
        # All three routers front the same upstream; without this Traefik would
        # look for a service named after each router and 404 two of them.
        labels[f"{prefix}.service"] = name
        if middleware is not None:
            labels[f"{prefix}.middlewares"] = middleware

    return labels
