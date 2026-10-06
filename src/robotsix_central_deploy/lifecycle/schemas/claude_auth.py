"""Claude auth request / response schemas for lifecycle endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClaudeAuthStatusResponse(BaseModel):
    """Credential state returned by GET /claude-auth/status."""

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
    refresh_capable: bool = Field(
        default=True,
        description=(
            "Whether the stored credential carries a refresh token. When false "
            "the credential cannot be renewed and dies at its expiry, needing a "
            "manual re-login"
        ),
    )


class ClaudeAuthLoginResponse(BaseModel):
    """Login session and OAuth URL from POST /claude-auth/login."""

    login_id: str = Field(description="Opaque login session identifier")
    oauth_url: str = Field(description="OAuth URL the user must visit to authorize")


class ClaudeAuthCompleteRequest(BaseModel):
    """Request body for POST /claude-auth/login/complete."""

    login_id: str = Field(
        description="Login session identifier from the initiate-login response"
    )
    auth_code: str = Field(description="Authorization code from the OAuth callback")


class ClaudeAuthCancelRequest(BaseModel):
    """Request body for POST /claude-auth/login/cancel."""

    login_id: str = Field(description="Login session identifier to cancel")


class ClaudeAuthCompleteResponse(BaseModel):
    """Outcome of the OAuth code exchange from POST /claude-auth/login/complete."""

    status: str = Field(description="'authenticated' on success, 'error' on failure")
    error: str = Field(default="", description="Error message when status is 'error'")
    warning: str = Field(
        default="",
        description=(
            "Non-fatal problem with an otherwise successful login — currently "
            "set when the exchange returned no refresh token, so the credential "
            "cannot be auto-renewed"
        ),
    )


class ClaudeAuthCredentialsRequest(BaseModel):
    """Request body for POST /claude-auth/credentials (raw JSON import)."""

    credentials_json: str = Field(
        description="Raw credentials JSON blob from the OAuth provider"
    )


class ClaudeAuthCredentialsResponse(BaseModel):
    """Outcome of a raw-credentials import via POST /claude-auth/credentials."""

    status: str = Field(description="'authenticated' on success, 'error' on failure")
    error: str = Field(default="", description="Error message when status is 'error'")


# ---------------------------------------------------------------------------
# Chat agent write-surface models
# ---------------------------------------------------------------------------
