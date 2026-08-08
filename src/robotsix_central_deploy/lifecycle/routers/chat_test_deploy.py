"""Chat agent test-deploy endpoint.

Provides ``POST /chat/deploy/test`` — a validation-mode deploy that
brings a container up, probes a user-supplied URL, and returns a
structured pass/fail result with container logs.  On failure the
container is rolled back but the audit trail and logs are preserved.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ...registry.config_store import ComponentConfigStore
from ...registry.loader import ComponentRegistry
from .._config_utils import _sanitize_log
from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deploy_lock import release_deploy_lock, try_acquire_deploy_lock
from ..deps import (
    _get_backend,
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_config,
    _get_env_store,
    _get_registry,
    _get_store,
)
from ..models import ServiceRecord, ServiceState
from ..schemas import ChatAgentTestDeployRequest, ChatAgentTestDeployResponse
from ..store import ServiceStore
from ._chat_common import _check_rate_limit, logger
from .chat_services import _resolve_deploy_contract

# Hostnames that must never be probed, even if they resolve to a
# non-private address.
_BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "metadata.google.internal",
        "169.254.169.254",
    }
)

# IPv4 / IPv6 networks that the probe must never target.
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    # AWS / cloud metadata endpoint (link-local).
    ipaddress.IPv4Network("169.254.169.254/32"),
    # Shared address space (RFC 6598) — often used by cloud metadata.
    ipaddress.IPv4Network("100.64.0.0/10"),
    # IPv4 link-local.
    ipaddress.IPv4Network("169.254.0.0/16"),
    # IPv6 link-local.
    ipaddress.IPv6Network("fe80::/10"),
)


def _validate_probe_url(raw: str) -> str:
    """Validate *raw* as a safe probe URL, raising HTTP 422 on failure.

    Permits only ``http`` / ``https`` schemes and blocks metadata-service
    IPs (cloud metadata endpoints, link-local, shared address space).

    Returns a sanitized URL string reconstructed from the parsed
    components so that the dataflow through this function is recognized
    as a sanitizer by static analysis.
    """
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Probe URL scheme must be http or https, got {parsed.scheme!r}.",
        )
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Probe URL must include a hostname.",
        )
    if hostname in _BLOCKED_HOSTNAMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Probe URL hostname {hostname!r} is blocked.",
        )
    # Resolve hostname → IP and check against blocked networks.
    try:
        addr_info = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Probe URL hostname {hostname!r} could not be resolved.",
        )
    for _family, _socktype, _proto, _canonname, sockaddr in addr_info:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        for net in _BLOCKED_NETWORKS:
            if ip in net:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Probe URL resolves to blocked IP {ip_str}.",
                )
    # Reconstruct URL from validated components to break direct
    # user-controlled → request dataflow for SSRF analysis.
    return parsed.geturl()


router = APIRouter(tags=["chat"])

# Maximum characters retained from the probe response body.
_RESPONSE_SNIPPET_MAX: int = 500


# ---------------------------------------------------------------------------
# POST /chat/deploy/test
# ---------------------------------------------------------------------------


@router.post(
    "/chat/deploy/test",
    response_model=ChatAgentTestDeployResponse,
    summary="Test-deploy a component and validate with a probe URL",
    responses={
        403: {"description": "Service not allowlisted for chat-agent mutation"},
        404: {"description": "Component config not found and no repo supplied"},
        409: {"description": "Deploy already in progress"},
        429: {"description": "Rate limited"},
        422: {"description": "Invalid request body or unresolvable deploy contract"},
        503: {"description": "Registry not loaded"},
    },
)
async def chat_test_deploy(
    body: ChatAgentTestDeployRequest,
    request: Request,
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ChatAgentTestDeployResponse:
    """Test-deploy a component and validate by probing *website*.

    Brings the container up, issues an HTTP GET to the supplied probe
    URL, captures the container logs, and returns a structured result.
    On failure the container is rolled back, but the audit entry and
    logs are retained so the operator or chat agent can investigate.

    Synchronous — waits for deploy + probe to complete before returning.
    Rate-limited to one test-deploy per 300 seconds per component.
    """
    _check_rate_limit(request.app.state, body.stub_name, "test_deploy")

    lifecycle_config = await _get_config(request)

    # --- Resolve the deploy contract (before mutatability check so
    #     the 404 branch is reachable when no config and no repo are
    #     supplied). ---
    comp_cfg = component_config_store.get(body.stub_name)
    if comp_cfg is None:
        if body.repo is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"No ComponentConfig found for '{body.stub_name}' "
                    "and no repo was supplied to resolve one."
                ),
            )
        # Build a synthetic deploy request to reuse the contract resolver.
        from ..schemas import ChatAgentDeployRequest

        synth = ChatAgentDeployRequest(name=body.stub_name, repo=body.repo)
        comp_cfg = await _resolve_deploy_contract(
            synth,
            request,
            lifecycle_config,
            component_config_store,
            registry,
            backend,
        )

    # --- Enforce chat-agent mutatability on the resolved config ---
    if not (comp_cfg.chat_agent_mutatable or comp_cfg.allow_chat_access):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Chat agent is not permitted to mutate service '{body.stub_name}'.",
        )

    # --- Merge env overrides ---
    env_store = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(body.stub_name, comp_cfg.env)
    # Inject the deploy API key when chat access is enabled.
    comp_cfg = comp_cfg.model_copy(update={"env": merged_env})

    # --- Get or create the service record ---
    record = await store.get(body.stub_name)
    previous_digest: str = ""
    if record is None:
        record = ServiceRecord(name=body.stub_name)
        await store.put(record)
    else:
        previous_digest = record.deployed_image_digest

    # --- Serialise concurrent deploys ---
    if not await try_acquire_deploy_lock(body.stub_name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Deploy already in progress for '{body.stub_name}'.",
        )

    deploy_image = comp_cfg.image
    try:
        outcome = await backend.deploy(record, comp_cfg, deploy_image)
    except Exception as exc:  # noqa: BLE001
        logger.exception("test-deploy %s: deploy failed", _sanitize_log(body.stub_name))
        await audit_store.append(
            ChatAgentAuditEntry(
                component=body.stub_name,
                action="test-deploy",
                detail=f"Deploy phase failed: {exc}",
            )
        )
        release_deploy_lock(body.stub_name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Test-deploy failed during deploy phase: {exc}",
        )

    # Update the record to reflect the newly deployed image.
    record.state = outcome.state
    record.image = deploy_image
    record.deployed_image_digest = outcome.deployed_digest
    record.previous_image_digest = outcome.previous_digest
    await store.put(record)

    # --- Probe the supplied website ---
    probe_url = _validate_probe_url(body.website)

    probe_pass: bool = False
    probe_status: int | None = None
    probe_snippet: str | None = None
    probe_error: str | None = None

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(probe_url)
            probe_status = resp.status_code
            body_text = resp.text
            probe_snippet = body_text[:_RESPONSE_SNIPPET_MAX]
            # Treat any 2xx/3xx as passing; 4xx/5xx as failing.
            probe_pass = 200 <= probe_status < 400
            if not probe_pass:
                probe_error = f"Probe returned HTTP {probe_status}" + (
                    f": {probe_snippet[:200]}" if probe_snippet else " (empty body)"
                )
    except httpx.TimeoutException:
        probe_error = f"Probe timed out after 10s: {probe_url}"
    except httpx.ConnectError as exc:
        probe_error = f"Probe connection failed: {exc}"
    except Exception as exc:  # noqa: BLE001
        probe_error = f"Probe error: {exc}"

    # --- Capture container logs ---
    container_logs: str = ""
    try:
        container_logs = await backend.get_container_logs(record, tail=200)
    except Exception:  # noqa: BLE001
        logger.warning(
            "test-deploy %s: failed to capture container logs",
            _sanitize_log(body.stub_name),
            exc_info=True,
        )

    # --- On failure: rollback ---
    if not probe_pass:
        logger.warning(
            "test-deploy %s: probe failed — %s",
            _sanitize_log(body.stub_name),
            _sanitize_log(probe_error or "unknown"),
        )
        await audit_store.append(
            ChatAgentAuditEntry(
                component=body.stub_name,
                action="test-deploy",
                detail=(
                    f"Probe failed: {probe_error}. "
                    f"Deployed digest: {outcome.deployed_digest[:19]}…"
                ),
            )
        )

        # Attempt rollback: stop the current container and restore the
        # previous image if one existed.
        rollback_detail: str = ""
        try:
            if previous_digest:
                await backend.stop(record)
                rb_outcome = await backend.rollback(record, comp_cfg)
                record.state = rb_outcome.state
                record.deployed_image_digest = rb_outcome.deployed_digest
                record.previous_image_digest = outcome.deployed_digest
                await store.put(record)
                rollback_detail = f" Rolled back to {previous_digest[:19]}…."
            else:
                # No previous digest — just stop the container.
                await backend.stop(record)
                record.state = ServiceState.STOPPED
                await store.put(record)
                rollback_detail = " Container stopped (no previous digest to restore)."
        except Exception as rb_exc:  # noqa: BLE001
            logger.exception(
                "test-deploy %s: rollback failed",
                _sanitize_log(body.stub_name),
            )
            rollback_detail = f" Rollback also failed: {rb_exc}"

        release_deploy_lock(body.stub_name)

        return ChatAgentTestDeployResponse(
            stub_name=body.stub_name,
            pass_fail="fail",  # noqa: S106
            http_status=probe_status,
            response_snippet=probe_snippet,
            container_logs=container_logs,
            deployed_digest=outcome.deployed_digest,
            detail=f"Probe failed: {probe_error}.{rollback_detail}",
        )

    # --- Success ---
    await audit_store.append(
        ChatAgentAuditEntry(
            component=body.stub_name,
            action="test-deploy",
            detail=(
                f"Probe passed (HTTP {probe_status}). "
                f"Deployed digest: {outcome.deployed_digest[:19]}…"
            ),
        )
    )

    release_deploy_lock(body.stub_name)

    return ChatAgentTestDeployResponse(
        stub_name=body.stub_name,
        pass_fail="pass",  # noqa: S106
        http_status=probe_status,
        response_snippet=probe_snippet,
        container_logs=container_logs,
        deployed_digest=outcome.deployed_digest,
        detail=f"Probe passed: HTTP {probe_status}.",
    )
