"""Unit tests for the in-memory session store."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

from robotsix_central_deploy.lifecycle.session import SessionStore


class TestSessionStore:
    """SessionStore unit tests — no I/O, no fixtures needed."""

    def test_create_returns_unique_tokens(self) -> None:
        store = SessionStore()
        t1 = store.create()
        t2 = store.create()
        assert t1 != t2

    def test_create_and_validate(self) -> None:
        store = SessionStore()
        token = store.create()
        assert store.validate(token) is True

    def test_validate_nonexistent(self) -> None:
        store = SessionStore()
        assert store.validate("nonexistent-token") is False

    def test_validate_expired(self) -> None:
        store = SessionStore()
        token = store.create()

        # Fast-forward time past the 24h TTL
        future = time.time() + 86401.0
        with patch.object(time, "time", return_value=future):
            assert store.validate(token) is False

    def test_expired_token_is_evicted_on_validate(self) -> None:
        store = SessionStore()
        token = store.create()

        future = time.time() + 86401.0
        with patch.object(time, "time", return_value=future):
            store.validate(token)

        # After eviction, validate (with real time) should still be False
        assert store.validate(token) is False

    def test_delete_invalidates_token(self) -> None:
        store = SessionStore()
        token = store.create()
        assert store.validate(token) is True
        store.delete(token)
        assert store.validate(token) is False

    def test_delete_nonexistent_does_not_raise(self) -> None:
        store = SessionStore()
        store.delete("nonexistent-token")  # should not raise

    def test_concurrent_create_validate_no_corruption(self) -> None:
        store = SessionStore()
        results: list[str] = []
        errors: list[Exception] = []

        def make_and_check() -> None:
            try:
                for _ in range(100):
                    t = store.create()
                    results.append(t)
                    assert store.validate(t) is True
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=make_and_check) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # All tokens should be unique
        assert len(results) == len(set(results)) == 1000
