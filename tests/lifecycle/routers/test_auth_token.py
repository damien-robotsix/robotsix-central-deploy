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


# ---------------------------------------------------------------------------
# GET /auth/login + /auth/login/complete — mobile deep-link login handoff
# ---------------------------------------------------------------------------

_DEEP_LINK = "robotsixchat://auth/callback"


class TestMobileLoginHandoff:
    async def test_start_sets_cookie_and_redirects_to_complete(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get("/auth/login", params={"redirect_to": _DEEP_LINK})
        assert resp.status_code == 303, resp.text
        assert resp.headers["location"] == "/auth/login/complete"
        set_cookie = resp.headers["set-cookie"]
        assert "robotsix_login_redirect=" in set_cookie
        assert "Path=/auth" in set_cookie
        assert "HttpOnly" in set_cookie

    async def test_start_rejects_web_scheme(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get(
            "/auth/login", params={"redirect_to": "https://evil.example/steal"}
        )
        assert resp.status_code == 400, resp.text

    async def test_start_rejects_control_characters(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get(
            "/auth/login",
            params={"redirect_to": "robotsixchat://auth/callback\r\nSet-Cookie: x"},
        )
        assert resp.status_code == 400, resp.text

    async def test_start_requires_redirect_to(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get("/auth/login")
        assert resp.status_code == 422, resp.text

    async def test_complete_without_remote_user_returns_401(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get(
            "/auth/login/complete",
            cookies={"robotsix_login_redirect": _DEEP_LINK},
        )
        assert resp.status_code == 401, resp.text

    async def test_complete_without_cookie_returns_400(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get(
            "/auth/login/complete", headers={"Remote-User": "dana@ex.com"}
        )
        assert resp.status_code == 400, resp.text

    async def test_complete_rejects_tampered_cookie_scheme(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get(
            "/auth/login/complete",
            headers={"Remote-User": "dana@ex.com"},
            cookies={"robotsix_login_redirect": "https://evil.example/steal"},
        )
        assert resp.status_code == 400, resp.text

    async def test_full_handoff_issues_valid_token(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        start = await client.get("/auth/login", params={"redirect_to": _DEEP_LINK})
        assert start.status_code == 303
        # Carry the handoff cookie across the (simulated) SSO round-trip.
        cookie_value = start.cookies["robotsix_login_redirect"]

        complete = await client.get(
            "/auth/login/complete",
            headers={"Remote-User": "dana@ex.com"},
            cookies={"robotsix_login_redirect": cookie_value},
        )
        assert complete.status_code == 302, complete.text
        location = complete.headers["location"]
        assert location.startswith(f"{_DEEP_LINK}?token="), location

        from urllib.parse import parse_qs, urlsplit

        token = parse_qs(urlsplit(location).query)["token"][0]
        payload = wired_store.validate(token)
        assert payload is not None
        assert payload["sub"] == "dana@ex.com"

        # The handoff cookie is cleared on completion.
        cleared = complete.headers["set-cookie"]
        assert "robotsix_login_redirect=" in cleared
        assert "Max-Age=0" in cleared or "expires" in cleared.lower()

    async def test_complete_appends_with_ampersand_when_target_has_query(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.get(
            "/auth/login/complete",
            headers={"Remote-User": "dana@ex.com"},
            cookies={"robotsix_login_redirect": "robotsixchat://auth/callback?src=sso"},
        )
        assert resp.status_code == 302
        assert "?src=sso&token=" in resp.headers["location"]


# ---------------------------------------------------------------------------
# POST /chat/auth/mobile-token — mobile app token exchange
# ---------------------------------------------------------------------------


class TestExchangeMobileToken:
    async def test_valid_token_is_echoed_with_expiry(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        token = wired_store.issue("erin@ex.com")
        resp = await client.post("/chat/auth/mobile-token", json={"token": token})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["access_token"] == token
        assert body["token_type"] == "Bearer"
        assert body["scope"] == "chat"
        assert 0 < body["expires_in"] <= 90 * 86400

    async def test_garbage_token_returns_401(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.post(
            "/chat/auth/mobile-token", json={"token": "not-a-token"}
        )
        assert resp.status_code == 401, resp.text

    async def test_revoked_token_returns_401(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        token = wired_store.issue("frank@ex.com")
        payload = wired_store.validate(token)
        assert payload is not None
        await wired_store.revoke_one(payload["jti"])

        resp = await client.post("/chat/auth/mobile-token", json={"token": token})
        assert resp.status_code == 401, resp.text

    async def test_missing_body_field_returns_422(
        self, client: AsyncClient, wired_store: TokenStore
    ):
        resp = await client.post("/chat/auth/mobile-token", json={})
        assert resp.status_code == 422, resp.text
