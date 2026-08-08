"""Tests for the in-repo board client.

This client replaced the one from ``robotsix-board-agent`` when that
repository was archived and made private. The wire format has to stay
identical — an existing deployment's `board_api_url` / `board_api_token` /
`board_repo_id` must keep working — so these tests pin the request the board
actually receives, not just the return value.
"""

from __future__ import annotations

import json

import httpx
import pytest

from robotsix_central_deploy.caretaker.volume_audit.board import (
    BoardAPIError,
    BoardClient,
)

_TOKEN = "tok"


def _client(handler, token: str = _TOKEN, repo_id: str = "repo-1") -> BoardClient:
    return BoardClient(
        base_url="http://board.local/",
        token=token,
        repo_id=repo_id,
        transport=httpx.MockTransport(handler),
    )


class TestCreateTicket:
    @pytest.mark.asyncio
    async def test_posts_to_tickets_with_expected_body(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["method"] = request.method
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "T-1"})

        client = _client(handler)
        result = await client.create_ticket(
            title="Volume audit: v1 (comp)",
            description="body",
            kind="task",
            source="volume-audit",
        )
        await client.close()

        assert result == {"id": "T-1"}
        assert seen["method"] == "POST"
        assert seen["url"] == "http://board.local/tickets"
        assert seen["auth"] == "Bearer tok"
        assert seen["body"] == {
            "title": "Volume audit: v1 (comp)",
            "description": "body",
            "source": "volume-audit",
            "kind": "task",
            "repo_id": "repo-1",
        }

    @pytest.mark.asyncio
    async def test_repo_id_defaults_to_configured_repo(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "T-2"})

        client = _client(handler, repo_id="fallback-repo")
        await client.create_ticket(title="t", description="d")
        await client.close()
        assert seen["body"]["repo_id"] == "fallback-repo"

    @pytest.mark.asyncio
    async def test_explicit_repo_id_wins(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "T-3"})

        client = _client(handler, repo_id="fallback-repo")
        await client.create_ticket(title="t", description="d", repo_id="other")
        await client.close()
        assert seen["body"]["repo_id"] == "other"

    @pytest.mark.asyncio
    async def test_defaults_match_the_previous_client(self):
        """``source="agent"`` / ``kind="task"`` came from board-agent's
        constants; a caller relying on them must not see a change."""
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={})

        client = _client(handler)
        await client.create_ticket(title="t", description="d")
        await client.close()
        assert seen["body"]["source"] == "agent"
        assert seen["body"]["kind"] == "task"


class TestAuthHeader:
    @pytest.mark.asyncio
    async def test_empty_token_sends_no_authorization_header(self):
        """A bare ``Bearer `` is rejected by httpx, and a loopback board
        needs no auth — so the header must be absent, not empty."""
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={})

        client = _client(handler, token="")
        await client.create_ticket(title="t", description="d")
        await client.close()
        assert seen["auth"] is None


class TestErrors:
    @pytest.mark.asyncio
    async def test_non_2xx_raises_with_detail(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"detail": "bad repo"})

        client = _client(handler)
        with pytest.raises(BoardAPIError) as exc:
            await client.create_ticket(title="t", description="d")
        await client.close()
        assert exc.value.status_code == 422
        assert "bad repo" in str(exc.value)

    @pytest.mark.asyncio
    async def test_non_json_error_body_falls_back_to_text(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="upstream exploded")

        client = _client(handler)
        with pytest.raises(BoardAPIError) as exc:
            await client.create_ticket(title="t", description="d")
        await client.close()
        assert "upstream exploded" in str(exc.value)


class TestClose:
    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        client = _client(handler)
        await client.create_ticket(title="t", description="d")
        await client.close()
        await client.close()  # must not raise

    @pytest.mark.asyncio
    async def test_close_before_any_request_is_safe(self):
        client = _client(lambda r: httpx.Response(200, json={}))
        await client.close()
