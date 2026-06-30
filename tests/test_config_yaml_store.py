"""Tests for config.yaml parsing and ConfigYamlStore."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fastapi import HTTPException

from robotsix_central_deploy.onboard.models import ConfigParseError
from robotsix_central_deploy.onboard.parser import parse_config_yaml
from robotsix_central_deploy.registry.config_yaml_store import ConfigYamlStore


# ---------------------------------------------------------------------------
# parse_config_yaml
# ---------------------------------------------------------------------------


class TestParseConfigYaml:
    def test_parse_config_yaml_flat(self):
        yaml_bytes = yaml.dump({"host": "localhost", "port": 8080}).encode()
        result = parse_config_yaml(yaml_bytes)
        assert result == {"host": "localhost", "port": 8080}

    def test_parse_config_yaml_nested(self):
        yaml_bytes = yaml.dump(
            {"server": {"host": "localhost", "port": 8080}, "log_level": "info"}
        ).encode()
        result = parse_config_yaml(yaml_bytes)
        assert result == {
            "server": {"host": "localhost", "port": 8080},
            "log_level": "info",
        }

    def test_parse_config_yaml_empty_secret(self):
        yaml_bytes = yaml.dump({"api_key": ""}).encode()
        result = parse_config_yaml(yaml_bytes)
        assert result == {"api_key": ""}

    def test_parse_config_yaml_null_secret(self):
        yaml_str = "api_key:\n"
        result = parse_config_yaml(yaml_str.encode())
        assert result == {"api_key": None}

    def test_parse_config_yaml_invalid_yaml(self):
        with pytest.raises(ConfigParseError, match="parse error"):
            parse_config_yaml(b"\tinvalid: yaml: [")

    def test_parse_config_yaml_invalid_non_mapping(self):
        with pytest.raises(ConfigParseError, match="top-level YAML mapping"):
            parse_config_yaml(b"- item\n")


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
    _CONFIG_SECRET_SENTINEL,
    _annotate_secret_sentinels,
    _mask_secrets,
    _merge_config,
    _prune_unset,  # new
    _validate_account_ids,  # new
    _seed_for_detect,
)


class TestMaskSecrets:
    def test_mask_secret_with_value(self):
        """A template leaf of _CONFIG_SECRET_SENTINEL with a configured
        current value is masked as '***' regardless of key name."""
        result = _mask_secrets(
            {"password": _CONFIG_SECRET_SENTINEL}, {"password": "actual"}
        )
        assert result == {"password": "***"}

    def test_none_template_is_not_secret(self):
        """None in template no longer marks a secret — the value passes
        through from current.  (Previously None was treated as a secret
        marker alongside ''; now only _CONFIG_SECRET_SENTINEL is.)"""
        result = _mask_secrets({"k": None}, {"k": "value"})
        assert result == {"k": "value"}

    def test_no_mask_on_non_secret(self):
        result = _mask_secrets({"k": "default"}, {"k": "default"})
        assert result == {"k": "default"}

    def test_empty_string_template_is_not_secret(self):
        """Empty string in template no longer marks a secret — the current
        value passes through unmasked."""
        result = _mask_secrets({"k": ""}, {"k": "filled"})
        assert result == {"k": "filled"}

    def test_mask_nested(self):
        template = {"server": {"password": "SECRET"}, "port": 8080}
        current = {"server": {"password": "realpass"}, "port": 8080}
        result = _mask_secrets(template, current)
        assert result == {"server": {"password": "***"}, "port": 8080}

    def test_unconfigured_sentinel_returns_empty(self):
        """When template is _CONFIG_SECRET_SENTINEL but no current value
        exists, the field is returned as '' (not masked, not the
        literal sentinel string)."""
        result = _mask_secrets({"k": "SECRET"}, {})
        assert result == {"k": ""}

    def test_unconfigured_sentinel_current_equals_sentinel_returns_empty(self):
        """When current == template == 'SECRET' (no config stored yet),
        the field must NOT be masked — return '' (empty)."""
        result = _mask_secrets({"k": "SECRET"}, {"k": "SECRET"})
        assert result == {"k": ""}

    def test_mask_array_of_dicts_masks_secrets_in_each_item(self):
        template = {"accounts": [{"host": "example.com", "password": "SECRET"}]}
        current = {
            "accounts": [
                {"host": "imap.example.com", "password": "secret1"},
                {"host": "smtp.example.com", "password": "secret2"},
            ]
        }
        result = _mask_secrets(template, current)
        assert result == {
            "accounts": [
                {"host": "imap.example.com", "password": "***"},
                {"host": "smtp.example.com", "password": "***"},
            ]
        }

    def test_mask_scalar_array_passthrough(self):
        template = {"hosts": ["example.com"]}
        current = {"hosts": ["a.com", "b.com"]}
        result = _mask_secrets(template, current)
        assert result == {"hosts": ["a.com", "b.com"]}


class TestMergeConfig:
    def test_merge_preserves_masked_secrets(self):
        """When submitted is '***' for a _CONFIG_SECRET_SENTINEL leaf, the
        existing value is preserved."""
        result = _merge_config(
            {"pwd": _CONFIG_SECRET_SENTINEL}, {"pwd": "real"}, {"pwd": "***"}
        )
        assert result == {"pwd": "real"}

    def test_merge_replaces_secret(self):
        """When a new plain-text value is submitted for a
        _CONFIG_SECRET_SENTINEL leaf, the existing value is replaced."""
        result = _merge_config({"pwd": "SECRET"}, {"pwd": "old"}, {"pwd": "new"})
        assert result == {"pwd": "new"}

    def test_merge_uses_default_for_missing_key(self):
        result = _merge_config({"host": "localhost"}, {}, {})
        assert result == {"host": "localhost"}

    def test_merge_nested(self):
        template = {"server": {"host": "localhost", "password": "SECRET"}}
        existing = {"server": {"host": "0.0.0.0", "password": "realpass"}}
        submitted = {"server": {"host": "10.0.0.1", "password": "***"}}
        result = _merge_config(template, existing, submitted)
        assert result == {"server": {"host": "10.0.0.1", "password": "realpass"}}

    def test_merge_nested_adds_new_key_from_template(self):
        template = {"server": {"host": "localhost", "port": 8080}}
        existing = {"server": {"host": "0.0.0.0"}}
        submitted = {}
        result = _merge_config(template, existing, submitted)
        # Per spec: non-submitted keys fall back to template default.
        assert result == {"server": {"host": "localhost", "port": 8080}}

    def test_merge_none_template_secret(self):
        """None is no longer a secret marker — submitted '***' is NOT
        treated as a secret-preserve sentinel and is written literally."""
        template = {"password": None}
        existing = {"password": "real"}
        submitted = {"password": "***"}
        result = _merge_config(template, existing, submitted)
        # None is not _CONFIG_SECRET_SENTINEL, so the secret-preserve
        # branch is skipped; the submitted string '***' is written as-is.
        assert result == {"password": "***"}

    def test_merge_coerces_int_from_string(self):
        # The UI submits everything as strings; an int template leaf must
        # round-trip as an int, not "8080".
        result = _merge_config({"port": 8080}, {"port": 8080}, {"port": "9090"})
        assert result == {"port": 9090}
        assert isinstance(result["port"], int)

    def test_merge_coerces_bool_from_string(self):
        result = _merge_config(
            {"enabled": True}, {"enabled": True}, {"enabled": "false"}
        )
        assert result == {"enabled": False}
        assert isinstance(result["enabled"], bool)

    def test_merge_coerces_float_from_string(self):
        result = _merge_config({"ratio": 0.5}, {"ratio": 0.5}, {"ratio": "1.25"})
        assert result == {"ratio": 1.25}

    def test_merge_string_leaf_unchanged(self):
        result = _merge_config(
            {"host": "localhost"}, {"host": "localhost"}, {"host": "10.0.0.1"}
        )
        assert result == {"host": "10.0.0.1"}

    def test_merge_coerces_nested_typed_leaf(self):
        template = {"server": {"host": "localhost", "port": 8080}}
        existing = {"server": {"host": "0.0.0.0", "port": 8080}}
        submitted = {"server": {"host": "10.0.0.1", "port": "443"}}
        result = _merge_config(template, existing, submitted)
        assert result == {"server": {"host": "10.0.0.1", "port": 443}}
        assert isinstance(result["server"]["port"], int)

    def test_merge_unparseable_int_kept_as_string(self):
        # Never raise on a bad value — keep the submitted string.
        result = _merge_config({"port": 8080}, {"port": 8080}, {"port": "not-a-number"})
        assert result == {"port": "not-a-number"}

    def test_merge_coerces_list_from_json_string(self):
        result = _merge_config(
            {"hosts": ["a"]}, {"hosts": ["a"]}, {"hosts": '["x", "y"]'}
        )
        assert result == {"hosts": ["x", "y"]}

    def test_merge_array_of_dicts_preserves_secret_sentinel_per_item(self):
        template = {"accounts": [{"host": "example.com", "password": "SECRET"}]}
        existing = {"accounts": [{"host": "imap.example.com", "password": "real"}]}
        submitted = {"accounts": [{"host": "imap2.example.com", "password": "***"}]}
        result = _merge_config(template, existing, submitted)
        assert result == {
            "accounts": [{"host": "imap2.example.com", "password": "real"}]
        }

    def test_merge_array_of_dicts_updates_secret_when_new_value_submitted(self):
        template = {"accounts": [{"host": "example.com", "password": "SECRET"}]}
        existing = {"accounts": [{"host": "imap.example.com", "password": "old"}]}
        submitted = {"accounts": [{"host": "imap.example.com", "password": "new"}]}
        result = _merge_config(template, existing, submitted)
        assert result == {"accounts": [{"host": "imap.example.com", "password": "new"}]}

    def test_merge_array_of_dicts_add_item_no_existing(self):
        """New item has no corresponding existing entry — sentinel produces empty password."""
        template = {"accounts": [{"host": "example.com", "password": "SECRET"}]}
        existing = {"accounts": [{"host": "old.com", "password": "pass1"}]}
        submitted = {
            "accounts": [
                {"host": "old.com", "password": "***"},  # item 0 — preserve existing
                {
                    "host": "new.com",
                    "password": "pass2",
                },  # item 1 — no existing, sentinel falls back to template default
            ]
        }
        result = _merge_config(template, existing, submitted)
        assert (
            result["accounts"][0]["password"] == "pass1"
        )  # sentinel preserved for existing item
        assert result["accounts"][1]["password"] == "pass2"  # new item written as-is

    def test_merge_array_of_dicts_scalar_list_as_actual_list(self):
        """Scalar list submitted as an actual list (not JSON string) passes through."""
        result = _merge_config(
            {"hosts": ["a"]}, {"hosts": ["a"]}, {"hosts": ["x", "y"]}
        )
        assert result == {"hosts": ["x", "y"]}


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


def test_merge_preserves_both_accounts_two_account_config():
    template = {"accounts": [{"id": "", "imap": {"host": "", "password": "SECRET"}}]}
    existing = {
        "accounts": [
            {"id": "ovh", "imap": {"host": "ssl0.ovh.net", "password": "secret1"}},
            {"id": "gmail", "imap": {"host": "imap.gmail.com", "password": "secret2"}},
        ]
    }
    submitted = {
        "accounts": [
            {"id": "ovh", "imap": {"host": "ssl0.ovh.net", "password": "***"}},
            {"id": "gmail", "imap": {"host": "imap.gmail.com", "password": "***"}},
        ]
    }
    result = _merge_config(template, existing, submitted)
    assert len(result["accounts"]) == 2
    assert result["accounts"][0]["id"] == "ovh"
    assert result["accounts"][0]["imap"]["password"] == "secret1"
    assert result["accounts"][1]["id"] == "gmail"
    assert result["accounts"][1]["imap"]["password"] == "secret2"


def test_merge_editing_account0_does_not_touch_account1():
    template = {"accounts": [{"id": "", "imap": {"host": "", "password": "SECRET"}}]}
    existing = {
        "accounts": [
            {"id": "ovh", "imap": {"host": "ssl0.ovh.net", "password": "secret1"}},
            {"id": "gmail", "imap": {"host": "imap.gmail.com", "password": "secret2"}},
        ]
    }
    submitted = {
        "accounts": [
            {
                "id": "ovh",
                "imap": {"host": "new.host.net", "password": "***"},
            },  # edited
            {"id": "gmail", "imap": {"host": "imap.gmail.com", "password": "***"}},
        ]
    }
    result = _merge_config(template, existing, submitted)
    assert result["accounts"][0]["imap"]["host"] == "new.host.net"
    assert result["accounts"][1]["imap"]["host"] == "imap.gmail.com"
    assert result["accounts"][1]["imap"]["password"] == "secret2"


# ---------------------------------------------------------------------------
# Bug 3 — _merge_config no "***" literals
# ---------------------------------------------------------------------------


def test_merge_no_sentinel_literal_when_existing_dict_absent():
    """'***' must not survive to stored config when existing lacks the dict key."""
    template = {
        "archive": {"namespace": ""},
        "calendar": {"broker_password": _CONFIG_SECRET_SENTINEL},
    }
    existing: dict = {}
    submitted = {
        "archive": {"namespace": ""},
        "calendar": {"broker_password": "***"},
    }
    result = _merge_config(template, existing, submitted)
    assert result.get("calendar", {}).get("broker_password") != "***"
    assert result.get("archive", {}).get("namespace") != "***"


def test_merge_unsubmitted_secret_leaf_never_persists_sentinel():
    """A secret leaf absent from the submitted form must store "" — never the
    literal "SECRET" sentinel (which would be ingested as a real credential by
    components that read config.yaml directly)."""
    # Leaf missing while its parent section IS submitted.
    template = {"llm": {"api_key": _CONFIG_SECRET_SENTINEL, "model": "x"}}
    result = _merge_config(template, {}, {"llm": {"model": "x"}})
    assert result["llm"]["api_key"] == ""
    assert result["llm"]["api_key"] != _CONFIG_SECRET_SENTINEL

    # Whole section missing from the submitted form.
    template2 = {
        "host": "localhost",
        "calendar": {"broker_token": _CONFIG_SECRET_SENTINEL},
    }
    result2 = _merge_config(template2, {}, {"host": "localhost"})
    assert result2["calendar"]["broker_token"] == ""
    assert _CONFIG_SECRET_SENTINEL not in json.dumps(result2)


def test_merge_dict_branch_recurses_with_empty_existing():
    template = {"smtp": {"host": "", "port": 587, "password": _CONFIG_SECRET_SENTINEL}}
    existing: dict = {}
    submitted = {"smtp": {"host": "smtp.gmail.com", "port": "587", "password": "***"}}
    result = _merge_config(template, existing, submitted)
    assert result["smtp"]["host"] == "smtp.gmail.com"
    assert result["smtp"]["port"] == 587  # coerced from string
    assert result["smtp"]["password"] == ""  # *** with no existing → ""
    assert result["smtp"]["password"] != "***"


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


class TestConfigRoundTrip:
    def test_round_trip_two_account_config_unchanged(self):
        """GET mask → collectConfigValues sim → PUT merge+prune == original existing."""
        import copy
        import json

        template = {
            "accounts": [
                {"id": "", "imap": {"host": "", "password": _CONFIG_SECRET_SENTINEL}}
            ],
            "archive": {"namespace": ""},
            "calendar": {"broker_password": _CONFIG_SECRET_SENTINEL},
        }
        existing = {
            "accounts": [
                {"id": "ovh", "imap": {"host": "ssl0.ovh.net", "password": "secret1"}},
                {
                    "id": "gmail",
                    "imap": {"host": "imap.gmail.com", "password": "secret2"},
                },
            ]
            # archive and calendar deliberately absent from existing
        }
        # Simulate _mask_secrets (what GET /config returns)
        masked = _mask_secrets(template, existing)
        # Simulate collectConfigValues: empty password inputs → '***'
        submitted = copy.deepcopy(masked)
        # archive and calendar appear in masked as template defaults;
        # collectConfigValues converts empty password to '***'
        submitted["archive"] = {"namespace": ""}
        submitted["calendar"] = {"broker_password": "***"}

        merged = _merge_config(template, existing, submitted)
        result = _prune_unset(merged, existing)

        assert len(result["accounts"]) == 2
        assert result["accounts"][0]["id"] == "ovh"
        assert result["accounts"][0]["imap"]["password"] == "secret1"
        assert result["accounts"][1]["id"] == "gmail"
        assert result["accounts"][1]["imap"]["password"] == "secret2"
        serialised = json.dumps(result)
        assert "***" not in serialised
        assert result.get("archive", {}).get("namespace") != "***"
        assert result.get("calendar", {}).get("broker_password") != "***"


# ---------------------------------------------------------------------------
# _annotate_secret_sentinels
# ---------------------------------------------------------------------------


class TestAnnotateSecretSentinels:
    def test_secret_named_blank_leaf_is_NOT_marked(self):
        """Detection is sentinel-only: a ``password`` leaf left blank stays
        blank (a plain editable field), NOT auto-marked as a secret."""
        result = _annotate_secret_sentinels(
            {"host": "localhost", "password": "", "port": 8080}
        )
        assert result == {"host": "localhost", "password": "", "port": 8080}

    def test_explicit_sentinel_flat_preserved(self):
        result = _annotate_secret_sentinels(
            {"host": "localhost", "password": "SECRET", "port": 8080}
        )
        assert result == {"host": "localhost", "password": "SECRET", "port": 8080}

    def test_flat_dict_non_secret_unchanged(self):
        result = _annotate_secret_sentinels(
            {"host": "localhost", "port": 8080, "timeout": 30}
        )
        assert result == {"host": "localhost", "port": 8080, "timeout": 30}

    def test_non_secret_named_key_not_marked(self):
        """A field whose name merely *looks* secret (``public_key``) is left
        editable unless its value is the explicit sentinel."""
        result = _annotate_secret_sentinels({"langfuse": {"public_key": ""}})
        assert result == {"langfuse": {"public_key": ""}}

    def test_nested_dict_explicit_sentinel_at_depth_two(self):
        result = _annotate_secret_sentinels(
            {"server": {"host": "", "api_key": "SECRET"}}
        )
        assert result == {"server": {"host": "", "api_key": "SECRET"}}

    def test_array_of_objects_explicit_sentinel(self):
        result = _annotate_secret_sentinels(
            {"accounts": [{"imap": {"host": "", "password": "SECRET"}}]}
        )
        assert result == {"accounts": [{"imap": {"host": "", "password": "SECRET"}}]}

    def test_explicit_sentinel_preserved(self):
        """A non-secret-named key with value "SECRET" is preserved."""
        result = _annotate_secret_sentinels({"custom_field": "SECRET"})
        assert result == {"custom_field": "SECRET"}

    def test_scalar_array_unchanged(self):
        result = _annotate_secret_sentinels({"ports": [80, 443]})
        assert result == {"ports": [80, 443]}

    def test_idempotent(self):
        """Calling twice yields the same result."""
        template = {"password": "SECRET", "host": "localhost"}
        first = _annotate_secret_sentinels(template)
        second = _annotate_secret_sentinels(first)
        assert first == second
        assert first == {"password": "SECRET", "host": "localhost"}
