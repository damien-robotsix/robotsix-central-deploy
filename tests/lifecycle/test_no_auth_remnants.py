"""Guard against component-level auth creeping back into config or docs.

Component-level authentication was deleted (auth-removal epic): the fleet
edge — Traefik plus tinyauth — is the only gate, and central-deploy holds
itself to the same fleet-wide rule it enforces on every other component.

``tests/lifecycle/test_app.py`` already guards the *runtime* surface: no route
may carry a real auth dependency.  This module guards what that test cannot
see — the committed configuration files and the configuration models — because
a reintroduced ``api_key`` key costs the same credential-provisioning mistakes
even when no route reads it.

The docs half lives in ``test_no_env_config_channel.py``, which forbids naming
*any* concrete ``ROBOTSIX_LIFECYCLE_*`` variable and so covers the deleted auth
and login-rate-limit names as a special case.

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
