"""Env endpoint request / response schemas for lifecycle endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field

from robotsix_central_deploy.lifecycle.schemas._base import _EnvSecretsFields


class EnvResponse(BaseModel):
    """Env, secrets (masked), and runtime toggles from GET /services/{name}/env."""

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
    memswap_limit: str | None = Field(
        default=None,
        description="Optional Docker memory+swap limit string (e.g. '4g')",
    )
    allow_chat_access: bool = Field(
        default=False,
        description="Whether chat-agent mutation is permitted for this component",
    )
    claude_mount: bool = Field(
        default=False,
        description="Whether the Claude code mount is enabled for this component",
    )
    auto_update_enabled: bool = Field(
        default=True,
        description=(
            "Whether the caretaker may automatically update this component. "
            "For central-deploy this governs the plane's own self-update."
        ),
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


class EnvUpdate(_EnvSecretsFields):
    """Request body for PUT /services/{name}/env; None fields are left unchanged."""

    mem_limit: str | None = Field(
        default=None,
        description="Docker memory limit; None leaves the current value unchanged",
    )
    memswap_limit: str | None = Field(
        default=None,
        description="Docker memory+swap limit; None leaves the current value unchanged",
    )
    allow_chat_access: bool | None = Field(
        default=None,
        description="Toggle chat-agent mutability; None leaves the current value unchanged",
    )
    claude_mount: bool | None = Field(
        default=None,
        description="Toggle Claude code mount; None leaves the current value unchanged",
    )
    auto_update_enabled: bool | None = Field(
        default=None,
        description=(
            "Toggle whether the caretaker may automatically update this "
            "component (for central-deploy, its own self-update); None leaves "
            "the current value unchanged"
        ),
    )


# ---------------------------------------------------------------------------
# Volume browser models
# ---------------------------------------------------------------------------
