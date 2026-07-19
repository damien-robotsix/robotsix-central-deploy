"""Chat-agent GitHub router — thin aggregator.

See the domain modules for the implementations:
- chat_github_actions.py  — GitHub Actions
- chat_github_pulls.py    — PRs & Reviews
- chat_github_repos.py    — Repository CRUD
- chat_github_security.py — Security features
"""

from __future__ import annotations

from fastapi import APIRouter

from ..github_app import get_github_client, get_repo_create_client
from .chat_github_actions import router as _actions_router
from .chat_github_pulls import router as _pulls_router
from .chat_github_repos import router as _repos_router
from .chat_github_security import router as _security_router

__all__ = ["get_github_client", "get_repo_create_client", "router"]
router = APIRouter()
router.include_router(_actions_router)
router.include_router(_pulls_router)
router.include_router(_repos_router)
router.include_router(_security_router)