"""Chat-agent GitHub router — re-exports for test monkeypatching.

The domain modules import ``get_github_client`` and
``get_repo_create_client`` from here (not directly from ``..github_app``)
so that the test suite can monkeypatch ``chat_github.get_github_client``
and have the patch take effect everywhere.

This is the sole implementation of GitHub access for the chat agent — the
chat container never holds a GitHub credential of its own. Actions-status
and repo-read/update mint a GitHub App installation token server-side
(:mod:`..github_app`). Repo
creation alone uses a separate PAT (``github_repo_create_token``): GitHub
App installation tokens cannot create repositories under a personal
account.

Reads need no audit/confirmation gate. Repo update, security-features
toggle, repo creation, review submission, and review dismissal are genuine
mutations, so all are audit-logged (mirroring :mod:`.chat`'s
config/restart/update endpoints) and — per the ``github`` skill's
documented safety rule — expected to only be called after the chat agent
has obtained explicit user confirmation in-conversation (a server-side
confirmation gate isn't possible here; the skill text is the enforcement
point, same as the config/restart/update endpoints in :mod:`.chat`).

Router aggregation is done in ``app.py``, which imports each domain
router directly — that avoids a top-level import cycle between this
module and the domain routers.
"""

from __future__ import annotations

from ..github_app import get_github_client, get_repo_create_client  # noqa: F401

__all__ = ["get_github_client", "get_repo_create_client"]
