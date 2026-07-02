"""HTTP client for the mill component's ticket-ingest and repo-registration APIs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from .models import CaretakerFinding

if TYPE_CHECKING:
    from ..registry.config_store import ComponentConfigStore
    from ..registry.loader import ComponentRegistry

logger = logging.getLogger(__name__)


class MillClient:
    """Thin async HTTP wrapper over the mill's ingest and repo endpoints.

    Every method returns a bool (True on 2xx, False on any error) — never
    raises.  This keeps the caretaker scheduler resilient: a mill outage
    degrades to local-JSONL fallback but does not crash the loop.
    """

    def __init__(self, base_url: str, http_client: httpx.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http_client

    async def ingest_finding(self, finding: CaretakerFinding) -> bool:
        """POST {base_url}/tickets/ingest — report a finding to the mill.

        The mill endpoint deduplicates by repo_id + title, so re-reports
        of a persisting problem do not spawn duplicate tickets.
        """
        try:
            resp = await self._http.post(
                f"{self._base_url}/tickets/ingest",
                json={
                    "repo_id": finding.repo_id,
                    "title": finding.title,
                    "body": finding.detail,
                    "kind": finding.kind.value,
                },
            )
            if resp.is_success:
                return True
            logger.warning(
                "mill ingest returned %d for finding %s/%s",
                resp.status_code,
                finding.repo_id,
                finding.title,
            )
            return False
        except httpx.HTTPError as exc:
            logger.warning("mill ingest call failed: %s", exc)
            return False

    async def register_repo(self, repo_id: str, git_url: str) -> bool:
        """POST {base_url}/repos — register a new repo with the mill.

        Called once during onboard; best-effort — failure does not block
        onboarding.
        """
        try:
            resp = await self._http.post(
                f"{self._base_url}/repos",
                json={"repo_id": repo_id, "git_url": git_url},
            )
            if resp.is_success:
                logger.info("registered repo %s with mill", repo_id)
                return True
            logger.warning(
                "mill repo registration returned %d for %s",
                resp.status_code,
                repo_id,
            )
            return False
        except httpx.HTTPError as exc:
            logger.warning("mill repo registration call failed: %s", exc)
            return False

    @staticmethod
    def derive_url_from_registry(
        registry: ComponentRegistry,
        component_config_store: ComponentConfigStore,
        mill_component_id: str = "mill",
    ) -> str | None:
        """Find the mill component in the registry and derive its URL.

        The component id to look up comes from the ``mill_component_id``
        system setting (default ``"mill"``).  Returns
        ``http://localhost:{host_port}`` for the mill's first port
        mapping, or None when no such component is registered.
        """
        mill_cfg = component_config_store.get(mill_component_id)
        if mill_cfg is None:
            return None
        if not mill_cfg.ports:
            return None
        host_port = mill_cfg.ports[0].host
        return f"http://localhost:{host_port}"
