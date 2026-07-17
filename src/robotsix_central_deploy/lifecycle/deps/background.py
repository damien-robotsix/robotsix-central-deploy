"""Background loops: Claude auth credential refresh and registry check."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from ..backends import ExecutionBackend
from ..models import CLAUDE_AUTH_VOLUME, ServiceRecord
from ..store import ServiceStore
from ...registry_check import RegistryChecker

logger = logging.getLogger(__name__)

# -- Claude auth constants -------------------------------------------------

CLAUDE_AUTH_REFRESH_BEFORE_SECONDS = 3600  # refresh when ≤ 1 hour until expiry
CLAUDE_AUTH_USER_AGENT = "claude-cli/2.1.199 (external, cli)"  # noqa: E501 — avoids Cloudflare 403
CLAUDE_AUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"  # noqa: S105 — URL, not a password
CLAUDE_AUTH_CLIENT_ID = (
    "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # gitleaks:allow — public OAuth client id
)

#: Module-level state for the last Claude auth refresh attempt.
_claude_auth_refresh_state: dict[str, Any] = {
    "last_refresh": None,  # float — monotonic timestamp of last attempt
    "last_error": None,  # str | None — error message if last refresh failed
}


def get_claude_auth_refresh_state() -> dict[str, Any]:
    """Return a snapshot of the Claude auth refresh state.

    Keys: ``last_refresh`` (float | None), ``last_error`` (str | None).
    Callers can derive ``refresh_status`` — ``"ok"`` when last_refresh is
    set and last_error is None, ``"failed"`` when last_error is set, or
    ``"never"`` otherwise.
    """
    return dict(_claude_auth_refresh_state)


# ---------------------------------------------------------------------------
# Background registry-check loop
# ---------------------------------------------------------------------------


async def _check_and_update_record(
    record: ServiceRecord,
    store: ServiceStore,
    checker: RegistryChecker,
    backend: ExecutionBackend,
) -> None:
    """Refresh a single service record's digest and update-availability from the registry."""
    if record.image and not record.deployed_image_digest:
        try:
            ins = await backend.status(record)
            if ins.running_digest:
                record.deployed_image_digest = ins.running_digest
                await store.put(record)
        except Exception:
            pass  # Status check may fail if the container isn't running; proceed without digest

    if not record.image or not record.deployed_image_digest:
        return
    try:
        latest = await checker.get_latest_digest(record.image)
        if latest is not None:
            new_ua = latest != record.deployed_image_digest
            if (
                record.update_available != new_ua
                or record.latest_registry_digest != latest
            ):
                record.update_available = new_ua
                record.latest_registry_digest = latest
                await store.put(record)
    except Exception:  # noqa: BLE001
        pass


async def _registry_check_loop(
    store: ServiceStore,
    checker: RegistryChecker,
    backend: ExecutionBackend,
    interval_sec: int,
) -> None:
    """Periodically poll the registry for every managed service and
    update ``update_available`` / ``latest_registry_digest``."""
    try:
        while True:
            await asyncio.sleep(interval_sec)
            records = await store.list_all()
            for record in records:
                await _check_and_update_record(record, store, checker, backend)
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Background Claude auth credential refresh loop
# ---------------------------------------------------------------------------


async def _refresh_claude_credentials(
    backend: ExecutionBackend,
    oauth: dict[str, Any],
) -> tuple[bool, str | None]:
    """POST a refresh_token grant to the Anthropic OAuth token endpoint,
    build updated credentials, and persist them via *backend*.

    Returns ``(True, None)`` on success, ``(False, error_message)`` on failure.
    The caller is responsible for updating the module-level refresh state.
    """
    refresh_token = oauth.get("refreshToken")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                CLAUDE_AUTH_TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLAUDE_AUTH_CLIENT_ID,
                },
                headers={"User-Agent": CLAUDE_AUTH_USER_AGENT},
            )
        except Exception as exc:
            error_msg = f"Token endpoint unreachable: {exc}"
            logger.warning("Claude auth refresh: request failed: %s", exc)
            return False, error_msg

    if resp.status_code != 200:
        error_detail = resp.text[:500]
        try:
            error_detail = resp.json().get("error", {}).get("message", error_detail)
        except Exception:  # noqa: S110 — non-JSON body is fine
            pass
        error_msg = f"Refresh failed ({resp.status_code}): {error_detail}"
        logger.warning("Claude auth refresh: %s", error_detail)
        return False, error_msg

    try:
        payload: dict[str, Any] = resp.json()
    except Exception as exc:
        error_msg = f"Invalid JSON in refresh response: {exc}"
        logger.warning("Claude auth refresh: bad response JSON: %s", exc)
        return False, error_msg

    access_token = payload.get("access_token")
    new_refresh_token = payload.get("refresh_token", refresh_token)
    expires_in = payload.get("expires_in", 0)

    if not access_token:
        error_msg = "No access_token in refresh response"
        logger.warning("Claude auth refresh: no access_token in response")
        return False, error_msg

    # Build new credentials blob — always persist the rotated
    # refresh token from the server (the ticket gotcha).
    new_creds: dict[str, Any] = {
        "claudeAiOauth": {
            "accessToken": access_token,
            "refreshToken": new_refresh_token,
            "expiresAt": int((time.time() + float(expires_in)) * 1000),
            "scopes": oauth.get("scopes", ["user:inference"]),
        }
    }
    # Preserve optional fields from the original credential blob.
    for key in ("subscriptionType", "rateLimitTier"):
        if key in oauth:
            new_creds["claudeAiOauth"][key] = oauth[key]

    try:
        await backend.write_claude_credentials(
            CLAUDE_AUTH_VOLUME, json.dumps(new_creds, indent=2)
        )
    except Exception as exc:
        error_msg = f"Failed to write refreshed credentials: {exc}"
        logger.warning("Claude auth refresh: write failed: %s", exc)
        return False, error_msg

    return True, None


async def _claude_auth_refresh_loop(
    backend: ExecutionBackend,
    interval_sec: int,
) -> None:
    """Periodically check and refresh Claude auth credentials in the
    ``claude-auth`` named volume.

    Reads ``.credentials.json``, checks whether the access token expires
    within *CLAUDE_AUTH_REFRESH_BEFORE_SECONDS*, and POSTs a refresh_token
    grant to the Anthropic OAuth token endpoint when needed.  Rotated
    refresh tokens are persisted immediately — losing the rotated token
    strands the volume until a manual re-login.
    """
    global _claude_auth_refresh_state
    try:
        while True:
            await asyncio.sleep(interval_sec)
            try:
                # Check current status — skip if not authenticated.
                status = await backend.check_claude_auth(CLAUDE_AUTH_VOLUME)
            except NotImplementedError:
                return  # backend does not support claude auth -> nothing to do
            except Exception:
                logger.debug(
                    "Claude auth refresh: check_claude_auth failed", exc_info=True
                )
                continue

            if status.get("status") != "authenticated":
                continue

            # Read credentials to inspect expiry and refresh token.
            try:
                creds = await backend.read_claude_credentials(CLAUDE_AUTH_VOLUME)
            except Exception:
                logger.debug(
                    "Claude auth refresh: read_claude_credentials failed", exc_info=True
                )
                continue

            oauth = creds.get("claudeAiOauth", {})
            if not isinstance(oauth, dict):
                continue

            refresh_token = oauth.get("refreshToken")
            expires_at_ms = oauth.get("expiresAt")

            if not refresh_token or not expires_at_ms:
                continue  # nothing to refresh without these

            now_ms = int(time.time() * 1000)
            if expires_at_ms - now_ms > CLAUDE_AUTH_REFRESH_BEFORE_SECONDS * 1000:
                continue  # not close enough to expiry

            success, error = await _refresh_claude_credentials(backend, oauth)
            if success:
                _claude_auth_refresh_state = {
                    "last_refresh": time.monotonic(),
                    "last_error": None,
                }
                logger.info("Claude auth credentials refreshed successfully")
            else:
                _claude_auth_refresh_state = {
                    "last_refresh": time.monotonic(),
                    "last_error": error,
                }

    except asyncio.CancelledError:
        pass
