"""Guard against component-level auth creeping back into config or docs.

Component-level authentication was deleted (auth-removal epic): the fleet
edge — Traefik plus tinyauth — is the only gate, and central-deploy holds
itself to the same fleet-wide rule it enforces on every other component.

``tests/lifecycle/test_app.py`` already guards the *runtime* surface: no route
may carry a real auth dependency.  This module guards the surface that test
cannot see — the committed configuration files, the configuration models, and
the operator-facing documentation — because a reintroduced ``api_key`` key or
a documented ``AUTH_PASSWORD`` variable costs the same credential-provisioning
mistakes even when no route reads it.

Note that ``data/`` is gitignored: the live settings store is operator state,
not a committed default, so only tracked files are scanned here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.registry.settings_store import SystemSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Config keys that carried component-level credentials.  Matched
#: case-insensitively as whole key names, so ``board_api_token`` and
#: ``ghcr_pull_token`` (credentials central-deploy *presents* to services it
#: calls, which are not a gate on inbound requests) stay allowed.
BANNED_KEYS = frozenset({"api_key", "auth_username", "auth_password"})

#: The committed configuration surface.  ``data/`` is deliberately absent.
CONFIG_FILES = (
    "config/config.json",
    "config/config.example.json",
    "config/config.schema.json",
)

#: Documented variable names for the same deleted settings, plus the login
#: rate limits that only ever guarded the deleted login endpoint.
BANNED_DOC_TOKENS = (
    "ROBOTSIX_LIFECYCLE_API_KEY",
    "ROBOTSIX_LIFECYCLE_AUTH_USERNAME",
    "ROBOTSIX_LIFECYCLE_AUTH_PASSWORD",
    "ROBOTSIX_LIFECYCLE_RATE_LIMIT_LOGIN_PER_MINUTE",
    "ROBOTSIX_LIFECYCLE_RATE_LIMIT_LOGIN_MAX_ATTEMPTS",
    "ROBOTSIX_LIFECYCLE_RATE_LIMIT_LOGIN_LOCKOUT_SECONDS",
)

DOC_FILES = ("docs/lifecycle/configuration.md",)


def _banned_key_paths(node: object, trail: str = "") -> list[str]:
    """Return dotted paths of every banned key reachable from *node*."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{trail}.{key}" if trail else str(key)
            if isinstance(key, str) and key.lower() in BANNED_KEYS:
                found.append(path)
            found.extend(_banned_key_paths(value, path))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_banned_key_paths(value, f"{trail}[{index}]"))
    return found


@pytest.mark.parametrize("relative_path", CONFIG_FILES)
def test_config_files_carry_no_auth_keys(relative_path: str) -> None:
    """No committed config file may declare a component-level auth key."""
    path = REPO_ROOT / relative_path
    assert path.is_file(), f"{relative_path} is missing — update CONFIG_FILES"

    found = _banned_key_paths(json.loads(path.read_text()))
    assert not found, (
        f"{relative_path} reintroduces component-level auth key(s) at "
        f"{found} — the fleet edge is the only gate (see docs/edge.md)."
    )


@pytest.mark.parametrize("model", [LifecycleConfig, SystemSettings])
def test_config_models_carry_no_auth_fields(model: type) -> None:
    """Neither config model may declare a component-level auth field."""
    offenders = sorted(f for f in model.model_fields if f.lower() in BANNED_KEYS)
    assert not offenders, (
        f"{model.__name__} reintroduces component-level auth field(s) "
        f"{offenders} — the fleet edge is the only gate (see docs/edge.md)."
    )


@pytest.mark.parametrize("relative_path", DOC_FILES)
def test_docs_do_not_document_removed_auth_settings(relative_path: str) -> None:
    """Docs must not advertise settings nothing reads any more."""
    path = REPO_ROOT / relative_path
    assert path.is_file(), f"{relative_path} is missing — update DOC_FILES"

    text = path.read_text()
    found = [token for token in BANNED_DOC_TOKENS if token in text]
    assert not found, (
        f"{relative_path} documents removed setting(s) {found}; nothing reads "
        f"them, so an operator following the docs configures nothing."
    )
