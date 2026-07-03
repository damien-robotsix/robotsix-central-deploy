"""Backward-compatibility shim — re-exports from the new modular layout.

After the monolithic server.py was split into deps.py, schemas.py, app.py,
and the routers/ subpackage, this module exists so that existing external
imports (``from robotsix_central_deploy.lifecycle.server import app``, etc.)
continue to work without changes.
"""

from __future__ import annotations

import shutil  # noqa: F401 — tests monkeypatch server_mod.shutil

# Lifespan (tests import it directly)
from .deps import lifespan as lifespan  # noqa: F401

# Backend (tests monkeypatch server_mod.NoopBackend)
from .backend import NoopBackend as NoopBackend  # noqa: F401

# Config helpers (tests import these directly)
from .deps import (  # noqa: F401
    _mask_secrets as _mask_secrets,
    _merge_config as _merge_config,
    _prune_unset as _prune_unset,
    _seed_for_detect as _seed_for_detect,
    _validate_account_ids as _validate_account_ids,
    _validate_config_or_422 as _validate_config_or_422,
    _fetch_fresh_config_assist as _fetch_fresh_config_assist,
    _namespace_spec_volumes as _namespace_spec_volumes,
    VOLUME_CAT_MAX_BYTES as VOLUME_CAT_MAX_BYTES,
)

# Re-exports that tests reference via ``server_mod.<name>``
from .volume_audit.models import VolumeAuditResponse as VolumeAuditResponse  # noqa: F401


# App — lazy-loaded to avoid a circular import between
#   server.py -> app.py -> routers/service_config.py -> server.py
def __getattr__(name: str):
    if name == "app":
        from .app import app as _app

        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "app",  # noqa: F822 — lazy via __getattr__
    "lifespan",
    "_mask_secrets",
    "_merge_config",
    "_prune_unset",
    "_seed_for_detect",
    "_validate_account_ids",
    "_validate_config_or_422",
    "_fetch_fresh_config_assist",
    "_namespace_spec_volumes",
    "VOLUME_CAT_MAX_BYTES",
    "VolumeAuditResponse",
    "NoopBackend",
]
