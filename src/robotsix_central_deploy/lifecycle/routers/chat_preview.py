"""Chat agent preview deployment endpoints.

Exposes:
- ``POST /chat/preview/deploy`` — deploy a repo+branch into the single preview slot
- ``POST /chat/preview/teardown`` — tear down the preview slot

A single reusable preview slot with no concurrency: a new deploy request
replaces whatever occupies the slot.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import verify_auth
from ..deps import _get_backend, _get_config, _get_registry
from ..config import LifecycleConfig
from ..schemas import (
    ChatAgentPreviewDeployRequest,
    ChatAgentPreviewDeployResponse,
    ChatAgentPreviewTeardownResponse,
)
from ...gateway.proxy import PROXY_NETWORK
from ...registry.models import ComponentConfig, PortMapping

logger = logging.getLogger(__name__)


def _log_safe(value: str) -> str:
    """Replace newlines to prevent log-forgery via user-controlled input."""
    return value.replace("\n", "\\n").replace("\r", "\\r")


router = APIRouter(tags=["chat-preview"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PREVIEW_COMPONENT_ID = "preview"
_PREVIEW_CONTAINER_NAME = "preview"
_PREVIEW_DIR = Path("/tmp/preview-repo")  # noqa: S108  # nosec B108 — intentional fixed path for single preview slot
_PREVIEW_IMAGE_TAG = "preview:latest"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_gateway_base_domain(config: LifecycleConfig) -> str:
    """Return the gateway base domain from config, raising if unset."""
    domain = config.gateway_base_domain
    if not domain:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="gateway_base_domain is not configured — preview URLs cannot be generated",
        )
    return domain


async def _clone_repo(repo_url: str, branch: str, target_dir: Path) -> None:
    """Shallow-clone *repo_url* at *branch* into *target_dir*.

    Removes *target_dir* first when it already exists.
    """
    if target_dir.exists():
        logger.info("Removing existing preview dir %s", target_dir)
        shutil.rmtree(target_dir, ignore_errors=True)

    target_dir.mkdir(parents=True, exist_ok=True)

    proc = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        branch,
        repo_url,
        str(target_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="replace") if stderr else "unknown error"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"git clone failed: {err.strip()}",
        )
    logger.info("Cloned %s@%s → %s", _log_safe(repo_url), _log_safe(branch), target_dir)


def _find_compose_file(base_dir: Path) -> Path:
    """Locate the docker-compose file: ``deploy/docker-compose.yml`` preferred,
    falling back to root ``docker-compose.yml``.
    """
    deploy_compose = base_dir / "deploy" / "docker-compose.yml"
    root_compose = base_dir / "docker-compose.yml"
    if deploy_compose.exists():
        return deploy_compose
    if root_compose.exists():
        return root_compose
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="No docker-compose.yml found in deploy/ or repo root",
    )


def _extract_service_config(compose_path: Path) -> dict[str, Any]:
    """Parse the compose file and return the primary service dict.

    For single-service compose files the sole service is primary; for
    multi-service files the service labelled ``robotsix.deploy.primary:
    "true"`` wins.
    """
    raw = yaml.safe_load(compose_path.read_bytes())
    services: dict[str, Any] = raw.get("services", {})
    if not services:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="docker-compose.yml has no services defined",
        )

    primary: tuple[str, dict[str, Any]] | None = None
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        labels = svc.get("labels", {})
        if isinstance(labels, list):
            # docker-compose list-of-strings label format
            label_map = {}
            for item in labels:
                if "=" in item:
                    k, v = item.split("=", 1)
                    label_map[k] = v
            labels = label_map
        if str(labels.get("robotsix.deploy.primary", "")).lower() == "true":
            primary = (name, svc)
            break

    if primary is None:
        # Single service is implicitly primary
        if len(services) == 1:
            name, svc = next(iter(services.items()))
            primary = (name, svc)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Multi-service compose file must label one service "
                    'robotsix.deploy.primary: "true"'
                ),
            )

    return primary[1]


def _parse_ports(svc: dict[str, Any]) -> list[PortMapping]:
    """Extract PortMapping list from a compose service dict."""
    ports: list[PortMapping] = []
    raw_ports: Any = svc.get("ports", [])
    if not raw_ports or not isinstance(raw_ports, list):
        return ports

    for entry in raw_ports:
        proto = "tcp"
        if isinstance(entry, str):
            rest = entry
            if "/" in rest:
                rest, proto = rest.rsplit("/", 1)
            if ":" in rest:
                host_str, container_str = rest.split(":", 1)
                try:
                    ports.append(
                        PortMapping(
                            host=int(host_str),
                            container=int(container_str),
                            protocol=proto,
                        )
                    )
                except ValueError, TypeError:
                    continue
        elif isinstance(entry, dict):
            try:
                ports.append(
                    PortMapping(
                        host=int(entry.get("published", 0)),
                        container=int(entry.get("target", 0)),
                        protocol=entry.get("protocol", "tcp"),
                    )
                )
            except ValueError, TypeError:
                continue
    return ports


async def _build_image(
    compose_path: Path,
    svc: dict[str, Any],
    project_dir: Path,
) -> str:
    """Build the Docker image for *svc* and return the image reference.

    When the service declares ``build:`` we run ``docker compose build``
    as a subprocess so all build args, context, and dockerfile paths are
    honoured.  The built image is then tagged as ``preview:latest``.

    When the service only declares ``image:``, returns that value as-is.
    """
    image: str | None = svc.get("image")
    build: Any = svc.get("build")

    if build is not None:
        # Build via docker compose to handle all build options
        logger.info("Building preview image via docker compose")
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "compose",
            "-f",
            str(compose_path),
            "-p",
            _PREVIEW_COMPONENT_ID,
            "build",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode(errors="replace") if stderr else "build failed"
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"docker compose build failed: {err.strip()}",
            )

        # Find the built image — compose tags it as preview-<service>:latest
        # We'll tag it as preview:latest for our container creation.
        if not image:
            # Compose auto-names the image: <project>-<service>
            # But we need to find the actual image. Tag the first one we find.
            proc2 = await asyncio.create_subprocess_exec(
                "docker",
                "compose",
                "-f",
                str(compose_path),
                "-p",
                _PREVIEW_COMPONENT_ID,
                "images",
                "-q",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout2, stderr2 = await proc2.communicate()
            if proc2.returncode != 0 or not stdout2.strip():
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="docker compose build succeeded but could not locate built image",
                )
            image_id = stdout2.decode().strip().split("\n")[0]
            # Tag the built image
            tag_proc = await asyncio.create_subprocess_exec(
                "docker",
                "tag",
                image_id,
                _PREVIEW_IMAGE_TAG,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await tag_proc.communicate()
            image = _PREVIEW_IMAGE_TAG

    if not image:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Service has neither image: nor build: defined",
        )

    return image


async def _stop_and_remove_preview_container(backend: Any) -> None:
    """Stop and remove any existing preview container (best-effort)."""
    import docker

    loop = asyncio.get_running_loop()
    try:
        client = backend._client
    except AttributeError:
        return

    try:
        container = await loop.run_in_executor(
            None, client.containers.get, _PREVIEW_CONTAINER_NAME
        )
    except docker.errors.NotFound:
        return
    except Exception:
        return

    logger.info("Stopping existing preview container")
    try:
        await loop.run_in_executor(None, lambda: container.stop(timeout=10))
    except Exception as exc:
        logger.debug("Stop preview container: %s", exc)
    try:
        await loop.run_in_executor(None, lambda: container.remove(force=True))
    except Exception as exc:
        logger.debug("Remove preview container: %s", exc)


async def _create_preview_container(
    backend: Any,
    image_ref: str,
    ports: list[PortMapping],
    env: dict[str, str],
) -> str:
    """Create and start the preview container, returning its short id."""
    import docker

    loop = asyncio.get_running_loop()
    try:
        client = backend._client
    except AttributeError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Preview deployment requires the Docker SDK backend",
        )

    # Remove any existing preview container first
    await _stop_and_remove_preview_container(backend)

    # Build environment list from dict
    env_list = [f"{k}={v}" for k, v in env.items()] if env else None

    # Don't publish host ports — the gateway reaches over the proxy network
    container_ports: dict[str, Any] = {}
    volumes: dict[str, Any] = {}

    def _create() -> Any:
        return client.containers.create(
            image=image_ref,
            name=_PREVIEW_CONTAINER_NAME,
            environment=env_list,
            ports=container_ports,
            volumes=volumes,
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            network=PROXY_NETWORK,
        )

    try:
        container = await loop.run_in_executor(None, _create)
    except docker.errors.APIError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create preview container: {exc}",
        )

    try:
        await loop.run_in_executor(None, container.start)
    except docker.errors.APIError as exc:
        # Best-effort cleanup
        try:
            await loop.run_in_executor(None, lambda: container.remove(force=True))
        except Exception as cleanup_exc:
            logger.debug(
                "Cleanup preview container after start failure: %s", cleanup_exc
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start preview container: {exc}",
        )

    return str(container.short_id)


def _register_preview_component(
    registry: Any,
    ports: list[PortMapping],
    image_ref: str,
) -> None:
    """Register (or replace) the preview component in the in-memory registry."""
    config = ComponentConfig(
        id=_PREVIEW_COMPONENT_ID,
        image=image_ref,
        container_name=_PREVIEW_CONTAINER_NAME,
        ports=ports,
        env={},
        mounts=[],
    )
    # Unregister first if it was previously registered
    try:
        registry.unregister(_PREVIEW_COMPONENT_ID)
    except Exception as exc:
        logger.debug("Unregister preview component: %s", exc)
    registry.register(config)
    logger.info("Registered preview component in registry")


def _unregister_preview_component(registry: Any) -> None:
    """Remove the preview component from the in-memory registry (best-effort)."""
    try:
        registry.unregister(_PREVIEW_COMPONENT_ID)
    except Exception as exc:
        logger.debug("Unregister preview component on teardown: %s", exc)


# ---------------------------------------------------------------------------
# POST /chat/preview/deploy
# ---------------------------------------------------------------------------


@router.post(
    "/chat/preview/deploy",
    response_model=ChatAgentPreviewDeployResponse,
    summary="Deploy a repo+branch into the preview slot",
    responses={
        400: {"description": "Bad request — missing compose file or clone failure"},
        401: {"description": "Unauthorized"},
        503: {"description": "gateway_base_domain not configured"},
    },
)
async def preview_deploy(
    body: ChatAgentPreviewDeployRequest,
    request: Request,
    backend: Any = Depends(_get_backend),
    registry: Any = Depends(_get_registry),
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> ChatAgentPreviewDeployResponse:
    """Deploy *repo_url* at *branch* into the single reusable preview slot.

    Clones the repo, builds the Docker image via compose when needed,
    creates + starts the container on the proxy network, and registers
    the preview component so the gateway can route to it.

    A new deploy replaces whatever currently occupies the slot.
    """
    domain = _resolve_gateway_base_domain(config)
    preview_url = f"https://{_PREVIEW_COMPONENT_ID}.{domain}"

    # 1 — Teardown any existing preview deployment
    await _stop_and_remove_preview_container(backend)
    _unregister_preview_component(registry)
    if _PREVIEW_DIR.exists():
        shutil.rmtree(_PREVIEW_DIR, ignore_errors=True)

    # 2 — Clone the repo
    await _clone_repo(body.repo_url, body.branch, _PREVIEW_DIR)

    # 3 — Locate and parse the compose file
    compose_path = _find_compose_file(_PREVIEW_DIR)
    svc = _extract_service_config(compose_path)

    # 4 — Extract config
    ports = _parse_ports(svc)
    env: dict[str, str] = {}
    raw_env: Any = svc.get("environment", {})
    if isinstance(raw_env, dict):
        env = {str(k): str(v) for k, v in raw_env.items()}
    elif isinstance(raw_env, list):
        for item in raw_env:
            if isinstance(item, str) and "=" in item:
                k, v = item.split("=", 1)
                env[k] = v

    # 5 — Build or resolve the image
    image_ref = await _build_image(compose_path, svc, _PREVIEW_DIR)

    # 6 — Create and start the preview container
    container_id = await _create_preview_container(backend, image_ref, ports, env)

    # 7 — Register the component so the gateway can route to it
    _register_preview_component(registry, ports, image_ref)

    logger.info("Preview deployed: %s → %s", _log_safe(body.repo_url), preview_url)

    return ChatAgentPreviewDeployResponse(
        preview_url=preview_url,
        detail=f"Preview deployed from {body.repo_url}@{body.branch} (container {container_id})",
    )


# ---------------------------------------------------------------------------
# POST /chat/preview/teardown
# ---------------------------------------------------------------------------


@router.post(
    "/chat/preview/teardown",
    response_model=ChatAgentPreviewTeardownResponse,
    summary="Tear down the preview slot",
    responses={
        401: {"description": "Unauthorized"},
    },
)
async def preview_teardown(
    request: Request,
    backend: Any = Depends(_get_backend),
    registry: Any = Depends(_get_registry),
    _auth: None = Depends(verify_auth),
) -> ChatAgentPreviewTeardownResponse:
    """Tear down the preview slot — stop/remove the container and clean up."""

    await _stop_and_remove_preview_container(backend)
    _unregister_preview_component(registry)

    if _PREVIEW_DIR.exists():
        shutil.rmtree(_PREVIEW_DIR, ignore_errors=True)

    logger.info("Preview torn down")

    return ChatAgentPreviewTeardownResponse(
        detail="Preview slot freed.",
    )
