"""Config endpoint request / response schemas for lifecycle endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from robotsix_central_deploy.registry import ConfigAssistSeed


class ConfigResponse(BaseModel):
    """Config schema, current values, and assist metadata from GET /services/{name}/config."""

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
    component_settings_url: str | None = Field(
        default=None,
        description=(
            "URL to the component's own Settings/Config panel when the gateway "
            "is configured; None otherwise.  Component-owned keys should be "
            "edited there, not in the deploy config form."
        ),
    )


class SelfConfigResponse(BaseModel):
    """Response body for ``GET /config`` — central-deploy's own settings.

    Shape fixed by the robotsix-standards config-ownership standard, which is
    also what ``@robotsix/ui``'s ``mountConfigPanel`` reads. Field names are
    part of that contract; ``config_schema`` serialises as ``schema`` because
    ``schema`` shadows a BaseModel attribute.
    """

    config: dict[str, Any] = Field(
        description="Full effective config values; secrets masked"
    )
    config_schema: dict[str, Any] = Field(
        serialization_alias="schema",
        description="JSON Schema of LifecycleConfig, for rendering typed inputs",
    )
    version: int = Field(
        description="Monotonic config version; 0 when nothing has been written yet"
    )


class SelfConfigWriteResponse(BaseModel):
    """Response body for ``PUT /config`` and ``POST /config/rollback``."""

    config: dict[str, Any] = Field(
        description="Full effective config after the write; secrets masked"
    )
    version: int = Field(description="Version number the write produced")


class SelfConfigVersion(BaseModel):
    """One entry of central-deploy's own config history."""

    version: int = Field(description="Monotonic version number")
    timestamp: str = Field(description="ISO-8601 UTC timestamp of the write")
    changed_keys: list[str] = Field(
        description=(
            "Top-level keys this version changed. A key whose change involved "
            "a secret is reported as '<key> (secret)' — the name is recorded, "
            "the value never is."
        )
    )


class SelfConfigVersionsResponse(BaseModel):
    """Response body for ``GET /config/versions``."""

    versions: list[SelfConfigVersion] = Field(
        description="Recorded versions, newest first"
    )


class SelfConfigRollbackRequest(BaseModel):
    """Request body for ``POST /config/rollback``."""

    version: int = Field(description="The version to restore")


class ConfigExportResponse(BaseModel):
    """Response body for GET /services/{name}/config/export (migration-only).

    Returns the full current config WITH unmasked secret values so
    components can import their config exactly once during the
    config-ownership migration.

    Access is restricted to localhost + API-key auth.
    """

    component: str = Field(description="Component name")
    values: dict[str, Any] = Field(
        description="Full current config values with unmasked secrets"
    )
    note: str = Field(
        default=(
            "Migration-only endpoint. Secrets are included in plaintext — "
            "treat this payload as sensitive."
        ),
        description="Usage note about the sensitivity of the payload",
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
    """Component suggestions returned by GET /components/suggest."""

    components: list[ComponentSuggestItem] = Field(
        description="Matching component suggestions"
    )


class ContractRefreshRequest(BaseModel):
    """Optional body of POST /services/{name}/refresh-contract."""

    allow_env_clear: bool = Field(
        default=False,
        description=(
            "Permit the refresh to blank a non-empty stored env value that the "
            "compose contract clears. Defaults to false, so such a refresh is "
            "refused with 409 to prevent silent secret loss."
        ),
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
    preserved: dict[str, Any] = Field(
        default={},
        description=(
            "Values kept from the stored config against the compose contract: "
            "an ``image``/``sibling_images`` tag kept over a digest pin, and "
            "``sibling_env`` keys whose stored secrets survived compose "
            "placeholders."
        ),
    )


# ---------------------------------------------------------------------------
# Claude auth request / response models
# ---------------------------------------------------------------------------
