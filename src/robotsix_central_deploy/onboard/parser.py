from __future__ import annotations

import re
from typing import Any, Optional

import yaml

from robotsix_central_deploy.onboard.models import DerivedSpec, ParseError
from robotsix_central_deploy.registry.models import HealthCheck, PortMapping, VolumeMount

# Regex for Go-style duration strings: optional h, m, s, ms components.
_GO_DURATION_RE = re.compile(
    r"(?:(\d+)h)?" r"(?:(\d+)m(?!s))?" r"(?:(\d+)s)?" r"(?:(\d+)ms)?"
)

HEADER = b"# central-deploy-contract-version: 1"
CLAUDE_MOUNT_LABEL = "robotsix.deploy.claude-mount"
STATEFUL_LABEL = "robotsix.deploy.stateful"


def _parse_go_duration(s: str) -> int:
    """Convert a Go duration string (e.g. ``30s``, ``1m30s``) to integer seconds.

    Hours, minutes, and seconds are summed; milliseconds are ignored (floor).
    """
    m = _GO_DURATION_RE.fullmatch(s.strip())
    if not m:
        raise ValueError(f"invalid Go duration: {s!r}")
    h = int(m.group(1) or 0)
    mn = int(m.group(2) or 0)
    sec = int(m.group(3) or 0)
    # ms group(4) ignored — floor to seconds
    return h * 3600 + mn * 60 + sec


def _parse_ports(raw_ports: Any) -> tuple[list[PortMapping], list[str]]:
    """Parse compose ``ports:`` into PortMapping list + violations."""
    ports: list[PortMapping] = []
    violations: list[str] = []

    if raw_ports is None:
        return ports, violations
    if not isinstance(raw_ports, list):
        violations.append(f"ports: must be a list, got {type(raw_ports).__name__}")
        return ports, violations

    for entry in raw_ports:
        if isinstance(entry, str):
            # "HOST:CONTAINER" or "HOST:CONTAINER/PROTO"
            proto = "tcp"
            rest = entry
            if "/" in entry:
                rest, proto = entry.rsplit("/", 1)
            if ":" not in rest:
                violations.append(f"invalid port string: {entry!r}")
                continue
            host_str, container_str = rest.split(":", 1)
            try:
                ports.append(
                    PortMapping(
                        host=int(host_str),
                        container=int(container_str),
                        protocol=proto,
                    )
                )
            except (ValueError, TypeError):
                violations.append(f"invalid port string: {entry!r}")
        elif isinstance(entry, dict):
            # long-form: {target, published, protocol}
            try:
                ports.append(
                    PortMapping(
                        host=int(entry.get("published", 0)),
                        container=int(entry["target"]),
                        protocol=str(entry.get("protocol", "tcp")),
                    )
                )
            except (KeyError, ValueError, TypeError):
                violations.append(f"invalid port mapping: {entry!r}")
        else:
            violations.append(f"unrecognised port entry: {entry!r}")

    return ports, violations


def _parse_volumes(
    raw_volumes: Any,
) -> tuple[list[VolumeMount], list[str]]:
    """Parse service-level ``volumes:`` into VolumeMount list + violations."""
    mounts: list[VolumeMount] = []
    violations: list[str] = []

    if raw_volumes is None:
        return mounts, violations
    if not isinstance(raw_volumes, list):
        violations.append(f"volumes: must be a list, got {type(raw_volumes).__name__}")
        return mounts, violations

    for entry in raw_volumes:
        if not isinstance(entry, str):
            violations.append(f"unrecognised volume entry: {entry!r}")
            continue
        # parse "VOLNAME:CONTAINER_PATH" or "VOLNAME:CONTAINER_PATH:ro"
        parts = entry.split(":")
        if len(parts) < 2 or len(parts) > 3:
            violations.append(f"invalid volume syntax: {entry!r}")
            continue
        source = parts[0]
        container_path = parts[1]
        read_only = len(parts) == 3 and parts[2] == "ro"

        # Check for bind-mount patterns
        if source.startswith("/") or source.startswith("./") or source.startswith("../") or source.startswith("~"):
            violations.append(f"host bind-mount not allowed: {entry!r}")
            continue

        mounts.append(
            VolumeMount(host=source, container=container_path, read_only=read_only)
        )

    return mounts, violations


def _parse_env(raw_env: Any) -> tuple[dict[str, str], list[str]]:
    """Parse compose ``environment:`` into a dict + violations."""
    env: dict[str, str] = {}
    violations: list[str] = []

    if raw_env is None:
        return env, violations

    if isinstance(raw_env, list):
        for item in raw_env:
            if not isinstance(item, str):
                violations.append(f"environment list entry must be a string: {item!r}")
                continue
            if "=" not in item:
                violations.append(f"environment entry missing '=': {item!r}")
                continue
            key, _, val = item.partition("=")
            env[key] = val
    elif isinstance(raw_env, dict):
        for key, val in raw_env.items():
            if val is None:
                env[str(key)] = ""
            else:
                env[str(key)] = str(val)
    else:
        violations.append(
            f"environment: must be a list or dict, got {type(raw_env).__name__}"
        )

    return env, violations


def _parse_healthcheck(raw_hc: Any) -> tuple[Optional[HealthCheck], list[str]]:
    """Parse compose ``healthcheck:`` block into HealthCheck + violations."""
    violations: list[str] = []

    if raw_hc is None:
        return None, violations
    if not isinstance(raw_hc, dict):
        violations.append(f"healthcheck: must be a mapping, got {type(raw_hc).__name__}")
        return None, violations

    test = raw_hc.get("test")
    if test is None:
        violations.append("healthcheck.test is required")
        return None, violations
    if isinstance(test, str) and test.upper() == "NONE":
        # NONE disables healthcheck
        return None, violations
    if not isinstance(test, list) or not all(isinstance(t, str) for t in test):
        violations.append(f"healthcheck.test must be a list of strings, got {test!r}")
        return None, violations

    try:
        interval = _parse_go_duration(str(raw_hc.get("interval", "30s")))
    except (ValueError, TypeError):
        violations.append(f"invalid healthcheck interval: {raw_hc.get('interval')!r}")
        interval = 30

    try:
        timeout = _parse_go_duration(str(raw_hc.get("timeout", "10s")))
    except (ValueError, TypeError):
        violations.append(f"invalid healthcheck timeout: {raw_hc.get('timeout')!r}")
        timeout = 10

    retries = raw_hc.get("retries", 3)
    if not isinstance(retries, int):
        violations.append(f"healthcheck retries must be an integer: {retries!r}")
        retries = 3

    try:
        start_period = _parse_go_duration(str(raw_hc.get("start_period", "0s")))
    except (ValueError, TypeError):
        violations.append(
            f"invalid healthcheck start_period: {raw_hc.get('start_period')!r}"
        )
        start_period = 0

    return (
        HealthCheck(
            test=test,
            interval_seconds=interval,
            timeout_seconds=timeout,
            retries=retries,
            start_period_seconds=start_period,
        ),
        violations,
    )


def parse_compose(compose_bytes: bytes, name: str, git_url: str) -> DerivedSpec:
    """Parse a service repo's docker-compose.yml into a DerivedSpec.

    Accumulates all violations before raising ParseError.
    """
    violations: list[str] = []

    # 1. Check header
    if HEADER not in compose_bytes.splitlines():
        violations.append("missing or incorrect central-deploy-contract-version header")

    # 2. YAML parse
    try:
        doc = yaml.safe_load(compose_bytes)
    except yaml.YAMLError:
        raise ParseError(["compose is not valid YAML"])

    if not isinstance(doc, dict):
        violations.append("compose root must be a mapping")
        raise ParseError(violations)

    # 3. Exactly one service
    services = doc.get("services")
    if not isinstance(services, dict) or len(services) != 1:
        if not isinstance(services, dict):
            violations.append("services: must be a mapping with exactly one entry")
        elif len(services) == 0:
            violations.append("services: must contain exactly one service")
        else:
            violations.append(
                f"exactly one service required, found {len(services)}: "
                + ", ".join(services.keys())
            )

    # If we can't extract a service dict, raise now
    if not isinstance(services, dict) or len(services) != 1:
        raise ParseError(violations)

    service_name = next(iter(services.keys()))
    svc = services[service_name]
    if not isinstance(svc, dict):
        violations.append(f"service {service_name!r} must be a mapping")
        raise ParseError(violations)

    # 4. No build key
    if "build" in svc:
        violations.append("build: is not permitted — only pre-built images are supported")

    # 5. Image required
    image = svc.get("image")
    if not image or not isinstance(image, str) or not image.strip():
        violations.append("image: is required and must be a non-empty string")
        image = ""
    else:
        image = image.strip()

    # 6. Environment
    env, env_violations = _parse_env(svc.get("environment"))
    violations.extend(env_violations)

    # 7. Ports
    ports, port_violations = _parse_ports(svc.get("ports"))
    violations.extend(port_violations)

    # 8. Service volumes
    volume_mounts, vol_violations = _parse_volumes(svc.get("volumes"))
    violations.extend(vol_violations)

    # 9. Verify named volumes exist in top-level volumes:
    top_volumes = doc.get("volumes")
    if not isinstance(top_volumes, dict):
        top_volumes = {}
    for vm in volume_mounts:
        if vm.host not in top_volumes:
            violations.append(
                f"volume {vm.host!r} referenced in service but not declared in top-level volumes:"
            )

    # 10. Healthcheck
    health_check, hc_violations = _parse_healthcheck(svc.get("healthcheck"))
    violations.extend(hc_violations)

    # 11. Labels — claude-mount
    claude_mount = False
    labels = svc.get("labels")
    if isinstance(labels, dict):
        val = labels.get(CLAUDE_MOUNT_LABEL)
        if isinstance(val, str) and val.strip().lower() == "true":
            claude_mount = True

    # 12. Top-level volume labels — stateful
    stateful_volumes: list[str] = []
    for vname, vdef in top_volumes.items():
        if isinstance(vdef, dict):
            vlabels = vdef.get("labels")
            if isinstance(vlabels, dict):
                val = vlabels.get(STATEFUL_LABEL)
                if isinstance(val, str) and val.strip().lower() == "true":
                    stateful_volumes.append(vname)

    # 13. Raise if any violations
    if violations:
        raise ParseError(violations)

    return DerivedSpec(
        name=name,
        git_url=git_url,
        image=image,
        ports=ports,
        volume_mounts=volume_mounts,
        stateful_volumes=stateful_volumes,
        env=env,
        claude_mount=claude_mount,
        health_check=health_check,
    )
