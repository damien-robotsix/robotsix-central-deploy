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

from .chat_audit import router as _audit_router

# Re-export for test access (CodeQL FP: these are accessed externally via
# ``robotsix_central_deploy.lifecycle.routers.chat``).
from .chat_components import _SKILL_CACHE_TTL, _skill_cache  # noqa: F401

# Import domain routers
from .chat_components import router as _components_router
from .chat_config import router as _config_router
from .chat_deploy import router as _deploy_router
from .chat_disk import router as _disk_router
from .chat_env import router as _env_router
from .chat_mutation import router as _mutation_router
from .chat_observability import router as _observability_router
from .chat_register import router as _register_router
from .chat_restart import router as _restart_router
from .chat_self import router as _self_router
from .chat_test_deploy import router as _test_deploy_router

router = APIRouter()
router.include_router(_components_router)
router.include_router(_config_router)
router.include_router(_disk_router)
router.include_router(_env_router)
router.include_router(_self_router)
router.include_router(_deploy_router)
router.include_router(_mutation_router)
router.include_router(_register_router)
router.include_router(_restart_router)
router.include_router(_audit_router)
router.include_router(_test_deploy_router)
router.include_router(_observability_router)
