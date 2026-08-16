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

===============  ========  ====================================  ======================================
Router            Priority  Matches                              Authenticated by
===============  ========  ====================================  ======================================
``-health``       30        ``GET /health``                      nothing — the health contract requires
                                                                 an auth-exempt probe
``-bearer``       20        ``Host(...) && Authorization: Bearer``  ``mobile-token`` ForwardAuth — validates
                                                                 a mobile bearer token issued by
                                                                 ``GET /auth/token``
(base)            10        everything else                      tinyauth SSO via ``forwardAuth``
===============  ========  ====================================  ======================================

Requests carrying an ``Authorization: Bearer <token>`` header match the
higher-priority ``-bearer`` router and never reach tinyauth — the
``mobile-token`` ForwardAuth validates the token directly.  All other
requests (browser SSO sessions) fall through to the tinyauth gate at
priority 10.

TLS is **not** configured here. The ``websecure`` entrypoint carries the
wildcard certificate for the whole base domain (see ``deploy/traefik/traefik.yml``),
so routers inherit it and no per-component certificate is ever requested.
"""

from __future__ import annotations

from .models import ComponentConfig

#: Traefik middleware protecting every externally routed request — tinyauth
#: forward-auth. There is no second, weaker gate; see the module docstring.
BROWSER_MIDDLEWARE: str = "tinyauth@file"

#: Traefik middleware validating mobile bearer tokens — forwards to
#: central-deploy's ``GET /auth/validate``.  Applied on a higher-priority
#: router so bearer-token requests never reach the tinyauth gate.
BEARER_MIDDLEWARE: str = "mobile-token@file"

#: Entrypoint every component router binds to (TLS, port 443).
ENTRYPOINT: str = "websecure"

_HEALTH_PRIORITY = 30
_BEARER_PRIORITY = 20
_BROWSER_PRIORITY = 10


def is_routable(config: ComponentConfig, base_domain: str) -> bool:
    """Whether the edge will carry traffic for *config*.

    The single predicate behind both the labels stamped on the container and
    the public URL reported for it. Kept in one place because the two must
    agree: a component advertised at a URL the edge has no route for answers
    404 while every other signal — container health, deploy status — says it
    is fine.

    False for all three of: no ``base_domain`` configured, no exposed port, or
    ``routable`` unset. All are normal states, not errors.
    """
    return bool(base_domain and config.ports and config.routable)


def public_url(config: ComponentConfig, base_domain: str) -> str | None:
    """Return the URL the edge serves *config* at, or ``None`` if unrouted.

    ``None`` is the honest answer for a component the edge will not carry:
    handing out ``https://<id>.<domain>`` regardless produces a link that
    404s, which is what made an unrouted component look healthy for as long
    as nobody opened it.
    """
    if not is_routable(config, base_domain):
        return None
    return f"https://{config.id}.{base_domain}"


def traefik_labels(
    config: ComponentConfig,
    base_domain: str,
    network: str,
) -> dict[str, str]:
    """Return the Traefik labels routing *config* at ``<id>.<base_domain>``.

    Returns an empty dict when the component must not be routed — no
    ``base_domain`` configured, no exposed port, or ``routable`` false (the
    sibling case: a database published at ``<component>-db.<base-domain>``
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
    if not is_routable(config, base_domain):
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

    # ``HeaderRegexp`` is the Traefik **v3** spelling. v2 called it
    # ``HeadersRegexp``; v3 rejects that name outright with "unsupported
    # function" and drops the whole router, which silently sends every bearer
    # request to the tinyauth gate instead. The edge has run v3 since the
    # Traefik cutover, so the plural form is always wrong here.
    for router, priority, rule, middleware in (
        (f"{name}-health", _HEALTH_PRIORITY, f"{host_rule} && Path(`/health`)", None),
        (
            f"{name}-bearer",
            _BEARER_PRIORITY,
            f"{host_rule} && HeaderRegexp(`Authorization`, `^Bearer .+`)",
            BEARER_MIDDLEWARE,
        ),
        (name, _BROWSER_PRIORITY, host_rule, BROWSER_MIDDLEWARE),
    ):
        prefix = f"traefik.http.routers.{router}"
        labels[f"{prefix}.rule"] = rule
        labels[f"{prefix}.priority"] = str(priority)
        labels[f"{prefix}.entrypoints"] = ENTRYPOINT
        # All routers front the same upstream; without this Traefik would
        # look for a service named after each router and 404 them.
        labels[f"{prefix}.service"] = name
        if middleware is not None:
            labels[f"{prefix}.middlewares"] = middleware

    return labels
