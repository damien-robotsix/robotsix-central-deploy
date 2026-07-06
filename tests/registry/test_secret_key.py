"""Tests for ``SecretKeyManager`` — Fernet encryption key management."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken

from robotsix_central_deploy.registry.secret_key import SecretKeyManager


class TestSecretKeyManager:
    def test_generates_key_when_file_absent(self, tmp_path: Path):
        key_path = tmp_path / "subdir" / "secrets.key"
        SecretKeyManager(key_path)

        assert key_path.exists()
        # Key file should be writable only by owner
        assert key_path.stat().st_mode & 0o777 == 0o600

    def test_loads_existing_key_file(self, tmp_path: Path):
        key_path = tmp_path / "secrets.key"
        key = Fernet.generate_key()
        key_path.write_bytes(key)

        km = SecretKeyManager(key_path)

        # Encrypt then decrypt — same key should work
        token = km.encrypt("hello")
        assert km.decrypt(token) == "hello"

    def test_encrypt_decrypt_round_trip(self, tmp_path: Path):
        km = SecretKeyManager(tmp_path / "secrets.key")

        plaintext = "my-secret-value"
        token = km.encrypt(plaintext)
        assert token != plaintext
        assert km.decrypt(token) == plaintext

    def test_encrypt_decrypt_unicode(self, tmp_path: Path):
        km = SecretKeyManager(tmp_path / "secrets.key")

        plaintext = "secrets with üñîçödé 🔑"
        token = km.encrypt(plaintext)
        assert km.decrypt(token) == plaintext

    def test_decrypt_bad_token_raises_invalid_token(self, tmp_path: Path):
        km = SecretKeyManager(tmp_path / "secrets.key")

        with pytest.raises(InvalidToken):
            km.decrypt("not-a-valid-fernet-token")

    def test_decrypt_token_from_different_key_raises_invalid_token(
        self, tmp_path: Path
    ):
        km_a = SecretKeyManager(tmp_path / "key_a.key")
        km_b = SecretKeyManager(tmp_path / "key_b.key")

        token = km_a.encrypt("secret")
        with pytest.raises(InvalidToken):
            km_b.decrypt(token)

    def test_key_file_has_correct_permissions(self, tmp_path: Path):
        key_path = tmp_path / "secrets.key"
        SecretKeyManager(key_path)

        st_mode = key_path.stat().st_mode
        assert st_mode & 0o777 == 0o600

    def test_repeated_construction_loads_same_key(self, tmp_path: Path):
        key_path = tmp_path / "secrets.key"
        km1 = SecretKeyManager(key_path)

        token = km1.encrypt("payload")
        km2 = SecretKeyManager(key_path)
        assert km2.decrypt(token) == "payload"

    def test_empty_string_encrypt_decrypt(self, tmp_path: Path):
        km = SecretKeyManager(tmp_path / "secrets.key")

        token = km.encrypt("")
        assert km.decrypt(token) == ""
