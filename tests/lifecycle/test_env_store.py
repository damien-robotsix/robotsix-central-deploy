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
    async def test_get_empty_store_returns_default(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        config = await store.get("chat")
        assert config.env == {}
        assert config.secret_tokens == {}

    async def test_upsert_then_get_preserves_env(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"LOG_LEVEL": "debug"}, {})
        config = await store.get("chat")
        assert config.env == {"LOG_LEVEL": "debug"}
        assert config.secret_tokens == {}

    async def test_upsert_merges_not_replaces(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"A": "1"}, {})
        await store.upsert("chat", {"B": "2"}, {})
        config = await store.get("chat")
        assert config.env == {"A": "1", "B": "2"}

    async def test_upsert_overwrites_existing_keys(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"A": "1"}, {})
        await store.upsert("chat", {"A": "new"}, {})
        config = await store.get("chat")
        assert config.env == {"A": "new"}

    async def test_stored_json_does_not_contain_plaintext(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {}, {"API_KEY": "super-secret"})
        raw_text = store_path.read_text()
        assert "super-secret" not in raw_text

    async def test_stored_secret_is_fernet_token(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {}, {"API_KEY": "s3cret"})
        config = await store.get("chat")
        token = config.secret_tokens["API_KEY"]
        # Token should be decryptable
        assert key_manager.decrypt(token) == "s3cret"

    async def test_upsert_preserves_other_components(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"A": "1"}, {})
        await store.upsert("mail", {"B": "2"}, {})
        assert (await store.get("chat")).env == {"A": "1"}
        assert (await store.get("mail")).env == {"B": "2"}


class TestEnvStoreGetMergedEnv:
    async def test_merged_base_only(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        merged = await store.get_merged_env("chat", {"DEFAULT": "val"})
        assert merged == {"DEFAULT": "val"}

    async def test_merged_stored_overrides_base(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"KEY": "user-val"}, {})
        merged = await store.get_merged_env("chat", {"KEY": "base-val"})
        assert merged == {"KEY": "user-val"}

    async def test_merged_secrets_overrides_base(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {}, {"TOKEN": "my-token"})
        merged = await store.get_merged_env("chat", {"TOKEN": "base-token"})
        assert merged == {"TOKEN": "my-token"}

    async def test_merged_env_overrides_secret_on_collision(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        """Secrets are applied last, so they win over stored plaintext env on collision."""
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"KEY": "env-val"}, {"KEY": "secret-val"})
        merged = await store.get_merged_env("chat", {"KEY": "base-val"})
        # Secret applied last wins over stored env
        assert merged == {"KEY": "secret-val"}

    async def test_merged_all_layers(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        await store.upsert(
            "chat",
            {"URL": "env-url", "SHARED": "env-shared"},
            {"SECRET": "secret-val", "SHARED": "secret-wins"},
        )
        merged = await store.get_merged_env(
            "chat", {"URL": "base-url", "SHARED": "base-shared"}
        )
        assert merged == {
            "URL": "env-url",
            "SHARED": "secret-wins",
            "SECRET": "secret-val",
        }


class TestEnvStoreDeleteKey:
    async def test_delete_existing_env_key(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"A": "1", "B": "2"}, {})
        assert await store.delete_key("chat", "A") is True
        config = await store.get("chat")
        assert config.env == {"B": "2"}

    async def test_delete_existing_secret_key(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {}, {"TOKEN": "val"})
        assert await store.delete_key("chat", "TOKEN") is True
        config = await store.get("chat")
        assert config.secret_tokens == {}

    async def test_delete_absent_key_returns_false(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"A": "1"}, {})
        assert await store.delete_key("chat", "B") is False

    async def test_delete_absent_component_returns_false(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        assert await store.delete_key("nonexistent", "KEY") is False

    async def test_delete_removes_component_entry_when_empty(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
        store = EnvStore(store_path, key_manager)
        await store.upsert("chat", {"A": "1"}, {})
        await store.delete_key("chat", "A")
        # After removing the only key, the component entry should be gone
        data = await store._load()
        assert "chat" not in data


class TestEnvStoreSurvivesRestart:
    async def test_data_survives_new_instance(
        self, store_path: Path, key_manager: SecretKeyManager
    ):
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


# ---------------------------------------------------------------------------
# Scope matching
# ---------------------------------------------------------------------------


class TestScopeMatches:
    def test_exact_match(self):
        from robotsix_central_deploy.registry.env_store import EnvStore

        assert EnvStore._scope_matches("website:ovh", "website:ovh") is True

    def test_wildcard_matches_everything(self):
        from robotsix_central_deploy.registry.env_store import EnvStore

        assert EnvStore._scope_matches("*", "website:ovh") is True
        assert EnvStore._scope_matches("*", "anything") is True

    def test_single_segment_wildcard(self):
        from robotsix_central_deploy.registry.env_store import EnvStore

        assert EnvStore._scope_matches("*:ovh", "website:ovh") is True
        assert EnvStore._scope_matches("*:ovh", "anything:ovh") is True
        assert EnvStore._scope_matches("*:ovh", "website:other") is False

    def test_double_star_glob(self):
        from robotsix_central_deploy.registry.env_store import EnvStore

        assert EnvStore._scope_matches("**:ovh", "website:ovh") is True
        assert EnvStore._scope_matches("**:ovh", "a:b:ovh") is True
        assert EnvStore._scope_matches("**:ovh", "ovh") is True
        assert EnvStore._scope_matches("**:ovh", "other") is False

    def test_mismatched_lengths(self):
        from robotsix_central_deploy.registry.env_store import EnvStore

        assert EnvStore._scope_matches("a:b", "a") is False
        assert EnvStore._scope_matches("a", "a:b") is False

    def test_segment_mismatch(self):
        from robotsix_central_deploy.registry.env_store import EnvStore

        assert EnvStore._scope_matches("website:ovh", "website:other") is False
        assert (
            EnvStore._scope_matches("service:deploy", "service:deploy-server") is False
        )


# ---------------------------------------------------------------------------
# resolve_consumed_credentials
# ---------------------------------------------------------------------------


class TestResolveConsumedCredentials:
    async def test_empty_scopes_returns_empty(self, store_path, key_manager):
        store = EnvStore(store_path, key_manager)
        result = await store.resolve_consumed_credentials("consumer", [])
        assert result == {}

    async def test_resolves_scoped_env_vars(self, store_path, key_manager):
        store = EnvStore(store_path, key_manager)
        await store.upsert(
            "cred-provider",
            env={"HOST": "example.com", "PORT": "22"},
            secrets={},
            env_scopes={"HOST": "website:ovh", "PORT": "website:ovh"},
        )
        result = await store.resolve_consumed_credentials(
            "consumer", ["website:ovh"]
        )
        assert result == {"HOST": "example.com", "PORT": "22"}

    async def test_resolves_scoped_secrets(self, store_path, key_manager):
        store = EnvStore(store_path, key_manager)
        await store.upsert(
            "cred-provider",
            env={},
            secrets={"PASSWORD": "s3cret"},
            secret_scopes={"PASSWORD": "website:ovh"},
        )
        result = await store.resolve_consumed_credentials(
            "consumer", ["website:ovh"]
        )
        assert result == {"PASSWORD": "s3cret"}

    async def test_resolves_mixed_env_and_secrets(self, store_path, key_manager):
        store = EnvStore(store_path, key_manager)
        await store.upsert(
            "cred-provider",
            env={"HOST": "example.com"},
            secrets={"PASSWORD": "s3cret"},
            env_scopes={"HOST": "website:ovh"},
            secret_scopes={"PASSWORD": "website:ovh"},
        )
        result = await store.resolve_consumed_credentials(
            "consumer", ["website:ovh"]
        )
        assert result == {"HOST": "example.com", "PASSWORD": "s3cret"}

    async def test_only_matches_requested_scopes(self, store_path, key_manager):
        store = EnvStore(store_path, key_manager)
        await store.upsert(
            "cred-provider",
            env={"HOST": "example.com", "OTHER": "ignored"},
            secrets={},
            env_scopes={"HOST": "website:ovh", "OTHER": "other:scope"},
        )
        result = await store.resolve_consumed_credentials(
            "consumer", ["website:ovh"]
        )
        assert result == {"HOST": "example.com"}
        assert "OTHER" not in result

    async def test_wildcard_scope_matches(self, store_path, key_manager):
        store = EnvStore(store_path, key_manager)
        await store.upsert(
            "cred-provider",
            env={"HOST": "example.com"},
            secrets={},
            env_scopes={"HOST": "website:ovh"},
        )
        result = await store.resolve_consumed_credentials("consumer", ["*"])
        assert result == {"HOST": "example.com"}

    async def test_unscoped_keys_not_returned(self, store_path, key_manager):
        store = EnvStore(store_path, key_manager)
        await store.upsert(
            "cred-provider",
            env={"HOST": "example.com", "UNSCOPED": "val"},
            secrets={},
            env_scopes={"HOST": "website:ovh"},
            # UNSCOPED has no scope tag
        )
        result = await store.resolve_consumed_credentials(
            "consumer", ["website:ovh"]
        )
        assert "UNSCOPED" not in result

    async def test_multiple_providers_aggregated(self, store_path, key_manager):
        store = EnvStore(store_path, key_manager)
        await store.upsert(
            "provider-a",
            env={"KEY_A": "val_a"},
            secrets={},
            env_scopes={"KEY_A": "website:ovh"},
        )
        await store.upsert(
            "provider-b",
            env={"KEY_B": "val_b"},
            secrets={},
            env_scopes={"KEY_B": "website:ovh"},
        )
        result = await store.resolve_consumed_credentials(
            "consumer", ["website:ovh"]
        )
        assert result == {"KEY_A": "val_a", "KEY_B": "val_b"}

    async def test_secret_plaintext_not_in_stored_json(self, store_path, key_manager):
        store = EnvStore(store_path, key_manager)
        await store.upsert(
            "cred-provider",
            env={},
            secrets={"PASSWORD": "super-secret"},
            secret_scopes={"PASSWORD": "website:ovh"},
        )
        raw = store_path.read_text()
        assert "super-secret" not in raw


# ---------------------------------------------------------------------------
# upsert with scope tags
# ---------------------------------------------------------------------------


class TestEnvStoreUpsertScopes:
    async def test_upsert_stores_env_scopes(self, store_path, key_manager):
        store = EnvStore(store_path, key_manager)
        await store.upsert(
            "svc",
            env={"HOST": "x"},
            secrets={},
            env_scopes={"HOST": "website:ovh"},
        )
        config = await store.get("svc")
        assert config.env_scopes == {"HOST": "website:ovh"}

    async def test_upsert_merges_env_scopes(self, store_path, key_manager):
        store = EnvStore(store_path, key_manager)
        await store.upsert(
            "svc",
            env={"A": "1"},
            secrets={},
            env_scopes={"A": "scope:a"},
        )
        await store.upsert(
            "svc",
            env={"B": "2"},
            secrets={},
            env_scopes={"B": "scope:b"},
        )
        config = await store.get("svc")
        assert config.env_scopes == {"A": "scope:a", "B": "scope:b"}

    async def test_upsert_stores_secret_scopes(self, store_path, key_manager):
        store = EnvStore(store_path, key_manager)
        await store.upsert(
            "svc",
            env={},
            secrets={"PWD": "val"},
            secret_scopes={"PWD": "website:ovh"},
        )
        config = await store.get("svc")
        assert config.secret_scopes == {"PWD": "website:ovh"}

    async def test_delete_key_removes_scope_entries(self, store_path, key_manager):
        store = EnvStore(store_path, key_manager)
        await store.upsert(
            "svc",
            env={"A": "1"},
            secrets={"S": "val"},
            env_scopes={"A": "scope:a"},
            secret_scopes={"S": "scope:s"},
        )
        assert await store.delete_key("svc", "A") is True
        config = await store.get("svc")
        assert "A" not in config.env_scopes
        # "S" should still be there
        assert "S" in config.secret_scopes
