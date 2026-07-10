"""Dependency providers, helpers, and lifespan for the lifecycle server.

Re-exports all ``_get_*`` FastAPI dependency factories and utility
symbols so existing ``from ..deps import _get_store`` imports in
router modules continue working unchanged.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import httpx

from . import background as _background
from . import lifespan as _lifespan
from .background import _claude_auth_refresh_loop, get_claude_auth_refresh_state
from .dependencies import (
    _compute_overall_health,
    _get_backend,
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_config,
    _get_config_yaml_store,
    _get_deploy_history_store,
    _get_env_store,
    _get_job_registry,
    _get_or_create_record,
    _get_registry,
    _get_registry_checker,
    _get_sibling_pairs,
    _get_store,
)
from .jobs import DeployJob, JobRegistry, OnboardJob
from .lifespan import _seed_component_registry, lifespan
from .seed import (
    _build_component_config_from_spec,
    _derive_account_id,
    _fetch_component_repo_files,
    _namespace_spec_volumes,
    _prune_unset,
    _relocate_account_seed_values,
    _resolve_placeholders,
    _seed_for_detect,
    _seed_list_item,
    _validate_account_ids,
    _validate_config_or_422,
)
from .volume import (
    VOLUME_CAT_MAX_BYTES,
    _assert_volume_browsable,
    _compute_orphan_volumes,
    _validate_volume_path,
)


# -- Proxy _claude_auth_refresh_state so rebinding in __init__ ----------
#    also rebinds background._claude_auth_refresh_state.  Tests (and
#    callers) assign directly to ``deps._claude_auth_refresh_state``;
#    the background refresh loop reads/writes the same variable via
#    its own module scope.  We use a custom module __getattr__ /
#    __setattr__ pair to keep both bindings in sync.


# Capture originals before any patching can occur.
_original_build_backend = _lifespan._build_backend


class _DepsModule(ModuleType):
    """Module subclass that proxies ``_claude_auth_refresh_state``
    reads/writes to the ``background`` submodule, and
    ``_build_backend`` reads/writes to the ``lifespan`` submodule."""

    def __getattr__(self, name: str) -> Any:
        if name == "_claude_auth_refresh_state":
            return _background._claude_auth_refresh_state
        if name == "_build_backend":
            return _lifespan._build_backend
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_claude_auth_refresh_state":
            _background._claude_auth_refresh_state = value
            return
        if name == "_build_backend":
            _lifespan._build_backend = value
            return
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name == "_claude_auth_refresh_state":
            return  # nothing to delete — proxy always returns the real attribute
        if name == "_build_backend":
            _lifespan._build_backend = _original_build_backend
            return
        super().__delattr__(name)


_current = sys.modules[__name__]
_current.__class__ = _DepsModule

# -----------------------------------------------------------------------

__all__ = [
    "httpx",
    # background
    "_claude_auth_refresh_loop",
    "_claude_auth_refresh_state",
    "get_claude_auth_refresh_state",
    # dependencies
    "_compute_overall_health",
    "_get_backend",
    "_get_chat_agent_audit_store",
    "_get_component_config_store",
    "_get_config",
    "_get_config_yaml_store",
    "_get_deploy_history_store",
    "_get_env_store",
    "_get_job_registry",
    "_get_or_create_record",
    "_get_registry",
    "_get_registry_checker",
    "_get_sibling_pairs",
    "_get_store",
    # jobs
    "DeployJob",
    "JobRegistry",
    "OnboardJob",
    # lifespan
    "_build_backend",
    "_seed_component_registry",
    "lifespan",
    # seed
    "_build_component_config_from_spec",
    "_derive_account_id",
    "_fetch_component_repo_files",
    "_namespace_spec_volumes",
    "_prune_unset",
    "_relocate_account_seed_values",
    "_resolve_placeholders",
    "_seed_for_detect",
    "_seed_list_item",
    "_validate_account_ids",
    "_validate_config_or_422",
    # volume
    "VOLUME_CAT_MAX_BYTES",
    "_assert_volume_browsable",
    "_compute_orphan_volumes",
    "_validate_volume_path",
]
