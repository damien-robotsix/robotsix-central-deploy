"""Chat agent router — router aggregation for the chat-agent endpoints.

The domain modules (chat_components, chat_config, chat_self, chat_services,
chat_audit) each define their own ``APIRouter``.  This module aggregates them
into a single ``router`` so ``app.py`` only needs a single ``include_router``
call.

``_skill_cache`` and ``_SKILL_CACHE_TTL`` from ``chat_components`` are
re-exported here so that the test suite can access them via
``robotsix_central_deploy.lifecycle.routers.chat``.
"""

from __future__ import annotations

from fastapi import APIRouter

# Re-export for test access (CodeQL FP: these are accessed externally via
# ``robotsix_central_deploy.lifecycle.routers.chat``).
from .chat_components import _SKILL_CACHE_TTL, _skill_cache  # noqa: F401

# Import domain routers
from .chat_components import router as _components_router
from .chat_config import router as _config_router
from .chat_env import router as _env_router
from .chat_self import router as _self_router
from .chat_services import router as _services_router
from .chat_audit import router as _audit_router

router = APIRouter()
router.include_router(_components_router)
router.include_router(_config_router)
router.include_router(_env_router)
router.include_router(_self_router)
router.include_router(_services_router)
router.include_router(_audit_router)
