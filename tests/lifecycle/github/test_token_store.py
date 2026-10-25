"""Unit tests for the TokenStore — issue, validate, revoke (per-token and per-user).

Uses a SecretKeyManager with a deterministic Fernet key so token
payloads are stable and testable.  Revocation data is persisted to
a temp path; each test gets a fresh store.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from cryptography.fernet import Fernet

from robotsix_central_deploy.lifecycle.token_store import TokenStore
from robotsix_central_deploy.registry.secret_key import SecretKeyManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_KEY = Fernet.generate_key()


def _key_manager(key_path: Path) -> SecretKeyManager:
    """Create a SecretKeyManager backed by a *deterministic* Fernet key."""
    key_path.write_bytes(_FIXED_KEY)
    return SecretKeyManager(key_path)


def _store(tmp_path: Path, ttl_days: int = 90) -> TokenStore:
    key_path = tmp_path / "secrets.key"
    km = _key_manager(key_path)
    rev_path = tmp_path / "revocations.json"
    return TokenStore(key_manager=km, revocation_path=rev_path, ttl_days=ttl_days)


# ---------------------------------------------------------------------------
# issue
# ---------------------------------------------------------------------------


class TestIssue:
    def test_returns_non_empty_encrypted_string(self, tmp_path):
        store = _store(tmp_path)
        token = store.issue("alice@example.com")
        assert isinstance(token, str)
        assert len(token) > 0

    def test_token_is_fernet_encrypted(self, tmp_path):
        store = _store(tmp_path)
        token = store.issue("alice@example.com")
        f = Fernet(_FIXED_KEY)
        plain = f.decrypt(token.encode("utf-8")).decode("utf-8")
        payload = json.loads(plain)
        assert isinstance(payload, dict)

    def test_token_contains_required_claims(self, tmp_path):
        store = _store(tmp_path)
        token = store.issue("bob@example.com")
        f = Fernet(_FIXED_KEY)
        plain = f.decrypt(token.encode("utf-8")).decode("utf-8")
        payload = json.loads(plain)
        assert payload["sub"] == "bob@example.com"
        assert payload["scope"] == "chat"
        assert "exp" in payload
        assert "iat" in payload
        assert "jti" in payload
        assert isinstance(payload["jti"], str)
        assert len(payload["jti"]) > 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_happy_path_returns_payload(self, tmp_path):
        store = _store(tmp_path)
        token = store.issue("carol@example.com")
        payload = store.validate(token)
        assert payload is not None
        assert payload["sub"] == "carol@example.com"
        assert payload["scope"] == "chat"
        assert payload["exp"] > time.time()

    def test_garbage_token_returns_none(self, tmp_path):
        store = _store(tmp_path)
        assert store.validate("not-a-valid-fernet-token") is None

    def test_empty_token_returns_none(self, tmp_path):
        store = _store(tmp_path)
        assert store.validate("") is None

    def test_expired_token_returns_none(self, tmp_path, monkeypatch):
        """Issue a token with a very short TTL, then advance the clock."""
        store = _store(tmp_path, ttl_days=0)  # expires immediately
        token = store.issue("dan@example.com")
        # Advance time past the expiry.
        future = time.time() + 60
        monkeypatch.setattr(time, "time", lambda: future)
        assert store.validate(token) is None

    def test_revoked_jti_returns_none(self, tmp_path):
        import asyncio

        store = _store(tmp_path)
        token = store.issue("eve@example.com")

        # Extract jti.
        f = Fernet(_FIXED_KEY)
        plain = f.decrypt(token.encode("utf-8")).decode("utf-8")
        jti = json.loads(plain)["jti"]

        asyncio.run(store.revoke_one(jti))
        assert store.validate(token) is None

    def test_user_revoked_with_earlier_iat_returns_none(self, tmp_path):
        import asyncio

        store = _store(tmp_path)
        token = store.issue("frank@example.com")

        # Revoke all tokens for frank.
        asyncio.run(store.revoke_user("frank@example.com"))
        assert store.validate(token) is None

    def test_user_revoked_with_later_iat_succeeds(self, tmp_path, monkeypatch):
        """Issue a token, travel back in time, revoke, then validate.

        The token's iat must be *after* the revocation timestamp to pass.
        """
        import asyncio

        store = _store(tmp_path)
        now = time.time()

        # Issue a token "now".
        token = store.issue("grace@example.com")

        # Travel back 60 seconds and revoke.
        monkeypatch.setattr(time, "time", lambda: now - 60)
        asyncio.run(store.revoke_user("grace@example.com"))

        # Restore real time for validation.
        monkeypatch.undo()
        # The token's iat is ~now, revocation timestamp is ~now-60 → token iat > revocation time → accepted.
        assert store.validate(token) is not None
        # But the revocation timestamp was set; a token with iat = now-120 would be rejected.

    def test_missing_required_claims_returns_none(self, tmp_path):
        """Encrypt a payload that is valid JSON but missing required keys."""
        store = _store(tmp_path)
        # Bypass TokenStore.issue — encrypt our own incomplete payload.
        incomplete = json.dumps({"sub": "hacker"})  # missing exp, iat, jti
        token = store._key_manager.encrypt(incomplete)
        assert store.validate(token) is None

    def test_non_json_payload_returns_none(self, tmp_path):
        """Encrypt a plain string that is not valid JSON."""
        store = _store(tmp_path)
        token = store._key_manager.encrypt("not-json")
        assert store.validate(token) is None

    def test_payload_with_extra_keys_still_validates(self, tmp_path):
        """Extra keys in the payload should not break validation."""
        store = _store(tmp_path)
        payload = json.dumps(
            {
                "sub": "ivan@example.com",
                "scope": "chat",
                "exp": time.time() + 3600,
                "iat": time.time(),
                "jti": "extra-test-jti",
                "custom": "value",
            }
        )
        token = store._key_manager.encrypt(payload)
        result = store.validate(token)
        assert result is not None
        assert result["custom"] == "value"


# ---------------------------------------------------------------------------
# revoke_one
# ---------------------------------------------------------------------------


class TestRevokeOne:
    def test_first_call_returns_true(self, tmp_path):
        import asyncio

        store = _store(tmp_path)
        token = store.issue("judy@example.com")
        f = Fernet(_FIXED_KEY)
        plain = f.decrypt(token.encode("utf-8")).decode("utf-8")
        jti = json.loads(plain)["jti"]

        assert asyncio.run(store.revoke_one(jti)) is True

    def test_second_call_returns_false_idempotent(self, tmp_path):
        import asyncio

        store = _store(tmp_path)
        token = store.issue("karl@example.com")
        f = Fernet(_FIXED_KEY)
        plain = f.decrypt(token.encode("utf-8")).decode("utf-8")
        jti = json.loads(plain)["jti"]

        asyncio.run(store.revoke_one(jti))
        assert asyncio.run(store.revoke_one(jti)) is False

    def test_persists_to_disk(self, tmp_path):
        import asyncio

        store = _store(tmp_path)
        token = store.issue("laura@example.com")
        f = Fernet(_FIXED_KEY)
        plain = f.decrypt(token.encode("utf-8")).decode("utf-8")
        jti = json.loads(plain)["jti"]

        asyncio.run(store.revoke_one(jti))

        # Create a *new* TokenStore from the same revocation file.
        key_path = tmp_path / "secrets.key"
        km = _key_manager(key_path)
        rev_path = tmp_path / "revocations.json"
        store2 = TokenStore(key_manager=km, revocation_path=rev_path)
        asyncio.run(store2.start())

        assert jti in store2._revoked_jtis
        assert store2.validate(token) is None


# ---------------------------------------------------------------------------
# revoke_user
# ---------------------------------------------------------------------------


class TestRevokeUser:
    def test_sets_user_revoke_timestamp(self, tmp_path):
        import asyncio

        store = _store(tmp_path)
        asyncio.run(store.revoke_user("mike@example.com"))
        assert "mike@example.com" in store._user_revoke_before
        assert store._user_revoke_before["mike@example.com"] > 0

    def test_persists_to_disk(self, tmp_path):
        import asyncio

        store = _store(tmp_path)
        asyncio.run(store.revoke_user("nora@example.com"))

        key_path = tmp_path / "secrets.key"
        km = _key_manager(key_path)
        rev_path = tmp_path / "revocations.json"
        store2 = TokenStore(key_manager=km, revocation_path=rev_path)
        asyncio.run(store2.start())

        assert "nora@example.com" in store2._user_revoke_before


# ---------------------------------------------------------------------------
# start / _load_locked
# ---------------------------------------------------------------------------


class TestStart:
    def test_loads_existing_revocation_file(self, tmp_path):
        import asyncio

        # Write a revocation file manually.
        rev_path = tmp_path / "revocations.json"
        rev_path.write_text(
            json.dumps(
                {
                    "revoked_jtis": {"jti-abc": True, "jti-def": True},
                    "user_revoke_before": {"olivia@example.com": 1700000000.0},
                }
            )
        )

        key_path = tmp_path / "secrets.key"
        km = _key_manager(key_path)
        store = TokenStore(key_manager=km, revocation_path=rev_path)
        asyncio.run(store.start())

        assert "jti-abc" in store._revoked_jtis
        assert "jti-def" in store._revoked_jtis
        assert store._user_revoke_before["olivia@example.com"] == 1700000000.0

    def test_handles_missing_file_gracefully(self, tmp_path):
        import asyncio

        store = _store(tmp_path)
        # revocations.json does not exist yet; start() should not raise.
        asyncio.run(store.start())
        assert store._revoked_jtis == set()
        assert store._user_revoke_before == {}

    def test_handles_corrupt_json_gracefully(self, tmp_path):
        import asyncio

        rev_path = tmp_path / "revocations.json"
        rev_path.write_text("not valid json {{{")

        key_path = tmp_path / "secrets.key"
        km = _key_manager(key_path)
        store = TokenStore(key_manager=km, revocation_path=rev_path)
        asyncio.run(store.start())

        # Treated as empty.
        assert store._revoked_jtis == set()
        assert store._user_revoke_before == {}
