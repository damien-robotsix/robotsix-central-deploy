"""Pydantic request/response schemas for lifecycle endpoints.

Extracted from the monolithic server.py so that each router module can
import the models it needs without importing the FastAPI app.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from robotsix_central_deploy.lifecycle.models import (
    ActionType,
    DeployJobPhase,
    DiskUsageResponse,
    OnboardJobPhase,
    VolumeEntryType,
)
from robotsix_central_deploy.onboard.models import DerivedSpec  # noqa: TCH001
from robotsix_central_deploy.registry import ConfigAssistSeed  # noqa: TCH001


# ---------------------------------------------------------------------------
# Onboard request / response models
# ---------------------------------------------------------------------------


class PortShift(BaseModel):
    container_port: int = Field(
        description="Container-side port from the service's docker-compose.yml"
    )
    protocol: str = Field(description="Transport protocol: 'tcp' or 'udp'")
    original_host: int = Field(
        description="Host port from the repo's docker-compose.yml"
    )
    assigned_host: int = Field(
        description="Auto-assigned free host port during onboarding"
    )
    collision_component_id: str = Field(
        description="Component whose stored port collided; 'central-deploy' for lifecycle-assigned ports"
    )
    collision_repo_id: str = Field(
        description="Repo ID of the colliding component; empty string when unknown"
    )


class OnboardPreflightRequest(BaseModel):
    git_url: str = Field(description="Git clone URL of the repository to onboard")
    name: str = Field(description="Component name; must match ^[a-z0-9][a-z0-9-]*$")


class OnboardPreflightResponse(BaseModel):
    spec: DerivedSpec = Field(
        description="Derived deployment specification for the component"
    )
    port_shifts: list[PortShift] = Field(
        default=[],
        description="Port remappings needed to avoid collisions; empty when no shifts required",
    )


class OnboardConfirmRequest(BaseModel):
    spec: DerivedSpec = Field(
        description="Final DerivedSpec with user-supplied environment values"
    )
    config_values: dict[str, Any] | None = Field(
        default=None,
        description="Optional config.json key-value overrides",
    )
    register_with_mill: bool = Field(
        default=True,
        description="Whether to register the component with the mill after onboarding",
    )
    port_shifts: list[PortShift] = Field(
        default=[],
        description="Port shift list echoed from preflight; used for collision ticket filing",
    )


class OnboardConfirmAcceptedResponse(BaseModel):
    """Returned by POST /onboard/confirm (202) when the job is queued."""

    job_id: str = Field(description="Unique identifier for the queued onboard job")
    name: str = Field(description="Component name")


class OnboardJobStatusResponse(BaseModel):
    """Returned by GET /onboard/jobs/{job_id}."""

    job_id: str = Field(description="Unique identifier of the onboard job")
    component: str = Field(
        description="Component ID returned by the mill after registration"
    )
    phase: OnboardJobPhase = Field(
        description="Current phase of the onboarding workflow"
    )
    error: str | None = Field(
        default=None,
        description="Error message when phase is 'failed'; None otherwise",
    )
    name: str | None = Field(default=None, description="Component name")
    image: str | None = Field(
        default=None,
        description="Selected OCI image reference; None before image resolution",
    )
    state: str | None = Field(
        default=None,
        description="Deployment state string; None if not yet deployed",
    )
    warnings: list[str] = Field(
        default=[],
        description="Non-empty when the mill was unreachable during port-shift ticket filing",
    )


# ---------------------------------------------------------------------------
# Deploy job models (async deploy pattern)
# ---------------------------------------------------------------------------


class DeployAcceptedResponse(BaseModel):
    """Returned by POST /services/{name}/deploy (202) when the job is queued."""

    job_id: str = Field(description="Unique identifier for the queued deploy job")
    name: str = Field(description="Component name")


class DeployJobStatusResponse(BaseModel):
    """Returned by GET /services/deploy-jobs/{job_id}."""

    job_id: str = Field(description="Unique identifier of the deploy job")
    component: str = Field(description="Component ID")
    phase: DeployJobPhase = Field(description="Current phase of the deploy workflow")
    error: str | None = Field(
        default=None,
        description="Error message when phase is 'failed'; None otherwise",
    )
    name: str | None = Field(default=None, description="Component name")
    image: str | None = Field(
        default=None,
        description="Selected OCI image reference; None before image resolution",
    )
    state: str | None = Field(
        default=None,
        description="Deployment state string; None if not yet deployed",
    )
    warnings: list[str] = Field(
        default=[],
        description="Warnings encountered during deploy (e.g. pre-pull failures)",
    )


# ---------------------------------------------------------------------------
# Env endpoint models
# ---------------------------------------------------------------------------


class EnvResponse(BaseModel):
    env: dict[str, str] = Field(
        description="Plain-text environment variables (key → value)"
    )
    secrets: dict[str, str] = Field(
        description="Secret environment variables; values are always masked as '***'"
    )
    env_scopes: dict[str, str] = Field(
        default={},
        description="Visibility scope per env key ('global', 'component', or repo URL)",
    )
    secret_scopes: dict[str, str] = Field(
        default={},
        description="Visibility scope per secret key ('global', 'component', or repo URL)",
    )
    mem_limit: str = Field(
        default="2g", description="Docker memory limit string (e.g. '2g')"
    )
    allow_chat_access: bool = Field(
        default=False,
        description="Whether chat-agent mutation is permitted for this component",
    )
    claude_mount: bool = Field(
        default=False,
        description="Whether the Claude code mount is enabled for this component",
    )


class EnvSyncResponse(BaseModel):
    """Body of the 200 response from POST /services/{name}/env/sync-keys."""

    added_env: list[str] = Field(
        description="Newly declared plain env keys, seeded with default values"
    )
    added_secrets: list[str] = Field(
        description="Newly declared secret slots; stored with empty values"
    )
    undeclared: list[str] = Field(
        description="Stored keys that the compose contract no longer declares"
    )


class EnvUpdate(BaseModel):
    env: dict[str, str] = Field(
        default={},
        description="Plain-text environment variables to set (key → value)",
    )
    secrets: dict[str, str] = Field(
        default={},
        description="Secret environment variables to set (key → value)",
    )
    env_scopes: dict[str, str] = Field(
        default={},
        description="Visibility scope overrides for env keys",
    )
    secret_scopes: dict[str, str] = Field(
        default={},
        description="Visibility scope overrides for secret keys",
    )
    mem_limit: str | None = Field(
        default=None,
        description="Docker memory limit; None leaves the current value unchanged",
    )
    allow_chat_access: bool | None = Field(
        default=None,
        description="Toggle chat-agent mutability; None leaves the current value unchanged",
    )
    claude_mount: bool | None = Field(
        default=None,
        description="Toggle Claude code mount; None leaves the current value unchanged",
    )


# ---------------------------------------------------------------------------
# Volume browser models
# ---------------------------------------------------------------------------


class VolumeEntry(BaseModel):
    name: str = Field(description="Volume name as reported by Docker")
    type: VolumeEntryType = Field(
        description="Volume category: named-volume, bind-mount, or tmpfs"
    )
    size_bytes: int = Field(description="Disk usage of the volume in bytes")


class VolumeListResponse(BaseModel):
    entries: list[VolumeEntry] = Field(description="Volume entries for the component")


class VolumeFileResponse(BaseModel):
    size_bytes: int = Field(description="Total file size in bytes")
    content: str | None = Field(
        default=None,
        description="File content as UTF-8 text; None when binary or too large",
    )
    binary: bool = Field(
        description="True when the file is detected as binary (not valid UTF-8)"
    )
    truncated: bool = Field(
        description="True when content exceeds the display limit and was cut"
    )


# ---------------------------------------------------------------------------
# Orphan-volume prune models
# ---------------------------------------------------------------------------


class OrphanVolume(BaseModel):
    """A Docker volume owned by no registered component and not in use."""

    name: str = Field(description="Docker volume name")
    size_bytes: int = Field(
        default=0, description="Disk usage in bytes; 0 when unknown"
    )


class OrphanVolumesResponse(BaseModel):
    volumes: list[OrphanVolume] = Field(
        default=[], description="List of orphan Docker volume candidates"
    )
    total_bytes: int = Field(
        default=0, description="Sum of all orphan volume sizes in bytes"
    )


class PruneVolumesRequest(BaseModel):
    names: list[str] | None = Field(
        default=None,
        description="Volume names to prune; None means prune every orphan candidate",
    )


class PruneVolumesResponse(BaseModel):
    removed: list[str] = Field(
        default=[], description="Volumes confirmed gone after the prune"
    )
    skipped: list[str] = Field(
        default=[], description="Requested names that were not eligible orphans"
    )
    failed: list[str] = Field(
        default=[],
        description="Eligible orphans that were still present after the prune attempt",
    )
    space_reclaimed_bytes: int = Field(
        default=0, description="Total bytes freed by successfully removed volumes"
    )


# ---------------------------------------------------------------------------
# Config endpoint models
# ---------------------------------------------------------------------------


class ConfigResponse(BaseModel):
    config_schema: dict[str, Any] = Field(
        serialization_alias="schema",
        description="JSON Schema describing the config.json structure for the component",
    )
    current: dict[str, Any] = Field(
        description="Current config values read from the volume; secrets masked"
    )
    drift: bool = Field(
        default=False,
        description="True when the volume content differs from the last stored hash",
    )
    config_assist_command: str | None = Field(
        default=None,
        description="One-shot container command for config auto-fill; None when unavailable",
    )
    config_assist_seeds: list[ConfigAssistSeed] = Field(
        default=[],
        description="ConfigAssistSeed entries registered for this component",
    )


class ConfigUpdate(BaseModel):
    values: dict[str, Any] = Field(
        description="Key-value pairs to write to the component's config volume"
    )
    force_overwrite: bool = Field(
        default=False,
        description="When True, overwrite even when config drift is detected",
    )


class ComponentSuggestItem(BaseModel):
    """Lightweight component info for the config-form URL suggest feature."""

    id: str = Field(description="Component ID")
    container_name: str = Field(description="Docker container name")
    container_port: int | None = Field(
        default=None,
        description="First exposed container port; None when no ports are defined",
    )


class ComponentSuggestResponse(BaseModel):
    components: list[ComponentSuggestItem] = Field(
        description="Matching component suggestions"
    )


class ConfigDriftConflict(BaseModel):
    """Body of the 409 Conflict response when drift is detected on Save."""

    drift: Literal[True] = Field(
        default=True, description="Always True; signals a drift conflict"
    )
    live_config: dict[str, Any] = Field(
        description="Current config volume content; secrets masked"
    )
    stored_config: dict[str, Any] = Field(
        description="Last stored config snapshot; secrets masked"
    )


class ConfigImportResponse(BaseModel):
    """Body of the 200 response from POST /services/{name}/config/import."""

    current: dict[str, Any] = Field(
        description="Imported config values; secrets masked"
    )
    volume_hash: str = Field(
        description="Canonical hash of the imported content for drift tracking"
    )


class ConfigSchemaRefreshResponse(BaseModel):
    """Body of the 200 response from POST /services/{name}/config/refresh-schema."""

    config_schema: dict[str, Any] = Field(
        serialization_alias="schema",
        description="Refreshed JSON Schema for the component's config.json",
    )


class ContractRefreshResponse(BaseModel):
    """Body of the 200 response from POST /services/{name}/refresh-contract."""

    name: str = Field(description="Component name")
    changed_fields: list[str] = Field(
        default=[],
        description="Top-level keys whose values changed after the refresh",
    )
    previous: dict[str, Any] = Field(
        default={},
        description="Snapshot of the contract before refresh",
    )
    current: dict[str, Any] = Field(
        default={},
        description="Snapshot of the contract after refresh",
    )


class ConfigAssistRequest(BaseModel):
    values: dict[str, Any] = Field(
        description="Current (partial) form values to seed the assist command"
    )
    target_account_index: int | None = Field(
        default=None,
        description="Optional target account index for multi-account configs",
    )
    account_name: str | None = Field(
        default=None,
        description="Optional account name for account-scoped config assist",
    )


class ConfigAssistResponse(BaseModel):
    config: dict[str, Any] = Field(
        description="Auto-filled config dict read back from the volume after the command ran"
    )
    output: str = Field(
        description="Captured stdout and stderr from the one-shot container"
    )


# ---------------------------------------------------------------------------
# Claude auth request / response models
# ---------------------------------------------------------------------------


class ClaudeAuthStatusResponse(BaseModel):
    status: str = Field(
        description="Auth status: 'authenticated', 'not-authenticated', 'expiring', or 'error'"
    )
    detail: str = Field(default="", description="Human-readable status detail")
    refresh_status: str = Field(
        default="",
        description="Token refresh status: 'ok', 'failed', 'never', or empty",
    )
    last_refresh_error: str = Field(
        default="",
        description="Error message from the most recent failed refresh attempt",
    )


class ClaudeAuthLoginResponse(BaseModel):
    login_id: str = Field(description="Opaque login session identifier")
    oauth_url: str = Field(description="OAuth URL the user must visit to authorize")


class ClaudeAuthCompleteRequest(BaseModel):
    login_id: str = Field(
        description="Login session identifier from the initiate-login response"
    )
    auth_code: str = Field(description="Authorization code from the OAuth callback")


class ClaudeAuthCancelRequest(BaseModel):
    login_id: str = Field(description="Login session identifier to cancel")


class ClaudeAuthCompleteResponse(BaseModel):
    status: str = Field(description="'authenticated' on success, 'error' on failure")
    error: str = Field(default="", description="Error message when status is 'error'")


class ClaudeAuthCredentialsRequest(BaseModel):
    credentials_json: str = Field(
        description="Raw credentials JSON blob from the OAuth provider"
    )


class ClaudeAuthCredentialsResponse(BaseModel):
    status: str = Field(description="'authenticated' on success, 'error' on failure")
    error: str = Field(default="", description="Error message when status is 'error'")


# ---------------------------------------------------------------------------
# Chat agent write-surface models
# ---------------------------------------------------------------------------


class ChatAgentConfigUpdate(BaseModel):
    """Request body for PUT /chat/config/{name}.

    Secret keys are accepted with partial-update semantics: omitted or
    sentinel (``"***"``) values keep the stored secret; only an explicitly
    supplied non-empty value overwrites it.
    """

    values: dict[str, Any] = Field(
        description="Key-value pairs to write; sentinel '***' values preserve existing secrets"
    )


class ChatAgentConfigRollbackResponse(BaseModel):
    """Response body for PUT /chat/config/{name} and POST /chat/config/{name}/rollback."""

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


class ChatAgentUpdateResponse(BaseModel):
    """Response body for POST /chat/services/{name}/update."""

    name: str = Field(description="Component name")
    action: str = Field(default="update", description="Always 'update'")
    deployed_digest: str = Field(
        default="",
        description="Digest of the newly deployed image; empty when unchanged",
    )
    previous_digest: str = Field(
        default="", description="Digest of the previously deployed image"
    )
    current_state: str = Field(description="Container state after update")
    detail: str = Field(default="", description="Human-readable summary")
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


class ChatAgentDeployResponse(BaseModel):
    """Response body for POST /chat/deploy."""

    name: str = Field(description="Component name")
    action: str = Field(default="deploy", description="Always 'deploy'")
    deployed_digest: str = Field(
        default="",
        description="Digest of the newly deployed image; empty when unchanged",
    )
    previous_digest: str = Field(
        default="", description="Digest of the previously deployed image"
    )
    current_state: str = Field(description="Container state after deploy")
    detail: str = Field(default="", description="Human-readable summary")
    deployed_siblings: list[str] = Field(
        default_factory=list,
        description="Sibling service names that were deployed alongside the primary",
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


class ChatAgentEnvUpdate(BaseModel):
    """Request body for PUT /chat/env/{name}.

    Secret values are accepted in the ``secrets`` dict and are encrypted
    at rest — they are never logged or echoed in responses.
    """

    env: dict[str, str] = Field(
        default={},
        description="Plain-text environment variables to set (key → value)",
    )
    secrets: dict[str, str] = Field(
        default={},
        description="Secret environment variables to set (key → value). Values are encrypted at rest and never returned in responses.",
    )
    env_scopes: dict[str, str] = Field(
        default={},
        description="Visibility scope tags for env keys",
    )
    secret_scopes: dict[str, str] = Field(
        default={},
        description="Visibility scope tags for secret keys",
    )


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


class ChatAgentDiskReclaimRequest(BaseModel):
    """Request body for POST /chat/disk/reclaim.

    Selects which safe reclaim targets to prune.  Only ``dangling_images``
    and ``build_cache`` are accepted — tagged images, in-use images, and
    named volumes are never pruned.
    """

    dangling_images: bool = Field(
        default=False,
        description="Prune dangling (untagged) Docker images.",
    )
    build_cache: bool = Field(
        default=False,
        description="Prune reclaimable Docker build cache.",
    )


class ChatAgentDiskReclaimResponse(BaseModel):
    """Response body for POST /chat/disk/reclaim."""

    name: str = Field(default="central-deploy")
    action: str = Field(default="disk-reclaim")
    space_reclaimed_bytes: int = Field(
        description="Total bytes freed by the reclaim operation."
    )
    detail: str = Field(default="", description="Human-readable summary")
    disk_snapshot: "DiskUsageResponse | None" = Field(
        default=None,
        description="Full disk-usage snapshot taken after the reclaim operation.",
    )
