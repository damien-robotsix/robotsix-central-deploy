"""Shared constants for the component registry."""

from __future__ import annotations

#: User-defined bridge network that central-deploy, the Traefik edge, and every
#: managed container join, so Traefik can reach components by container name
#: without any host port being published.
PROXY_NETWORK: str = "central-deploy-proxy"

#: Component slugs that MUST NOT be onboarded, because each component is
#: published at ``<id>.<base-domain>`` and these would shadow the fleet's own
#: edge hostnames.
#:
#: This list used to be much longer: when central-deploy proxied component
#: traffic through its own router table, every one of its API paths (``/ui``,
#: ``/services``, ``/health``, …) was a potential collision. Traefik routes by
#: Host, and central-deploy answers only on the bare base domain, so path
#: collisions are structurally impossible now and only hostnames remain.
RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "traefik",  # the edge's own dashboard host
        "tinyauth",  # the SSO gate — shadowing it would break every login
        "auth",  # conventional alias for the SSO gate
        "deploy",  # central-deploy itself, on the bare base domain
    }
)
