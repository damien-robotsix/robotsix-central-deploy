"""central-deploy's own settings — the standard component config surface.

Every other router here manages *other* components. This one is the deploy
plane answering for itself: ``GET``/``PUT /config``, ``GET /config/versions``
and ``POST /config/rollback`` over its own ``LifecycleConfig``, exactly as
the robotsix-standards config-ownership standard requires of every component.

Its absence was the gap left by #622. That PR removed the dashboard's
per-service "Configure" modal on the correct principle — a component owns its
config, so the deploy plane must not write it — but central-deploy is itself
a component, and nothing replaced the surface for its own settings. Editing
them meant hand-editing ``/data/config.json`` on the fleet host.

The merge, secret preservation, validation, write and history entry are one
call into ``robotsix_config.history``. They are deliberately not separable:
a UI renders a secret masked, the operator edits a neighbouring field, the
form posts every field back, and a hand-rolled merge writes the mask over the
real credential. That has cost this fleet live credentials before.

**Writes do not take effect until the server restarts.** ``app.state.config``
is the snapshot taken at startup and several settings (the registry-check
interval, the store backend) are consumed once during lifespan. Reloading it
underneath live request handlers would leave stores built from the old paths
disagreeing with the config that describes them, so the file is the only
thing this router changes and the panel says so.
"""

from __future__ import annotations

import logging
from typing import Any

import robotsix_config
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from ..auth import verify_auth
from ..config import LifecycleConfig
from ..schemas import (
    SelfConfigResponse,
    SelfConfigRollbackRequest,
    SelfConfigVersion,
    SelfConfigVersionsResponse,
    SelfConfigWriteResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["config"])

#: The 422 body shape from the fleet's http-error-envelope standard, which is
#: what ``@robotsix/ui``'s ConfigClient parses to place a message on the field
#: the message blames.
_VALIDATION_ERROR_TYPE = "urn:robotsix:error:config-validation"


def _effective_config() -> dict[str, Any]:
    """Return every setting's current value, secrets masked.

    Reads the file rather than ``app.state.config`` so an operator who edited
    it by hand sees what is actually on disk, and so the values the panel
    diffs against are the values the next write will merge into.

    The file holds only the keys someone set; validating it through the model
    fills the rest from field defaults, which is what "effective" means in the
    standard. Pydantic masks ``SecretStr`` with the same ten-asterisk sentinel
    ``robotsix_config`` uses, and ``mask_secrets`` then covers anything the
    raw file carries that the model does not surface.
    """
    loaded = robotsix_config.load_config(LifecycleConfig)
    return robotsix_config.mask_secrets(loaded.model_dump(mode="json"), LifecycleConfig)


def _validation_detail(exc: Exception, fallback: str) -> str:
    """Render *exc* as a field-scoped ``detail`` string for the 422 body.

    Two reasons not to use ``str(exc)``. It opens with the config file's
    absolute path, which is the server's filesystem leaking into an HTTP
    response. And the panel places a message on the offending input only when
    the detail opens with ``"<dotted.key>: "`` (``parseProblemKey`` in
    ``@robotsix/ui``); anything else lands in a banner with no field attached.

    ``robotsix_config`` raises ``InvalidConfigError`` ``from`` the pydantic
    ``ValidationError``, so the structured errors are one ``__cause__`` away.
    """
    errors = getattr(exc.__cause__, "errors", None)
    if not callable(errors):
        return fallback
    try:
        items = list(errors())
    except Exception:  # noqa: BLE001 # pragma: no cover — errors() is pydantic's
        return fallback
    if not items:
        return fallback
    first = items[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "invalid value"))
    remainder = f" (and {len(items) - 1} more)" if len(items) > 1 else ""
    return f"{location}: {message}{remainder}" if location else message + remainder


def _validation_problem(detail: str, instance: str) -> JSONResponse:
    """Return a 422 in the fleet's ``application/problem+json`` envelope."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        media_type="application/problem+json",
        content={
            "type": _VALIDATION_ERROR_TYPE,
            "title": "Config validation failed",
            "detail": detail,
            "instance": instance,
        },
    )


@router.get(
    "/config",
    response_model=SelfConfigResponse,
    summary="Read central-deploy's own settings (secrets masked)",
)
async def get_self_config(_auth: None = Depends(verify_auth)) -> SelfConfigResponse:
    """Return central-deploy's effective config, its JSON Schema and version."""
    return SelfConfigResponse(
        config=_effective_config(),
        config_schema=robotsix_config.config_schema(LifecycleConfig),
        version=robotsix_config.current_version(),
    )


@router.put(
    "/config",
    response_model=SelfConfigWriteResponse,
    summary="Update central-deploy's own settings (partial, versioned)",
    responses={422: {"description": "The update fails LifecycleConfig validation"}},
)
async def put_self_config(
    request: Request,
    _auth: None = Depends(verify_auth),
) -> Any:
    """Apply a partial update to central-deploy's config file.

    Only the keys the caller sends change. A secret submitted as the mask
    sentinel or blank means "unchanged" and keeps its stored value. Validation
    runs before the file is touched, so a rejected update leaves the running
    server's config exactly as it was.
    """
    try:
        body = await request.json()
    except ValueError:
        return _validation_problem("Request body is not valid JSON.", "/config")
    if not isinstance(body, dict):
        return _validation_problem("Request body must be a JSON object.", "/config")

    try:
        _merged, changed, version = robotsix_config.apply_update(LifecycleConfig, body)
    except robotsix_config.InvalidConfigError as exc:
        logger.info("rejected self-config update: %s", exc)
        detail = _validation_detail(
            exc, "The update is not valid for this server's config model."
        )
        return _validation_problem(detail, "/config")

    if changed:
        logger.info(
            "self-config updated to version %s; changed keys: %s",
            version,
            ", ".join(changed),
        )
    return SelfConfigWriteResponse(config=_effective_config(), version=version)


@router.get(
    "/config/versions",
    response_model=SelfConfigVersionsResponse,
    summary="List central-deploy's own config history",
)
async def get_self_config_versions(
    _auth: None = Depends(verify_auth),
) -> SelfConfigVersionsResponse:
    """Return recorded config versions, newest first.

    The history sidecar stores no secret values — a version whose change
    touched a credential names the key and nothing more.
    """
    entries = robotsix_config.read_versions(include_data=False)
    return SelfConfigVersionsResponse(
        versions=[
            SelfConfigVersion(
                version=int(entry["version"]),
                timestamp=str(entry.get("timestamp", "")),
                changed_keys=list(entry.get("changed_keys") or []),
            )
            for entry in reversed(entries)
        ]
    )


@router.post(
    "/config/rollback",
    response_model=SelfConfigWriteResponse,
    summary="Restore an earlier version of central-deploy's own settings",
    responses={
        404: {"description": "No such version in the history"},
        422: {"description": "That version no longer validates against the model"},
    },
)
async def rollback_self_config(
    body: SelfConfigRollbackRequest,
    _auth: None = Depends(verify_auth),
) -> Any:
    """Restore *version*'s values as a new version.

    Nothing is truncated: rolling back from 5 to 2 writes version 6. Secrets
    are **not** restored — the history never stored them — so current
    credentials carry forward unchanged and a rollback meant to undo a
    credential change must be followed by setting that credential explicitly.
    """
    known = {
        int(entry["version"])
        for entry in robotsix_config.read_versions(include_data=False)
    }
    if body.version not in known:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No config version {body.version} in the history; have {sorted(known)}"
            ),
        )

    try:
        _restored, changed, version = robotsix_config.rollback(
            LifecycleConfig, body.version
        )
    except robotsix_config.InvalidConfigError as exc:
        logger.info("rejected self-config rollback to %s: %s", body.version, exc)
        detail = _validation_detail(
            exc,
            f"Version {body.version} no longer validates against the current "
            "config model.",
        )
        return _validation_problem(detail, "/config/rollback")

    if changed:
        logger.info(
            "self-config rolled back to %s as version %s; changed keys: %s",
            body.version,
            version,
            ", ".join(changed),
        )
    return SelfConfigWriteResponse(config=_effective_config(), version=version)
