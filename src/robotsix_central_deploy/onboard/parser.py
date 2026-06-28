from __future__ import annotations

import shlex

import re
from typing import Any, Optional

import yaml

from robotsix_central_deploy.onboard.models import ConfigParseError, DerivedSpec, ParseError, SiblingDerivedSpec
from robotsix_central_deploy.registry.models import HealthCheck, PortMapping, VolumeMount

# Regex for Go-style duration strings: optional h, m, s, ms components.
_GO_DURATION_RE = re.compile(
    r"(?:(\d+)h)?" r"(?:(\d+)m(?!s))?" r"(?:(\d+)s)?" r"(?:(\d+)ms)?"
)

HEADER = b"# central-deploy-contract-version: 1"
CLAUDE_MOUNT_LABEL = "robotsix.deploy.claude-mount"
STATEFUL_LABEL = "robotsix.deploy.stateful"
PRIMARY_LABEL = "robotsix.deploy.primary"

# Service-key validation pattern: must match ^[a-z0-9][a-z0-9-]*$
_SERVICE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


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


def _parse_one_service(
    svc: dict,
    key: str,
    *,
    component_name: str,
    prefix: str = "",
) -> tuple[dict, list[str]]:
    """Parse a single service dict (primary or sibling) into a result dict + violations.

    The result dict has keys: image, env, ports, volume_mounts, health_check,
    claude_mount, container_name.
    """
    violations: list[str] = []

    # Build key
    if "build" in svc:
        violations.append(f"{prefix}build: is not permitted — only pre-built images are supported")

    # Image
    image = svc.get("image")
    if not image or not isinstance(image, str) or not image.strip():
        violations.append(f"{prefix}image: is required and must be a non-empty string")
        image = ""
    else:
        image = image.strip()

    # Environment
    env, env_violations = _parse_env(svc.get("environment"))
    violations.extend(f"{prefix}{v}" for v in env_violations)

    # Ports
    ports, port_violations = _parse_ports(svc.get("ports"))
    violations.extend(f"{prefix}{v}" for v in port_violations)

    # Service volumes
    volume_mounts, vol_violations = _parse_volumes(svc.get("volumes"))
    violations.extend(f"{prefix}{v}" for v in vol_violations)

    # Healthcheck
    health_check, hc_violations = _parse_healthcheck(svc.get("healthcheck"))
    violations.extend(f"{prefix}{v}" for v in hc_violations)

    # Labels — claude-mount
    claude_mount = False
    labels = svc.get("labels")
    if isinstance(labels, dict):
        val = labels.get(CLAUDE_MOUNT_LABEL)
        if isinstance(val, str) and val.strip().lower() == "true":
            claude_mount = True

    # container_name override
    container_name = svc.get("container_name", "")
    if container_name is not None and not isinstance(container_name, str):
        violations.append(
            f"{prefix}container_name: must be a string, got {type(container_name).__name__}"
        )
        container_name = ""
    # For siblings, derive container_name if absent
    if not container_name and prefix:
        container_name = f"{component_name}-{key}"

    _raw_cmd = svc.get("command")
    if isinstance(_raw_cmd, str):
        command = shlex.split(_raw_cmd)
    elif isinstance(_raw_cmd, list):
        command = [str(x) for x in _raw_cmd]
    else:
        command = None

    return {
        "image": image,
        "env": env,
        "ports": ports,
        "volume_mounts": volume_mounts,
        "health_check": health_check,
        "claude_mount": claude_mount,
        "container_name": container_name,
        "command": command,
    }, violations


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

    # 3. N >= 1 services
    services = doc.get("services")
    if not isinstance(services, dict) or len(services) == 0:
        violations.append("services: must be a mapping with at least one entry")
        raise ParseError(violations)

    # Validate service keys
    for svc_key in services:
        if not _SERVICE_KEY_RE.fullmatch(svc_key):
            violations.append(
                f"service key {svc_key!r} must match ^[a-z0-9][a-z0-9-]*$"
            )

    # Identify primary service
    if len(services) == 1:
        primary_key = next(iter(services))
    else:
        primary_keys = [
            k for k, svc_dict in services.items()
            if isinstance(svc_dict, dict)
            and isinstance(svc_dict.get("labels"), dict)
            and str(svc_dict["labels"].get(PRIMARY_LABEL, "")).strip().lower() == "true"
        ]
        if len(primary_keys) == 0:
            violations.append(
                f"multi-service compose ({len(services)} services) must designate exactly one "
                f"primary via label '{PRIMARY_LABEL}: \"true\"'; none found"
            )
            raise ParseError(violations)
        if len(primary_keys) > 1:
            violations.append(
                f"exactly one primary allowed; found {len(primary_keys)}: "
                + ", ".join(primary_keys)
            )
            raise ParseError(violations)
        primary_key = primary_keys[0]

    # 4-11. Parse primary service
    primary_svc = services[primary_key]
    if not isinstance(primary_svc, dict):
        violations.append(f"service {primary_key!r} must be a mapping")
        raise ParseError(violations)

    primary_parsed, primary_violations = _parse_one_service(
        primary_svc, primary_key, component_name=name, prefix=""
    )
    violations.extend(primary_violations)

    # Parse sibling services
    siblings_parsed: list[SiblingDerivedSpec] = []
    for sib_key, sib_svc in services.items():
        if sib_key == primary_key:
            continue
        if not isinstance(sib_svc, dict):
            violations.append(f"service {sib_key!r} must be a mapping")
            continue
        sib_prefix = f"[service {sib_key!r}] "
        sib_parsed, sib_violations = _parse_one_service(
            sib_svc, sib_key, component_name=name, prefix=sib_prefix
        )
        violations.extend(sib_violations)
        siblings_parsed.append(
            SiblingDerivedSpec(
                service_key=sib_key,
                container_name=sib_parsed["container_name"],
                image=sib_parsed["image"],
                ports=sib_parsed["ports"],
                volume_mounts=sib_parsed["volume_mounts"],
                env=sib_parsed["env"],
                claude_mount=sib_parsed["claude_mount"],
                health_check=sib_parsed["health_check"],
                command=sib_parsed["command"],
            )
        )

    # 9. Verify named volumes exist in top-level volumes (primary + siblings)
    top_volumes = doc.get("volumes")
    if not isinstance(top_volumes, dict):
        top_volumes = {}
    for vm in primary_parsed["volume_mounts"]:
        if vm.host not in top_volumes:
            violations.append(
                f"volume {vm.host!r} referenced in service {primary_key!r} "
                f"but not declared in top-level volumes:"
            )
    for sib in siblings_parsed:
        for vm in sib.volume_mounts:
            if vm.host not in top_volumes:
                violations.append(
                    f"[service {sib.service_key!r}] volume {vm.host!r} "
                    f"referenced in service but not declared in top-level volumes:"
                )

    # 12. Top-level volume labels — stateful, and driver validation
    stateful_volumes: list[str] = []
    for vname, vdef in top_volumes.items():
        if isinstance(vdef, dict):
            # Validate driver: must be absent or "local"
            driver = vdef.get("driver", "local")
            if driver != "local":
                violations.append(
                    f"volume {vname!r}: driver must be 'local', got {driver!r}"
                )
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
        image=primary_parsed["image"],
        ports=primary_parsed["ports"],
        volume_mounts=primary_parsed["volume_mounts"],
        stateful_volumes=stateful_volumes,
        env=primary_parsed["env"],
        claude_mount=primary_parsed["claude_mount"],
        health_check=primary_parsed["health_check"],
        command=primary_parsed["command"],
        container_name=primary_parsed["container_name"],
        siblings=siblings_parsed,
    )


def parse_config_yaml(config_bytes: bytes) -> dict:
    """Parse config/config.yaml from raw bytes; return parsed mapping.

    Raises:
        ConfigParseError: if the YAML is malformed or not a top-level mapping.
    """
    try:
        doc = yaml.safe_load(config_bytes)
    except yaml.YAMLError as exc:
        raise ConfigParseError(f"config/config.yaml parse error: {exc}") from exc
    if not isinstance(doc, dict):
        raise ConfigParseError("config/config.yaml must be a top-level YAML mapping")
    return doc
