"""Pydantic request/response schemas for lifecycle endpoints.

Extracted from the monolithic server.py so that each router module can
import the models it needs without importing the FastAPI app.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from robotsix_central_deploy.onboard.models import DerivedSpec  # noqa: TCH001
from robotsix_central_deploy.registry.models import ConfigAssistSeed  # noqa: TCH001


# ---------------------------------------------------------------------------
# Onboard request / response models
# ---------------------------------------------------------------------------


class OnboardPreflightRequest(BaseModel):
    git_url: str
    name: str  # validated: ^[a-z0-9][a-z0-9-]*$


class OnboardPreflightResponse(BaseModel):
    spec: DerivedSpec


class OnboardConfirmRequest(BaseModel):
    spec: DerivedSpec  # env values now user-filled
    config_values: dict[str, Any] | None = None  # optional, for config.yaml repos


class OnboardConfirmResponse(BaseModel):
    name: str
    image: str
    state: str


# ---------------------------------------------------------------------------
# Env endpoint models
# ---------------------------------------------------------------------------


class EnvResponse(BaseModel):
    env: dict[str, str]
    secrets: dict[str, str]  # values are always "***"


class EnvUpdate(BaseModel):
    env: dict[str, str] = {}
    secrets: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Volume browser models
# ---------------------------------------------------------------------------


class VolumeEntry(BaseModel):
    name: str
    type: str  # "file" or "dir"
    size_bytes: int


class VolumeListResponse(BaseModel):
    entries: list[VolumeEntry]


class VolumeFileResponse(BaseModel):
    size_bytes: int
    content: str | None
    binary: bool
    truncated: bool


# ---------------------------------------------------------------------------
# Config endpoint models
# ---------------------------------------------------------------------------


class ConfigResponse(BaseModel):
    config_schema: dict[str, Any] = Field(serialization_alias="schema")
    current: dict[str, Any]
    config_assist_command: str | None = None
    config_assist_seeds: list[ConfigAssistSeed] = []


class ConfigUpdate(BaseModel):
    values: dict[str, Any]


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
