"""Integration tests for the mobile token exchange and validation endpoints.

Covers GET /auth/token, GET /auth/validate, DELETE /auth/token/{token_id},
and POST /auth/revoke-user.  Uses a real TokenStore wired into app.state
so the ForwardAuth path (verify_bearer_token) hits the real validation
logic.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.token_store import TokenStore
from robotsix_central_deploy.registry.secret_key import SecretKeyManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_KEY = Fernet.generate_key()


def _key_manager(key_path: Path) -> SecretKeyManager:
    key_path.write_bytes(_FIXED_KEY)
    return SecretKeyManager(key_path)


def _make_token_store(tmp_path: Path, ttl_days: int = 90) -> TokenStore:
    key_path = tmp_path / "secrets.key"
    km = _key_manager(key_path)
    rev_path = tmp_path / "revocations.json"
    return TokenStore(key_manager=km, revocation_path=rev_path, ttl_days=ttl_days)


@pytest.fixture
def wired_store(tmp_path, monkeypatch):
    """Create a real TokenStore and wire it into app.state.token_store."""
    store = _make_token_store(tmp_path)
    server_mod.app.state.token_store = store
    return store


# ---------------------------------------------------------------------------
# GET /auth/token — exchange_token
# ---------------------------------------------------------------------------


class TestExchangeToken:
    async def test_happy_path_returns_token_response(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get("/auth/token", headers={"Remote-User": "alice@ex.com"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "access_token" in body
        assert body["token_type"] == "Bearer"
        assert body["scope"] == "chat"
        assert body["expires_in"] > 0
        # The access_token should be valid.
        payload = wired_store.validate(body["access_token"])
        assert payload is not None
        assert payload["sub"] == "alice@ex.com"

    async def test_validation_fails_after_issue_returns_500(
        self, client: AsyncClient, wired_store: TokenStore, monkeypatch
    ):
        """When issue succeeds but validate returns None, the endpoint returns 500."""
        monkeypatch.setattr(wired_store, "validate", lambda _token: None)
        resp = await client.get("/auth/token", headers={"Remote-User": "zoe@ex.com"})
        assert resp.status_code == 500, resp.text

    async def test_missing_remote_user_header_returns_401(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get("/auth/token")
        assert resp.status_code == 401, resp.text
        assert "Remote-User" in resp.json()["error"]

    async def test_empty_remote_user_header_returns_401(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get("/auth/token", headers={"Remote-User": ""})
        assert resp.status_code == 401, resp.text

    async def test_whitespace_only_remote_user_returns_401(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get("/auth/token", headers={"Remote-User": "   "})
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# GET /auth/validate — validate_token (ForwardAuth)
# ---------------------------------------------------------------------------


class TestValidateToken:
    async def test_valid_token_returns_200_with_remote_user_header(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        token = wired_store.issue("bob@ex.com")
        resp = await client.get(
            "/auth/validate",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("Remote-User") == "bob@ex.com"

    async def test_missing_authorization_header_returns_401(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get("/auth/validate")
        assert resp.status_code == 401, resp.text

    async def test_non_bearer_authorization_returns_401(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get(
            "/auth/validate",
            headers={"Authorization": "Basic dGVzdDp0ZXN0"},
        )
        assert resp.status_code == 401, resp.text

    async def test_empty_bearer_token_returns_401(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get(
            "/auth/validate",
            headers={"Authorization": "Bearer "},
        )
        assert resp.status_code == 401, resp.text

    async def test_invalid_token_returns_401(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get(
            "/auth/validate",
            headers={"Authorization": "Bearer not-a-valid-token"},
        )
        assert resp.status_code == 401, resp.text

    async def test_expired_token_returns_401(
        self, client: AsyncClient, wired_store: TokenStore, monkeypatch
    ):
        import robotsix_central_deploy.lifecycle.token_store as ts_mod

        store = _make_token_store(Path("/tmp"), ttl_days=0)
        token = store.issue("carol@ex.com")
        server_mod.app.state.token_store = store

        # Advance time.
        future = time.time() + 60
        monkeypatch.setattr(ts_mod.time, "time", lambda: future)

        resp = await client.get(
            "/auth/validate",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401, resp.text

    async def test_no_token_store_returns_401(self, client: AsyncClient):
        server_mod.app.state.token_store = None
        resp = await client.get(
            "/auth/validate",
            headers={"Authorization": "Bearer some-token"},
        )
        assert resp.status_code == 401, resp.text

    async def test_post_method_also_works(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        """The ForwardAuth endpoint accepts any HTTP method."""
        token = wired_store.issue("dave@ex.com")
        resp = await client.post(
            "/auth/validate",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("Remote-User") == "dave@ex.com"


# ---------------------------------------------------------------------------
# DELETE /auth/token/{token_id} — revoke_token
# ---------------------------------------------------------------------------


class TestRevokeToken:
    async def test_revoke_existing_token_returns_204(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        token = wired_store.issue("eve@ex.com")
        f = Fernet(_FIXED_KEY)
        plain = f.decrypt(token.encode("utf-8")).decode("utf-8")
        jti = json.loads(plain)["jti"]

        resp = await client.delete(f"/auth/token/{jti}")
        assert resp.status_code == 204, resp.text
        # Verify the token is actually revoked.
        assert wired_store.validate(token) is None

    async def test_revoke_already_revoked_token_returns_404(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        token = wired_store.issue("frank@ex.com")
        f = Fernet(_FIXED_KEY)
        plain = f.decrypt(token.encode("utf-8")).decode("utf-8")
        jti = json.loads(plain)["jti"]

        await client.delete(f"/auth/token/{jti}")
        resp = await client.delete(f"/auth/token/{jti}")
        assert resp.status_code == 404, resp.text
        assert "already revoked" in resp.json()["error"].lower()

    async def test_revoke_unknown_jti_returns_204(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        """A non-existent jti is simply added to the revocation set — 204."""
        resp = await client.delete("/auth/token/nonexistent-jti")
        assert resp.status_code == 204, resp.text


# ---------------------------------------------------------------------------
# POST /auth/revoke-user — revoke_user_tokens
# ---------------------------------------------------------------------------


class TestRevokeUserTokens:
    async def test_revoke_user_returns_204(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        # Issue a token *before* revoking the user.
        token = wired_store.issue("grace@ex.com")
        assert wired_store.validate(token) is not None  # valid before revoke

        resp = await client.post(
            "/auth/revoke-user",
            json={"user": "grace@ex.com"},
        )
        assert resp.status_code == 204, resp.text

        # The pre-revoke token is now rejected.
        assert wired_store.validate(token) is None

    async def test_revoke_user_missing_body_field_returns_422(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.post(
            "/auth/revoke-user",
            json={},
        )
        assert resp.status_code == 422, resp.text

    async def test_revoke_user_tokens_issued_after_are_unaffected(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        # Revoke first.
        await client.post(
            "/auth/revoke-user",
            json={"user": "henry@ex.com"},
        )
        # Now issue a new token — its iat > revoke timestamp.
        token = wired_store.issue("henry@ex.com")
        assert wired_store.validate(token) is not None
