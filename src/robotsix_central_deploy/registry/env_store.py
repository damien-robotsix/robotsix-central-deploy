"""JSON-backed persistence for per-component environment variables and secrets.

Secrets are stored as Fernet ciphertext tokens; plaintext never touches disk.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .secret_key import SecretKeyManager


class ComponentEnvConfig(BaseModel):
    """Per-component stored environment and encrypted secret tokens."""

    env: dict[str, str] = {}
    secret_tokens: dict[str, str] = {}


class EnvStore:
    """Persist user-supplied env overrides and encrypted secrets to a JSON file.

    Uses a read-modify-write pattern with an ``asyncio.Lock`` for writes,
    matching the pattern of ``FileStore`` in ``lifecycle/store.py``.
    """

    def __init__(self, store_path: Path, key_manager: SecretKeyManager) -> None:
        self._path = store_path
        self._key_manager = key_manager
        self._lock = asyncio.Lock()

    async def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        raw = self._path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data: dict[str, Any] = json.loads(raw)
        return data

    async def _save(self, data: dict[str, Any]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.rename(self._path)

    async def get(self, name: str) -> ComponentEnvConfig:
        data = await self._load()
        entry = data.get(name)
        if entry is None:
            return ComponentEnvConfig()
        return ComponentEnvConfig.model_validate(entry)

    async def upsert(
        self, name: str, env: dict[str, str], secrets: dict[str, str]
    ) -> None:
        """Merge *env* and *secrets* into the stored config for *name*.

        Overwrites matching keys; does not wipe keys not mentioned.
        Encrypts each secret value before storing.
        """
        async with self._lock:
            data = await self._load()
            current = data.get(name, {"env": {}, "secret_tokens": {}})
            current_env: dict[str, str] = dict(current.get("env", {}))
            current_tokens: dict[str, str] = dict(current.get("secret_tokens", {}))

            current_env.update(env)
            for key, plaintext in secrets.items():
                current_tokens[key] = self._key_manager.encrypt(plaintext)

            data[name] = {"env": current_env, "secret_tokens": current_tokens}
            await self._save(data)

    async def delete_key(self, name: str, key: str) -> bool:
        """Remove *key* from env or secret_tokens.  Return True if found."""
        async with self._lock:
            data = await self._load()
            entry = data.get(name)
            if entry is None:
                return False
            found = False
            if key in entry.get("env", {}):
                del entry["env"][key]
                found = True
            if key in entry.get("secret_tokens", {}):
                del entry["secret_tokens"][key]
                found = True
            if found:
                # Remove the component entry entirely if both dicts are empty
                if not entry.get("env") and not entry.get("secret_tokens"):
                    del data[name]
                await self._save(data)
            return found

    async def delete(self, name: str) -> None:
        """Remove all env and secrets for *name*. No-op if absent."""
        async with self._lock:
            store = await self._load()
            store.pop(name, None)
            await self._save(store)

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
