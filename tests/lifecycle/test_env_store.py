"""Tests for SecretKeyManager and EnvStore."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import InvalidToken

from robotsix_central_deploy.registry.env_store import EnvStore
from robotsix_central_deploy.registry.secret_key import SecretKeyManager


# ---------------------------------------------------------------------------
# SecretKeyManager
# ---------------------------------------------------------------------------


class TestSecretKeyManager:
    def test_generates_key_on_first_run(self, tmp_path: Path):
        key_path = tmp_path / "secrets.key"
        assert not key_path.exists()
        SecretKeyManager(key_path)
        assert key_path.exists()
        # Verify permissions
        stat = key_path.stat()
        assert stat.st_mode & 0o777 == 0o600

    def test_loads_existing_key(self, tmp_path: Path):
        key_path = tmp_path / "secrets.key"
        km1 = SecretKeyManager(key_path)
        token = km1.encrypt("hello")
        # Construct a second instance — should load the same key
        km2 = SecretKeyManager(key_path)
        assert km2.decrypt(token) == "hello"

    def test_encrypt_decrypt_roundtrip(self, tmp_path: Path):
        km = SecretKeyManager(tmp_path / "secrets.key")
        plain = "my-secret-value"
        token = km.encrypt(plain)
        assert token != plain
        assert km.decrypt(token) == plain

    def test_decrypt_wrong_token_raises(self, tmp_path: Path):
        km = SecretKeyManager(tmp_path / "secrets.key")
        with pytest.raises(InvalidToken):
            km.decrypt("not-a-valid-token")

    def test_different_keys_produce_different_outputs(self, tmp_path: Path):
        km_a = SecretKeyManager(tmp_path / "key_a.key")
        km_b = SecretKeyManager(tmp_path / "key_b.key")
        token = km_a.encrypt("secret")
        with pytest.raises(InvalidToken):
            km_b.decrypt(token)


# ---------------------------------------------------------------------------
# EnvStore
# ---------------------------------------------------------------------------


@pytest.fixture
def key_manager(tmp_path: Path) -> SecretKeyManager:
    return SecretKeyManager(tmp_path / "secrets.key")


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "env.json"


class TestEnvStoreUpsertAndGet:
    async def test_get_empty_store_returns_default(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        config = await store.get("chat")
        assert config.env == {}
        assert config.secret_tokens == {}

    async def test_upsert_then_get_preserves_env(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"LOG_LEVEL": "debug"}, {})
        config = await store.get("chat")
        assert config.env == {"LOG_LEVEL": "debug"}
        assert config.secret_tokens == {}

    async def test_upsert_merges_not_replaces(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"A": "1"}, {})
        await store.upsert("chat", {"B": "2"}, {})
        config = await store.get("chat")
        assert config.env == {"A": "1", "B": "2"}

    async def test_upsert_overwrites_existing_keys(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"A": "1"}, {})
        await store.upsert("chat", {"A": "new"}, {})
        config = await store.get("chat")
        assert config.env == {"A": "new"}

    async def test_stored_json_does_not_contain_plaintext(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {}, {"API_KEY": "super-secret"})
        raw_text = store_path.read_text()
        assert "super-secret" not in raw_text

    async def test_stored_secret_is_fernet_token(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {}, {"API_KEY": "s3cret"})
        config = await store.get("chat")
        token = config.secret_tokens["API_KEY"]
        # Token should be decryptable
        assert key_manager.decrypt(token) == "s3cret"

    async def test_upsert_preserves_other_components(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"A": "1"}, {})
        await store.upsert("mail", {"B": "2"}, {})
        assert (await store.get("chat")).env == {"A": "1"}
        assert (await store.get("mail")).env == {"B": "2"}


class TestEnvStoreGetMergedEnv:
    async def test_merged_base_only(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        merged = await store.get_merged_env("chat", {"DEFAULT": "val"})
        assert merged == {"DEFAULT": "val"}

    async def test_merged_stored_overrides_base(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"KEY": "user-val"}, {})
        merged = await store.get_merged_env("chat", {"KEY": "base-val"})
        assert merged == {"KEY": "user-val"}

    async def test_merged_secrets_overrides_base(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {}, {"TOKEN": "my-token"})
        merged = await store.get_merged_env("chat", {"TOKEN": "base-token"})
        assert merged == {"TOKEN": "my-token"}

    async def test_merged_env_overrides_secret_on_collision(self, store_path: Path, key_manager: SecretKeyManager):
        """Secrets are applied last, so they win over stored plaintext env on collision."""
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"KEY": "env-val"}, {"KEY": "secret-val"})
        merged = await store.get_merged_env("chat", {"KEY": "base-val"})
        # Secret applied last wins over stored env
        assert merged == {"KEY": "secret-val"}

    async def test_merged_all_layers(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        await store.upsert(
            "chat",
            {"URL": "env-url", "SHARED": "env-shared"},
            {"SECRET": "secret-val", "SHARED": "secret-wins"},
        )
        merged = await store.get_merged_env("chat", {"URL": "base-url", "SHARED": "base-shared"})
        assert merged == {
            "URL": "env-url",
            "SHARED": "secret-wins",
            "SECRET": "secret-val",
        }


class TestEnvStoreDeleteKey:
    async def test_delete_existing_env_key(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"A": "1", "B": "2"}, {})
        assert await store.delete_key("chat", "A") is True
        config = await store.get("chat")
        assert config.env == {"B": "2"}

    async def test_delete_existing_secret_key(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {}, {"TOKEN": "val"})
        assert await store.delete_key("chat", "TOKEN") is True
        config = await store.get("chat")
        assert config.secret_tokens == {}

    async def test_delete_absent_key_returns_false(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"A": "1"}, {})
        assert await store.delete_key("chat", "B") is False

    async def test_delete_absent_component_returns_false(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        assert await store.delete_key("nonexistent", "KEY") is False

    async def test_delete_removes_component_entry_when_empty(self, store_path: Path, key_manager: SecretKeyManager):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"A": "1"}, {})
        await store.delete_key("chat", "A")
        # After removing the only key, the component entry should be gone
        data = await store._load()
        assert "chat" not in data


class TestEnvStoreSurvivesRestart:
    async def test_data_survives_new_instance(self, store_path: Path, key_manager: SecretKeyManager):
        store1 = EnvStore(store_path, key_manager)
        await store1.upsert("chat", {"A": "1"}, {"S": "val"})

        # Simulate restart: new EnvStore from same path + same key
        store2 = EnvStore(store_path, key_manager)
        config = await store2.get("chat")
        assert config.env == {"A": "1"}
        assert len(config.secret_tokens) == 1
        assert "S" in config.secret_tokens

        merged = await store2.get_merged_env("chat", {})
        assert merged == {"A": "1", "S": "val"}
