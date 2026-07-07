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
    OnboardJobPhase,
    VolumeEntryType,
)
from robotsix_central_deploy.onboard.models import DerivedSpec  # noqa: TCH001
from robotsix_central_deploy.registry.models import ConfigAssistSeed  # noqa: TCH001


# ---------------------------------------------------------------------------
# Onboard request / response models
# ---------------------------------------------------------------------------


class PortShift(BaseModel):
    container_port: int
    protocol: str
    original_host: int  # default from the repo's docker-compose.yml
    assigned_host: int  # auto-assigned free port
    collision_component_id: str  # component whose stored port collided ("central-deploy" for lifecycle port)
    collision_repo_id: str  # that component's repo_id, or "" when unknown


class OnboardPreflightRequest(BaseModel):
    git_url: str
    name: str  # validated: ^[a-z0-9][a-z0-9-]*$


class OnboardPreflightResponse(BaseModel):
    spec: DerivedSpec
    port_shifts: list[PortShift] = []


class OnboardConfirmRequest(BaseModel):
    spec: DerivedSpec  # env values now user-filled
    config_values: dict[str, Any] | None = None  # optional, for config.json repos
    register_with_mill: bool = True
    port_shifts: list[
        PortShift
    ] = []  # echoed from preflight; used only for ticket filing


class OnboardConfirmAcceptedResponse(BaseModel):
    """Returned by POST /onboard/confirm (202) when the job is queued."""

    job_id: str
    name: str


class OnboardJobStatusResponse(BaseModel):
    """Returned by GET /onboard/jobs/{job_id}."""

    job_id: str
    component: str
    phase: OnboardJobPhase
    error: str | None = None
    name: str | None = None
    image: str | None = None
    state: str | None = None
    warnings: list[
        str
    ] = []  # non-empty when mill was unreachable during port-shift ticket filing


# ---------------------------------------------------------------------------
# Deploy job models (async deploy pattern)
# ---------------------------------------------------------------------------


class DeployAcceptedResponse(BaseModel):
    """Returned by POST /services/{name}/deploy (202) when the job is queued."""

    job_id: str
    name: str


class DeployJobStatusResponse(BaseModel):
    """Returned by GET /services/deploy-jobs/{job_id}."""

    job_id: str
    component: str
    phase: DeployJobPhase
    error: str | None = None
    name: str | None = None
    image: str | None = None
    state: str | None = None
    warnings: list[str] = []


# ---------------------------------------------------------------------------
# Env endpoint models
# ---------------------------------------------------------------------------


class EnvResponse(BaseModel):
    env: dict[str, str]
    secrets: dict[str, str]  # values are always "***"
    mem_limit: str = "2g"
    allow_chat_access: bool = False
    claude_mount: bool = False


class EnvSyncResponse(BaseModel):
    """Body of the 200 response from POST /services/{name}/env/sync-keys."""

    added_env: list[str]  # newly declared plain env keys, seeded with defaults
    added_secrets: list[str]  # newly declared secret slots (empty value in contract)
    undeclared: list[str]  # stored keys the compose contract no longer declares


class EnvUpdate(BaseModel):
    env: dict[str, str] = {}
    secrets: dict[str, str] = {}
    mem_limit: str | None = None
    allow_chat_access: bool | None = None
    claude_mount: bool | None = None


# ---------------------------------------------------------------------------
# Volume browser models
# ---------------------------------------------------------------------------


class VolumeEntry(BaseModel):
    name: str
    type: VolumeEntryType
    size_bytes: int


class VolumeListResponse(BaseModel):
    entries: list[VolumeEntry]


class VolumeFileResponse(BaseModel):
    size_bytes: int
    content: str | None
    binary: bool
    truncated: bool


# ---------------------------------------------------------------------------
# Orphan-volume prune models
# ---------------------------------------------------------------------------


class OrphanVolume(BaseModel):
    """A Docker volume owned by no registered component and not in use."""

    name: str
    size_bytes: int = 0


class OrphanVolumesResponse(BaseModel):
    volumes: list[OrphanVolume] = []
    total_bytes: int = 0


class PruneVolumesRequest(BaseModel):
    # None → prune every orphan candidate; a list → prune only those names that
    # are (still) genuine orphan candidates (others are reported under skipped).
    names: list[str] | None = None


class PruneVolumesResponse(BaseModel):
    removed: list[str] = []  # volumes confirmed gone after the prune
    skipped: list[str] = []  # requested names that were not eligible orphans
    failed: list[str] = []  # eligible orphans that were still present afterwards
    space_reclaimed_bytes: int = 0


# ---------------------------------------------------------------------------
# Config endpoint models
# ---------------------------------------------------------------------------


class ConfigResponse(BaseModel):
    config_schema: dict[str, Any] = Field(serialization_alias="schema")
    current: dict[str, Any]
    drift: bool = False
    config_assist_command: str | None = None
    config_assist_seeds: list[ConfigAssistSeed] = []


class ConfigUpdate(BaseModel):
    values: dict[str, Any]
    force_overwrite: bool = False


class ComponentSuggestItem(BaseModel):
    """Lightweight component info for the config-form URL suggest feature."""

    id: str
    container_name: str
    container_port: int | None  # first container port, None when no ports


class ComponentSuggestResponse(BaseModel):
    components: list[ComponentSuggestItem]


class ConfigDriftConflict(BaseModel):
    """Body of the 409 Conflict response when drift is detected on Save."""

    drift: Literal[True] = True
    live_config: dict[str, Any]  # current volume content, secrets masked
    stored_config: dict[str, Any]  # store's current dict, secrets masked


class ConfigImportResponse(BaseModel):
    """Body of the 200 response from POST /services/{name}/config/import."""

    current: dict[str, Any]  # imported (and secret-masked) current values
    volume_hash: str  # canonical hash of the imported content


class ConfigSchemaRefreshResponse(BaseModel):
    """Body of the 200 response from POST /services/{name}/config/refresh-schema."""

    config_schema: dict[str, Any] = Field(serialization_alias="schema")


class ContractRefreshResponse(BaseModel):
    """Body of the 200 response from POST /services/{name}/refresh-contract."""

    name: str
    changed_fields: list[str] = []
    previous: dict[str, Any] = {}
    current: dict[str, Any] = {}


class ConfigAssistRequest(BaseModel):
    values: dict[
        str, Any
    ]  # current (partial) form values — same shape as ConfigUpdate.values
    target_account_index: int | None = None
    account_name: str | None = None


class ConfigAssistResponse(BaseModel):
    config: dict[
        str, Any
    ]  # the auto-filled config dict read back from the volume after the command ran
    output: str  # captured stdout+stderr from the one-shot container


# ---------------------------------------------------------------------------
# Claude auth request / response models
# ---------------------------------------------------------------------------


class ClaudeAuthStatusResponse(BaseModel):
    status: str  # "authenticated" | "not-authenticated" | "expiring" | "error"
    detail: str = ""
    refresh_status: str = ""  # "ok" | "failed" | "never" | ""
    last_refresh_error: str = ""


class ClaudeAuthLoginResponse(BaseModel):
    login_id: str
    oauth_url: str


class ClaudeAuthCompleteRequest(BaseModel):
    login_id: str
    auth_code: str


class ClaudeAuthCancelRequest(BaseModel):
    login_id: str


class ClaudeAuthCompleteResponse(BaseModel):
    status: str  # "authenticated" | "error"
    error: str = ""


class ClaudeAuthCredentialsRequest(BaseModel):
    credentials_json: str


class ClaudeAuthCredentialsResponse(BaseModel):
    status: str  # "authenticated" | "error"
    error: str = ""


# ---------------------------------------------------------------------------
# Chat agent write-surface models
# ---------------------------------------------------------------------------


class ChatAgentConfigUpdate(BaseModel):
    """Request body for PUT /chat/config/{name}.

    Only non-secret keys are accepted; secret fields are rejected with 403.
    """

    values: dict[str, Any]


class ChatAgentConfigRollbackResponse(BaseModel):
    """Response body for PUT /chat/config/{name} and POST /chat/config/{name}/rollback."""

    component: str
    restored: dict[str, Any]  # secret-masked snapshot of the restored config
    detail: str = ""


class ChatAgentRestartResponse(BaseModel):
    """Response body for POST /chat/services/{name}/restart."""

    name: str
    action: ActionType = ActionType.RESTART
    previous_state: str
    current_state: str
    detail: str = ""


class ChatAgentUpdateResponse(BaseModel):
    """Response body for POST /chat/services/{name}/update."""

    name: str
    action: str = "update"
    deployed_digest: str = ""
    previous_digest: str = ""
    current_state: str
    detail: str = ""


class ChatAgentAuditEntryResponse(BaseModel):
    """One audit-log entry exposed by GET /chat/audit-log."""

    timestamp: float
    agent_id: str
    component: str
    action: str
    key: str | None = None
    old_value: Any = None
    new_value: Any = None
    detail: str = ""


class ChatAgentAuditLogResponse(BaseModel):
    """Response body for GET /chat/audit-log."""

    entries: list[ChatAgentAuditEntryResponse] = []
