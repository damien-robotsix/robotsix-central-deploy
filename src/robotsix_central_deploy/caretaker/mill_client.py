"""HTTP client for the mill component's ticket-ingest and repo-registration APIs.

Used ONLY by the onboarding flow (repo registration + the one-time
port-collision finding). The periodic caretaker no longer talks to the mill:
it records findings locally and just updates containers (operator decision,
2026-09-01).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
from robotsix_http import ExternalHTTPError, RetryClient

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

    def __init__(self, base_url: str, http_client: RetryClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http_client

    #: Stages whose agent runs are hour-scale; aborting one wastes real work.
    #: Cheap, seconds-scale stages (classify, retrospect, …) are NOT listed:
    #: deferring on those would starve updates on a busy board forever.
    HEAVY_STAGES: frozenset[str] = frozenset({"implement", "ci_fix", "refine"})

    async def active_stage_summary(self) -> str | None:
        """GET {base_url}/active — summarise the mill's heavy running stages.

        Returns a short human-readable summary (e.g. ``"2 heavy stage(s):
        implement×2"``) when the mill has :data:`HEAVY_STAGES` in flight, or
        ``None`` when only cheap stages (or nothing) run.  Fails open: any
        transport or shape error also returns ``None`` — an unreachable or
        broken mill must not block its own update (deploying is then the
        likely remedy, not the risk).
        """
        try:
            response = await self._http.get(f"{self._base_url}/active")
            stages = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("mill /active probe failed (fail-open): %s", exc)
            return None
        if not isinstance(stages, list):
            return None
        counts: dict[str, int] = {}
        for entry in stages:
            stage = entry.get("stage", "") if isinstance(entry, dict) else ""
            if stage in self.HEAVY_STAGES:
                counts[stage] = counts.get(stage, 0) + 1
        if not counts:
            return None
        total = sum(counts.values())
        detail = ", ".join(f"{stage}×{n}" for stage, n in sorted(counts.items()))
        return f"{total} heavy stage(s) in flight: {detail}"

    async def ingest_finding(self, finding: CaretakerFinding) -> bool:
        """POST {base_url}/tickets/ingest — report a finding to the mill.

        The mill endpoint deduplicates by repo_id + title, so re-reports
        of a persisting problem do not spawn duplicate tickets.
        """
        try:
            await self._http.post(
                f"{self._base_url}/tickets/ingest",
                json={
                    "repo_id": finding.repo_id,
                    "title": finding.title,
                    "body": finding.detail,
                    "source_tag": f"caretaker/{finding.kind.value}",
                },
            )
            return True
        except ExternalHTTPError as exc:
            logger.warning(
                "mill ingest returned %d for finding %s/%s: %s",
                exc.status_code,
                finding.repo_id,
                finding.title,
                exc.response.text if exc.response is not None else str(exc),
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
            await self._http.post(
                f"{self._base_url}/repos",
                json={"repo_id": repo_id, "forge_remote_url": git_url},
            )
            logger.info("registered repo %s with mill", repo_id)
            return True
        except ExternalHTTPError as exc:
            logger.warning(
                "mill repo registration returned %d for %s",
                exc.status_code,
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
        mill_component_id: str = "",
    ) -> str | None:
        """Find the mill component in the registry and derive its URL.

        The component id to look up comes from the ``mill_component_id``
        system setting.  Returns
        ``http://{container_name}:{container_port}`` for the mill's first
        port mapping — managed components publish no host ports (the
        gateway reaches them over the shared proxy network by container
        name, and so must the caretaker) — or None when no such component
        is registered.
        """
        mill_cfg = component_config_store.get(mill_component_id)
        if mill_cfg is None:
            return None
        if not mill_cfg.ports:
            return None
        container_port = mill_cfg.ports[0].container
        return f"http://{mill_cfg.container_name}:{container_port}"
