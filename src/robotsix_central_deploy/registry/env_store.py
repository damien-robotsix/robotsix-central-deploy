"""JSON-backed persistence for per-component environment variables and secrets.

Secrets are stored as Fernet ciphertext tokens; plaintext never touches disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ._store_utils import JsonFileStore
from .secret_key import SecretKeyManager


class ComponentEnvConfig(BaseModel):
    """Per-component stored environment and encrypted secret tokens."""

    env: dict[str, str] = {}
    secret_tokens: dict[str, str] = {}
    env_scopes: dict[str, str] = {}  # maps env key → scope tag (None/absent = private)
    secret_scopes: dict[
        str, str
    ] = {}  # maps secret key → scope tag (None/absent = private)


class EnvStore(JsonFileStore):
    """Persist user-supplied env overrides and encrypted secrets to a JSON file.

    Uses a read-modify-write pattern with an ``asyncio.Lock`` for writes,
    matching the pattern of ``FileStore`` in ``lifecycle/store.py``.
    """

    def __init__(self, store_path: Path, key_manager: SecretKeyManager) -> None:
        super().__init__(store_path)
        self._key_manager = key_manager

    async def get(self, name: str) -> ComponentEnvConfig:
        data = await self._load()
        entry = data.get(name)
        if entry is None:
            return ComponentEnvConfig()
        return ComponentEnvConfig.model_validate(entry)

    async def upsert(
        self,
        name: str,
        env: dict[str, str],
        secrets: dict[str, str],
        env_scopes: dict[str, str] | None = None,
        secret_scopes: dict[str, str] | None = None,
    ) -> None:
        """Merge *env*, *secrets*, and scope tags into the stored config for *name*.

        Overwrites matching keys; does not wipe keys not mentioned.
        Encrypts each secret value before storing.
        """

        def _mutate(data: dict[str, Any]) -> None:
            current = data.get(
                name,
                {
                    "env": {},
                    "secret_tokens": {},
                    "env_scopes": {},
                    "secret_scopes": {},
                },
            )
            current_env: dict[str, str] = dict(current.get("env", {}))
            current_tokens: dict[str, str] = dict(current.get("secret_tokens", {}))
            current_env_scopes: dict[str, str] = dict(current.get("env_scopes", {}))
            current_secret_scopes: dict[str, str] = dict(
                current.get("secret_scopes", {})
            )

            current_env.update(env)
            for key, plaintext in secrets.items():
                current_tokens[key] = self._key_manager.encrypt(plaintext)
            if env_scopes:
                current_env_scopes.update(env_scopes)
            if secret_scopes:
                current_secret_scopes.update(secret_scopes)

            data[name] = {
                "env": current_env,
                "secret_tokens": current_tokens,
                "env_scopes": current_env_scopes,
                "secret_scopes": current_secret_scopes,
            }

        await self._update(_mutate)

    async def delete_key(self, name: str, key: str) -> bool:
        """Remove *key* from env, secret_tokens, and scope maps.  Return True if found."""
        found = False

        def _mutate(data: dict[str, Any]) -> None:
            nonlocal found
            entry = data.get(name)
            if entry is None:
                return
            if key in entry.get("env", {}):
                del entry["env"][key]
                entry.get("env_scopes", {}).pop(key, None)
                found = True
            if key in entry.get("secret_tokens", {}):
                del entry["secret_tokens"][key]
                entry.get("secret_scopes", {}).pop(key, None)
                found = True
            if found:
                # Remove the component entry entirely if all dicts are empty
                if (
                    not entry.get("env")
                    and not entry.get("secret_tokens")
                    and not entry.get("env_scopes")
                    and not entry.get("secret_scopes")
                ):
                    del data[name]

        await self._update(_mutate)
        return found

    async def delete(self, name: str) -> None:
        """Remove all env and secrets for *name*. No-op if absent."""

        def _mutate(data: dict[str, Any]) -> None:
            data.pop(name, None)

        await self._update(_mutate)

    async def get_merged_env(
        self, name: str, base_env: dict[str, str]
    ) -> dict[str, str]:
        """Return the effective environment for *name*.

        Merging order (later wins):
        1. *base_env* (static YAML ``ComponentConfig.env``)
        2. Stored env overrides (user-supplied plaintext)
        3. Decrypted secrets

        Stored user values always override static YAML on key collision.
        """
        config = await self.get(name)
        merged: dict[str, str] = dict(base_env)
        merged.update(config.env)
        for key, token in config.secret_tokens.items():
            merged[key] = self._key_manager.decrypt(token)
        return merged

    @staticmethod
    def _scope_matches(pattern: str, candidate: str) -> bool:
        """Return True if *candidate* matches the colon-segmented glob *pattern*.

        Each segment of the pattern is compared to the candidate: ``*``
        matches any segment value; otherwise the segments must be equal.
        The candidate must have the same number of segments as the pattern.
        """
        pattern_parts = pattern.split(":")
        candidate_parts = candidate.split(":")
        if len(pattern_parts) != len(candidate_parts):
            return False
        for p, c in zip(pattern_parts, candidate_parts):
            if p != "*" and p != c:
                return False
        return True

    async def resolve_consumed_credentials(
        self, consumer_name: str, consumed_scopes: list[str]
    ) -> dict[str, str]:
        """Resolve credentials from all other components matching the consumer's scopes.

        Iterates every component in the store (except *consumer_name*),
        checks each scoped env/secret key against *consumed_scopes* glob
        patterns, and returns a merged dict of matching key→value pairs.
        Keys with no scope tag are never shared.
        """
        if not consumed_scopes:
            return {}

        data = await self._load()
        resolved: dict[str, str] = {}

        for name, entry in data.items():
            if name == consumer_name:
                continue
            entry_env = entry.get("env", {})
            entry_secrets = entry.get("secret_tokens", {})
            entry_env_scopes = entry.get("env_scopes", {})
            entry_secret_scopes = entry.get("secret_scopes", {})

            # Scoped env keys
            for key, scope in entry_env_scopes.items():
                if not scope:
                    continue
                if any(
                    self._scope_matches(pattern, scope) for pattern in consumed_scopes
                ):
                    resolved[key] = entry_env.get(key, "")

            # Scoped secret keys
            for key, scope in entry_secret_scopes.items():
                if not scope:
                    continue
                if any(
                    self._scope_matches(pattern, scope) for pattern in consumed_scopes
                ):
                    token = entry_secrets.get(key)
                    if token:
                        resolved[key] = self._key_manager.decrypt(token)

        return resolved
