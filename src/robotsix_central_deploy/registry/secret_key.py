"""Fernet-based encryption for component secrets.

Loads or generates a symmetric encryption key from a file on disk.
If the key file is lost, Fernet tokens become unrecoverable; secrets
must be re-entered via the API.

``SecretKeyManager`` is safe to construct at startup and never blocks.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet


class SecretKeyManager:
    """Load a Fernet key file; generate and persist a new one if absent."""

    def __init__(self, key_path: Path) -> None:
        self._key_path = key_path
        if key_path.exists():
            key = key_path.read_bytes()
        else:
            key = Fernet.generate_key()
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_bytes(key)
            key_path.chmod(0o600)
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        """Return a Fernet token (URL-safe base64 string) for *plaintext*."""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        """Decrypt a Fernet token back to plaintext.

        Raises ``cryptography.fernet.InvalidToken`` on bad input.
        """
        return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
