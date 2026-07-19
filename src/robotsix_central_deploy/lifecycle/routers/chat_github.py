"""Chat-agent GitHub router — re-exports for test monkeypatching.

The domain modules import ``get_github_client`` and
``get_repo_create_client`` from here (not directly from ``..github_app``)
so that the test suite can monkeypatch ``chat_github.get_github_client``
and have the patch take effect everywhere.

Router aggregation is done in ``app.py``, which imports each domain
router directly — that avoids a top-level import cycle between this
module and the domain routers.
"""

from __future__ import annotations

from ..github_app import get_github_client, get_repo_create_client  # noqa: F401

__all__ = ["get_github_client", "get_repo_create_client"]
