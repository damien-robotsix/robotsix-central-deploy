"""In-memory session store for browser-session authentication."""
from __future__ import annotations
import secrets
import time
from threading import Lock

_SESSION_TTL: float = 86400.0  # 24 hours


class SessionStore:
    """Maps opaque session tokens to creation timestamps."""

    def __init__(self) -> None:
        self._tokens: dict[str, float] = {}
        self._lock = Lock()

    def create(self) -> str:
        """Mint a new session token, store it, return it."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._cleanup_unlocked()
            self._tokens[token] = time.time()
        return token

    def validate(self, token: str) -> bool:
        """Return True iff token exists and has not expired."""
        with self._lock:
            created = self._tokens.get(token)
            if created is None:
                return False
            if time.time() - created > _SESSION_TTL:
                del self._tokens[token]
                return False
            return True

    def delete(self, token: str) -> None:
        """Invalidate a specific token (logout)."""
        with self._lock:
            self._tokens.pop(token, None)

    def _cleanup_unlocked(self) -> None:
        """Evict all expired tokens. Must be called under self._lock."""
        now = time.time()
        expired = [t for t, ts in self._tokens.items() if now - ts > _SESSION_TTL]
        for t in expired:
            del self._tokens[t]
