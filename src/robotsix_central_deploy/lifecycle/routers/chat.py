"""Chat agent router — re-exports for test monkeypatching and router aggregation.

The domain modules (chat_components, chat_config, chat_self, chat_services,
chat_audit) each define their own ``APIRouter``.  This module aggregates them
into a single ``router`` so ``app.py`` only needs a single ``include_router``
call.

Shared plumbing lives in ``._chat_common`` and is re-exported here so that
the test suite can access ``_skill_cache``, ``_SKILL_CACHE_TTL``, and other
module-level state via ``robotsix_central_deploy.lifecycle.routers.chat``.
"""

from __future__ import annotations

from fastapi import APIRouter

# Re-export shared plumbing for test access
from ._chat_common import (  # noqa: F401
    _RATE_LIMIT_COOLDOWNS,
    _SKILL_CACHE_TTL,
    _check_rate_limit,
    _inject_auth,
    _require_allowed_service,
    _skill_cache,
    logger,
)

# Import domain routers
from .chat_components import router as _components_router
from .chat_config import router as _config_router
from .chat_self import router as _self_router
from .chat_services import router as _services_router
from .chat_audit import router as _audit_router

router = APIRouter(tags=["chat"])
router.include_router(_components_router)
router.include_router(_config_router)
router.include_router(_self_router)
router.include_router(_services_router)
router.include_router(_audit_router)
