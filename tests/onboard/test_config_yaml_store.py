"""Tests for config.json parsing and ConfigYamlStore."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastapi import HTTPException

from robotsix_central_deploy.onboard.models import ConfigParseError
from robotsix_central_deploy.onboard.parser import parse_config_json
from robotsix_central_deploy.registry.config_yaml_store import ConfigYamlStore

# ---------------------------------------------------------------------------
# parse_config_json
# ---------------------------------------------------------------------------


class TestParseConfigJson:
    def test_parse_config_json_flat(self):
        json_bytes = json.dumps({"host": "localhost", "port": 8080}).encode()
        result = parse_config_json(json_bytes)
        assert result == {"host": "localhost", "port": 8080}

    def test_parse_config_json_nested(self):
        json_bytes = json.dumps(
            {"server": {"host": "localhost", "port": 8080}, "log_level": "info"}
        ).encode()
        result = parse_config_json(json_bytes)
        assert result == {
            "server": {"host": "localhost", "port": 8080},
            "log_level": "info",
        }

    def test_parse_config_json_empty_secret(self):
        json_bytes = json.dumps({"api_key": ""}).encode()
        result = parse_config_json(json_bytes)
        assert result == {"api_key": ""}

    def test_parse_config_json_null_secret(self):
        json_bytes = b'{"api_key": null}'
        result = parse_config_json(json_bytes)
        assert result == {"api_key": None}

    def test_parse_config_json_invalid_yaml(self):
        with pytest.raises(ConfigParseError, match="parse error"):
            parse_config_json(b'{"invalid": ')

    def test_parse_config_json_invalid_non_mapping(self):
        with pytest.raises(ConfigParseError, match="top-level JSON object"):
            parse_config_json(b'["item"]')


# ---------------------------------------------------------------------------
# ConfigYamlStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_yaml_store_round_trip(tmp_path: Path):
    store_path = tmp_path / "component_config_yaml.json"
    store = ConfigYamlStore(store_path)

    template = {"host": "localhost", "port": 8080, "password": ""}
    await store.save_template("test-svc", template)

    # Template stored and retrievable
    got_template = await store.get_template("test-svc")
    assert got_template == template

    # Current is None initially
    assert await store.get_current("test-svc") is None

    # Update current
    current = {"host": "0.0.0.0", "port": 3000, "password": "secret123"}
    await store.update_current("test-svc", current)

    got_current = await store.get_current("test-svc")
    assert got_current == current

    # Template still intact
    assert await store.get_template("test-svc") == template

    # Delete
    await store.delete("test-svc")
    assert await store.get_template("test-svc") is None
    assert await store.get_current("test-svc") is None


@pytest.mark.asyncio
async def test_config_yaml_store_save_template_preserves_current(tmp_path: Path):
    store_path = tmp_path / "component_config_yaml.json"
    store = ConfigYamlStore(store_path)

    template = {"key": "val"}
    await store.save_template("svc", template)
    await store.update_current("svc", {"key": "overridden"})

    # Overwrite template — current must survive
    new_template = {"key": "new_default", "extra": "yes"}
    await store.save_template("svc", new_template)

    assert await store.get_template("svc") == new_template
    assert await store.get_current("svc") == {"key": "overridden"}


# ---------------------------------------------------------------------------
# _mask_secrets and _merge_config (imported from server)
# ---------------------------------------------------------------------------

from robotsix_central_deploy.lifecycle.server import (  # noqa: E402
    _prune_unset,  # new
    _validate_account_ids,  # new
    _seed_for_detect,
)

# TestMaskSecrets removed — old sentinel-based templates no longer supported

# TestMergeConfig removed — old sentinel-based templates no longer supported

# ---------------------------------------------------------------------------
# _seed_for_detect
# ---------------------------------------------------------------------------


class TestSeedForDetect:
    def test_omits_template_defaults(self):
        """Template defaults (empty strings) for unsubmitted fields are excluded."""
        template = {
            "accounts": [
                {
                    "imap": {"host": ""},
                    "smtp": {"host": ""},
                    "auth": {"username": "", "password": ""},
                }
            ]
        }
        submitted = {
            "accounts": [{"auth": {"username": "test@gmail.com", "password": "x"}}]
        }
        existing: dict = {}
        result = _seed_for_detect(template, existing, submitted)
        assert result == {
            "accounts": [{"auth": {"username": "test@gmail.com", "password": "x"}}]
        }
        # No imap or smtp keys in accounts[0]
        assert "imap" not in result["accounts"][0]
        assert "smtp" not in result["accounts"][0]

    def test_substitutes_secret_sentinel(self):
        """'***' sentinel for a secret field resolves from existing."""
        template = {
            "accounts": [
                {
                    "imap": {"host": ""},
                    "smtp": {"host": ""},
                    "auth": {"username": "", "password": ""},
                }
            ]
        }
        existing = {
            "accounts": [
                {
                    "auth": {"password": "real_pass"},
                }
            ]
        }
        submitted = {
            "accounts": [{"auth": {"username": "test@gmail.com", "password": "***"}}]
        }
        result = _seed_for_detect(template, existing, submitted)
        assert result == {
            "accounts": [
                {"auth": {"username": "test@gmail.com", "password": "real_pass"}}
            ]
        }

    def test_preserves_non_secret_submitted(self):
        """Explicitly submitted non-secret fields are included as-is."""
        template = {
            "accounts": [
                {
                    "imap": {"host": ""},
                    "auth": {"username": "", "password": ""},
                }
            ]
        }
        submitted = {
            "accounts": [
                {
                    "imap": {"host": "custom.imap.example.com"},
                    "auth": {"username": "test@gmail.com", "password": "x"},
                }
            ]
        }
        existing: dict = {}
        result = _seed_for_detect(template, existing, submitted)
        assert result == {
            "accounts": [
                {
                    "imap": {"host": "custom.imap.example.com"},
                    "auth": {"username": "test@gmail.com", "password": "x"},
                }
            ]
        }

    def test_secret_sentinel_defaults_to_empty_when_no_existing(self):
        """'***' with no existing value falls back to ''."""
        template = {"auth": {"password": ""}}
        existing: dict = {}
        submitted = {"auth": {"password": "***"}}
        result = _seed_for_detect(template, existing, submitted)
        assert result == {"auth": {"password": ""}}

    def test_shallow_dict_omits_unsubmitted_keys(self):
        template = {"host": "", "port": 993, "tls": True}
        existing: dict = {}
        submitted = {"port": 443}
        result = _seed_for_detect(template, existing, submitted)
        assert result == {"port": 443}
        assert "host" not in result
        assert "tls" not in result

    def test_skips_empty_string_values_in_submitted(self):
        """Empty strings that appear in submitted (e.g. from form defaults)
        are skipped — the detect program fills them in."""
        template = {
            "accounts": [
                {
                    "imap": {"host": ""},
                    "smtp": {"host": ""},
                    "auth": {"username": "", "password": ""},
                }
            ]
        }
        submitted = {
            "accounts": [
                {
                    "auth": {"username": "alice"},
                    "imap": {"host": ""},
                }
            ]
        }
        existing: dict = {}
        result = _seed_for_detect(template, existing, submitted)
        assert result == {"accounts": [{"auth": {"username": "alice"}}]}
        # imap.host was empty string → skipped entirely
        assert "imap" not in result["accounts"][0]

    def test_dict_result_omitted_when_recursion_empty(self):
        """A nested dict whose every field is skipped produces no entry."""
        template = {"server": {"host": "", "port": 0}}
        submitted = {"server": {"host": ""}}
        existing: dict = {}
        result = _seed_for_detect(template, existing, submitted)
        # host was empty → skipped; no other keys submitted → server omitted
        assert result == {}

    def test_list_omitted_when_all_items_empty(self):
        """A list of dicts where every item resolves to empty is omitted."""
        template = {"accounts": [{"imap": {"host": ""}, "auth": {"username": ""}}]}
        submitted = {"accounts": [{"imap": {"host": ""}}]}
        existing: dict = {}
        result = _seed_for_detect(template, existing, submitted)
        # The single item resolves to empty → list omitted
        assert "accounts" not in result


# ---------------------------------------------------------------------------
# Bug 1 — _merge_config preserves both accounts
# ---------------------------------------------------------------------------

# test_merge_preserves_both_accounts_two_account_config removed — old sentinel-based templates no longer supported

# test_merge_editing_account0_does_not_touch_account1 removed — old sentinel-based templates no longer supported

# ---------------------------------------------------------------------------
# Bug 3 — _merge_config no "***" literals
# ---------------------------------------------------------------------------

# test_merge_no_sentinel_literal_when_existing_dict_absent removed — old sentinel-based templates no longer supported

# test_merge_unsubmitted_secret_leaf_never_persists_sentinel removed — old sentinel-based templates no longer supported

# test_merge_dict_branch_recurses_with_empty_existing removed — old sentinel-based templates no longer supported

# ---------------------------------------------------------------------------
# Bug 2 — _validate_account_ids
# ---------------------------------------------------------------------------


class TestValidateAccountIds:
    def test_rejects_email_address_as_id(self):
        merged = {"accounts": [{"id": "damien@robotsix.net", "imap": {"host": "x"}}]}
        with pytest.raises(HTTPException) as exc_info:
            _validate_account_ids(merged)
        assert exc_info.value.status_code == 422
        assert "@" in exc_info.value.detail or "account_id" in exc_info.value.detail

    def test_accepts_valid_slugs(self):
        merged = {
            "accounts": [
                {"id": "ovh", "imap": {"host": "x"}},
                {"id": "gmail-work", "imap": {"host": "y"}},
            ]
        }
        _validate_account_ids(merged)  # must not raise

    def test_skips_non_account_configs(self):
        merged = {"host": "smtp.example.com", "port": 587}
        _validate_account_ids(merged)  # must not raise; no 'accounts' key


# ---------------------------------------------------------------------------
# Bug 3 — _prune_unset
# ---------------------------------------------------------------------------


class TestPruneUnset:
    def test_prune_removes_empty_field_absent_from_existing(self):
        merged = {"archive": {"namespace": ""}}
        existing: dict = {}
        result = _prune_unset(merged, existing)
        assert "archive" not in result

    def test_prune_preserves_nonempty_int_default(self):
        merged = {"host": "imap.example.com", "port": 993}
        existing = {"host": "old.host"}
        result = _prune_unset(merged, existing)
        assert result["port"] == 993  # int, not '' or None — never pruned

    def test_prune_preserves_explicit_clear_when_field_was_in_existing(self):
        """User explicitly clearing a field previously set must survive."""
        merged = {"archive": {"namespace": ""}}
        existing = {"archive": {"namespace": "myns"}}
        result = _prune_unset(merged, existing)
        assert result["archive"]["namespace"] == ""

    def test_prune_preserves_real_value_in_new_section(self):
        """A dict block not in existing but with a real value must survive."""
        merged = {"smtp": {"host": "smtp.gmail.com", "password": ""}}
        existing: dict = {}
        result = _prune_unset(merged, existing)
        assert result["smtp"]["host"] == "smtp.gmail.com"  # non-empty → survives


# ---------------------------------------------------------------------------
# Bug 4 — Round-trip integration invariant
# ---------------------------------------------------------------------------

# TestConfigRoundTrip removed — old sentinel-based templates no longer supported

# ---------------------------------------------------------------------------
# ConfigYamlStore — update_current_and_hash round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_current_and_hash_round_trip(tmp_path):
    """get_volume_hash returns the value passed to update_current_and_hash."""
    store = ConfigYamlStore(tmp_path / "config_yaml.json")
    await store.save_template("comp-a", {"host": "localhost"})
    await store.update_current_and_hash("comp-a", {"host": "prod"}, "abc123hash")
    h = await store.get_volume_hash("comp-a")
    assert h == "abc123hash"
