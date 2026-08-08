"""Minimal board REST client for filing volume-audit findings.

This used to come from ``robotsix-board-agent``, a git dependency pinned by
commit. That repository was archived and made private on 2026-08-08, at which
point every CI job here failed at dependency install with ``could not read
Username for 'https://github.com'`` — an unfetchable pin takes the whole
repository's CI down, and no amount of local correctness works around it.

Rather than restore access to an archived repository, the two calls this
package actually made are reimplemented here. ``robotsix-board-agent`` exposed
a full board client — tickets, comments, transitions, approvals — of which the
volume audit used exactly ``create_ticket`` and ``close``.

The wire format is unchanged: ``POST /tickets`` with a bearer token, so an
existing ``board_api_url`` / ``board_api_token`` / ``board_repo_id`` config
keeps working untouched.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class BoardAPIError(Exception):
    """Raised when the board API returns a non-2xx status code."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Board API error {status_code}: {detail}")


class BoardClient:
    """Files tickets on the mill board.

    Only the operations the volume audit performs are implemented. Anything
    further belongs in the board's own client, not here.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        repo_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._repo_id = repo_id
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http is None:
            # An empty token would yield a bare ``Bearer `` header, which httpx
            # rejects — and a loopback board needs no auth at all, so send no
            # header rather than an empty one.
            headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                transport=self._transport,
            )
        return self._http

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def create_ticket(
        self,
        title: str,
        description: str,
        source: str = "agent",
        kind: str = "task",
        repo_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a ticket, defaulting ``repo_id`` to the configured repo."""
        body: dict[str, Any] = {
            "title": title,
            "description": description,
            "source": source,
            "kind": kind,
            "repo_id": repo_id or self._repo_id,
        }
        client = await self._get_client()
        resp = await client.request("POST", "/tickets", json=body)
        if resp.is_error:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            logger.warning(
                "Board API POST /tickets -> %d %s", resp.status_code, resp.reason_phrase
            )
            raise BoardAPIError(resp.status_code, detail)
        result: dict[str, Any] = resp.json()
        return result
