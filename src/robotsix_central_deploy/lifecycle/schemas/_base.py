"""Shared base mixins for lifecycle endpoint schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class _JobStatusFields(BaseModel):
    """Status fields shared by the async onboard/deploy job responses."""

    job_id: str = Field(description="Unique identifier of the job")
    error: str | None = Field(
        default=None,
        description="Error message when phase is 'failed'; None otherwise",
    )
    logs: str | None = Field(
        default=None,
        description="Container logs captured when a deploy fails; None otherwise",
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


class _EnvSecretsFields(BaseModel):
    """Env/secret dict fields shared by the env-update request bodies."""

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


class _DeployResultBase(BaseModel):
    """Result fields shared by the chat-agent update/deploy responses."""

    name: str = Field(description="Component name")
    deployed_digest: str = Field(
        default="",
        description="Digest of the newly deployed image; empty when unchanged",
    )
    previous_digest: str = Field(
        default="", description="Digest of the previously deployed image"
    )
    current_state: str = Field(description="Container state after the operation")
    detail: str = Field(default="", description="Human-readable summary")
