"""Chat-agent write-surface request / response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from robotsix_central_deploy.lifecycle.models import ActionType, DiskUsageResponse
from robotsix_central_deploy.lifecycle.schemas._base import (
    _DeployResultBase,
    _EnvSecretsFields,
)


class ChatAgentConfigRollbackResponse(BaseModel):
    """Response body for GET /chat/config/{name}.

    Named for the retired rollback endpoint it used to share; the field
    names are part of the chat agent's read contract, so they stay put.
    """

    component: str = Field(description="Component name")
    restored: dict[str, Any] = Field(
        description="Secret-masked snapshot of the restored config"
    )
    detail: str = Field(
        default="", description="Human-readable summary of the rollback result"
    )


class ChatAgentRestartResponse(BaseModel):
    """Response body for POST /chat/services/{name}/restart."""

    name: str = Field(description="Component name")
    action: ActionType = Field(
        default=ActionType.RESTART, description="Always ActionType.RESTART"
    )
    previous_state: str = Field(description="Container state before restart")
    current_state: str = Field(description="Container state after restart")
    detail: str = Field(default="", description="Human-readable summary")


class ChatAgentUpdateResponse(_DeployResultBase):
    """Response body for POST /chat/services/{name}/update."""

    action: str = Field(default="update", description="Always 'update'")
    updated_siblings: list[str] = Field(
        default=[],
        description="Names of sibling components that were also redeployed",
    )


class ChatAgentSelfRestartResponse(BaseModel):
    """Response body for POST /chat/services/central-deploy/restart."""

    name: str = Field(default="central-deploy", description="Component name")
    action: str = Field(default="self-restart", description="Always 'self-restart'")
    container_id: str = Field(description="Container id of the restarted server")
    detail: str = Field(
        default="Container restart triggered; the server will be back shortly.",
        description="Human-readable summary",
    )


class ChatAgentDeployRequest(BaseModel):
    """Request body for POST /chat/deploy.

    Deploys a component by fetching and parsing the repo's
    ``deploy/docker-compose.yml`` (the deploy contract), resolving the
    image, command, ports, volumes, healthchecks, and siblings from the
    contract — matching the dashboard onboarding flow.

    The component does NOT need a pre-existing ``ComponentConfig``;
    one is derived from the deploy contract on first deploy.
    """

    name: str = Field(
        description="Component name; must match ^[a-z0-9][a-z0-9-]*$",
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    repo: str = Field(
        description="Git clone URL of the repository whose deploy/docker-compose.yml defines the component",
    )


class ChatAgentDeployResponse(_DeployResultBase):
    """Response body for POST /chat/deploy."""

    action: str = Field(default="deploy", description="Always 'deploy'")
    deployed_siblings: list[str] = Field(
        default_factory=list,
        description="Sibling service names that were deployed alongside the primary",
    )


class ChatAgentServiceDeployResponse(ChatAgentDeployResponse):
    """Response body for POST /chat/services/{name}/deploy.

    Returns the result of a first-boot deploy of an already-registered
    component: image digests, resulting state, and optional health status.
    """

    health: str = Field(
        default="",
        description="Health status string (empty when not yet checked)",
    )


class ChatAgentRegisterRequest(BaseModel):
    """Request body for POST /chat/services — register a new managed component.

    Registers a component with minimal metadata so it appears in the
    service inventory.  Registration does NOT auto-start or auto-deploy
    the component — those remain separate gated actions.
    """

    name: str = Field(
        description="Component name; must match ^[a-z0-9][a-z0-9-]*$",
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    image: str = Field(
        description="Container image reference (e.g. ghcr.io/org/repo:tag)",
    )
    owner_repo: str = Field(
        min_length=1,
        description="Git clone URL of the repository owning the deploy contract",
    )


class ChatAgentRegisterResponse(BaseModel):
    """Response body for POST /chat/services — register confirmation."""

    name: str = Field(description="Component name")
    action: str = Field(default="register", description="Always 'register'")
    image: str = Field(description="Container image reference")
    owner_repo: str = Field(
        default="", description="Owning repository URL (empty when not supplied)"
    )
    detail: str = Field(default="", description="Human-readable summary")
    existed: bool = Field(
        default=False,
        description="True when the component was already registered (idempotent re-registration)",
    )


class ChatAgentSelfUpdateResponse(BaseModel):
    """Response body for POST /chat/services/central-deploy/update."""

    name: str = Field(default="central-deploy", description="Component name")
    action: str = Field(default="self-update", description="Always 'self-update'")
    updater_container_id: str = Field(
        description="Container id of the one-shot updater that performs the update"
    )
    detail: str = Field(
        default="Self-update triggered; the server will restart with the new image shortly.",
        description="Human-readable summary",
    )


class ChatAgentAuditEntryResponse(BaseModel):
    """One audit-log entry exposed by GET /chat/audit-log."""

    timestamp: float = Field(description="Unix timestamp of the audit event")
    agent_id: str = Field(
        description="Identifier of the chat agent that performed the action"
    )
    component: str = Field(description="Target component name")
    action: str = Field(
        description="Action performed: 'set', 'delete', 'restart', 'update'"
    )
    key: str | None = Field(
        default=None,
        description="Config key affected; None for non-config actions",
    )
    old_value: Any = Field(
        default=None,
        description="Previous value; None when the key did not exist",
    )
    new_value: Any = Field(
        default=None,
        description="New value written; None for deletes",
    )
    detail: str = Field(default="", description="Human-readable event summary")


class ChatAgentAuditLogResponse(BaseModel):
    """Response body for GET /chat/audit-log."""

    entries: list[ChatAgentAuditEntryResponse] = Field(
        default=[], description="Audit log entries, most recent first"
    )


# ---------------------------------------------------------------------------
# Chat agent preview deployment models
# ---------------------------------------------------------------------------


class ChatAgentPreviewDeployRequest(BaseModel):
    """Request body for POST /chat/preview/deploy."""

    repo_url: str = Field(
        description="Git clone URL of the repository to preview-deploy"
    )
    branch: str = Field(description="Git branch to check out for the preview")


class ChatAgentPreviewDeployResponse(BaseModel):
    """Response body for POST /chat/preview/deploy."""

    preview_url: str = Field(
        description="URL where the preview deployment is accessible"
    )
    detail: str = Field(default="", description="Human-readable status message")


class ChatAgentPreviewTeardownResponse(BaseModel):
    """Response body for POST /chat/preview/teardown."""

    detail: str = Field(
        default="", description="Human-readable teardown status message"
    )


# ---------------------------------------------------------------------------
# Chat agent env (secret provisioning) models
# ---------------------------------------------------------------------------


class ChatAgentEnvUpdate(_EnvSecretsFields):
    """Request body for PUT /chat/env/{name}.

    Secret values are accepted in the ``secrets`` dict and are encrypted
    at rest — they are never logged or echoed in responses.
    """


class ChatAgentEnvResponse(BaseModel):
    """Response body for PUT /chat/env/{name}.

    Secret values are never included — only the key names are returned.
    """

    component: str = Field(description="Component name")
    env_keys: list[str] = Field(
        default=[], description="Plain-text env keys that were upserted"
    )
    secret_keys: list[str] = Field(
        default=[], description="Secret keys that were upserted (values never returned)"
    )
    detail: str = Field(default="", description="Human-readable summary")


# ---------------------------------------------------------------------------
# POST /chat/disk/reclaim
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# POST /chat/deploy/test
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# POST /chat/services/{name}/enable-mutation  +  /disable-mutation
# ---------------------------------------------------------------------------


class ChatAgentMutationEnableRequest(BaseModel):
    """Request body for POST /chat/services/{name}/enable-mutation.

    Grants the chat agent permission to mutate (restart / update / deploy
    / config-write / env-write / test-deploy) a single named stub.
    """

    ttl_seconds: int | None = Field(
        default=None,
        description=(
            "Optional TTL in seconds.  When set, the mutation grant is "
            "automatically disabled after this many seconds (best-effort; "
            "a server restart cancels the timer)."
        ),
    )


class ChatAgentMutationEnableResponse(BaseModel):
    """Response body for POST /chat/services/{name}/enable-mutation."""

    name: str = Field(description="Component name")
    action: str = Field(
        default="enable-mutation", description="Always 'enable-mutation'"
    )
    previous: bool = Field(description="Previous chat_agent_mutatable value")
    current: bool = Field(
        description="Current chat_agent_mutatable value (always True)"
    )
    ttl_seconds: int | None = Field(
        default=None, description="TTL echoed from the request body"
    )
    detail: str = Field(default="", description="Human-readable summary")


class ChatAgentMutationDisableResponse(BaseModel):
    """Response body for POST /chat/services/{name}/disable-mutation."""

    name: str = Field(description="Component name")
    action: str = Field(
        default="disable-mutation", description="Always 'disable-mutation'"
    )
    previous: bool = Field(description="Previous chat_agent_mutatable value")
    current: bool = Field(
        description="Current chat_agent_mutatable value (always False)"
    )
    detail: str = Field(default="", description="Human-readable summary")


class ChatAgentTestDeployRequest(BaseModel):
    """Request body for POST /chat/deploy/test.

    Validates a deployment by bringing the container up, probing the
    supplied *website* URL, and returning a structured pass/fail result
    together with the container logs.  On failure the container is
    rolled back, but the audit entry and logs are retained so the
    operator or chat agent can investigate.
    """

    stub_name: str = Field(
        description="Component/service name; must match ^[a-z0-9][a-z0-9-]*$",
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    website: str = Field(
        description="Probe URL (e.g. http://localhost:8080/health) to validate the deployed container",
    )
    repo: str | None = Field(
        default=None,
        description="Optional Git clone URL of the repository; used to resolve the deploy contract when no persisted ComponentConfig exists",
    )


class ChatAgentTestDeployResponse(BaseModel):
    """Response body for POST /chat/deploy/test."""

    stub_name: str = Field(description="Component/service name")
    pass_fail: Literal["pass", "fail"] = Field(description="'pass' or 'fail'")
    http_status: int | None = Field(
        default=None,
        description="HTTP status code returned by the probe; None when the connection failed",
    )
    response_snippet: str | None = Field(
        default=None,
        description="First 500 characters of the probe response body; None when unavailable",
    )
    container_logs: str = Field(
        default="",
        description="Last 200 lines of container stdout/stderr captured after the probe",
    )
    deployed_digest: str = Field(
        default="",
        description="Digest of the deployed image",
    )
    detail: str = Field(default="", description="Human-readable summary")


class ChatAgentDiskReclaimRequest(BaseModel):
    """Request body for POST /chat/disk/reclaim.

    Selects which safe reclaim targets to prune.  Only ``dangling_images``
    and ``build_cache`` are accepted — tagged images, in-use images, and
    named volumes are never pruned.

    When *force* is ``True``, stopped containers are removed before the
    image prune so that images they reference become eligible for removal.
    """

    model_config = {"populate_by_name": True}

    dangling_images: bool = Field(
        default=False,
        alias="images",
        description="Prune dangling (untagged) Docker images.",
    )
    build_cache: bool = Field(
        default=False,
        description="Prune reclaimable Docker build cache.",
    )
    force: bool = Field(
        default=False,
        description=(
            "Remove stopped containers before pruning images so that "
            "images referenced only by stopped containers can be reclaimed."
        ),
    )


class ChatAgentDiskReclaimResponse(BaseModel):
    """Response body for POST /chat/disk/reclaim."""

    name: str = Field(default="central-deploy")
    action: str = Field(default="disk-reclaim")
    space_reclaimed_bytes: int = Field(
        description="Total bytes freed by the reclaim operation."
    )
    detail: str = Field(default="", description="Human-readable summary")
    disk_snapshot: DiskUsageResponse | None = Field(
        default=None,
        description="Full disk-usage snapshot taken after the reclaim operation.",
    )
    # Image prune detail — surfaced so operators can diagnose zero-reclaim calls.
    images_removed: int = Field(
        default=0,
        description="Number of dangling images successfully removed.",
    )
    images_skipped_protected: int = Field(
        default=0,
        description="Dangling images skipped because they are rollback targets.",
    )
    images_skipped_in_use: int = Field(
        default=0,
        description="Dangling images skipped because a container still references them.",
    )
    images_skipped_intermediate: int = Field(
        default=0,
        description="Dangling images skipped because they are intermediate parent "
        "layers referenced by another (tagged) image — cannot be pruned.",
    )
    images_skipped_error: int = Field(
        default=0,
        description="Dangling images skipped because Docker returned an error.",
    )
    images_error_summary: str = Field(
        default="",
        description="First few distinct prune errors, if any.",
    )
    stopped_containers_removed: int = Field(
        default=0,
        description="When force=True: number of stopped containers removed.",
    )


# ---------------------------------------------------------------------------
# Diagnose endpoint models
# ---------------------------------------------------------------------------
