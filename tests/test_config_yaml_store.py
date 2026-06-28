"""Tests for config.yaml parsing and ConfigYamlStore."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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


from robotsix_central_deploy.lifecycle.server import _mask_secrets, _merge_config  # noqa: E402


class TestMaskSecrets:
    def test_mask_secret_with_value(self):
        result = _mask_secrets({"k": ""}, {"k": "actual"})
        assert result == {"k": "***"}

    def test_mask_secret_none_value(self):
        result = _mask_secrets({"k": None}, {"k": "actual"})
        assert result == {"k": "***"}

    def test_no_mask_on_non_secret(self):
        result = _mask_secrets({"k": "default"}, {"k": "default"})
        assert result == {"k": "default"}

    def test_no_mask_on_empty_secret_with_no_current_value(self):
        # If current value is also empty, it's not a non-empty string → no mask
        result = _mask_secrets({"k": ""}, {"k": ""})
        assert result == {"k": ""}

    def test_mask_nested(self):
        template = {"server": {"password": ""}, "port": 8080}
        current = {"server": {"password": "realpass"}, "port": 8080}
        result = _mask_secrets(template, current)
        assert result == {"server": {"password": "***"}, "port": 8080}

    def test_mask_secret_none_template_none_current(self):
        result = _mask_secrets({"k": None}, {"k": None})
        assert result == {"k": None}


class TestMergeConfig:
    def test_merge_preserves_masked_secrets(self):
        result = _merge_config({"pwd": ""}, {"pwd": "real"}, {"pwd": "***"})
        assert result == {"pwd": "real"}

    def test_merge_replaces_secret(self):
        result = _merge_config({"pwd": ""}, {"pwd": "old"}, {"pwd": "new"})
        assert result == {"pwd": "new"}

    def test_merge_uses_default_for_missing_key(self):
        result = _merge_config({"host": "localhost"}, {}, {})
        assert result == {"host": "localhost"}

    def test_merge_nested(self):
        template = {"server": {"host": "localhost", "password": ""}}
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
        template = {"password": None}
        existing = {"password": "real"}
        submitted = {"password": "***"}
        result = _merge_config(template, existing, submitted)
        assert result == {"password": "real"}

    def test_merge_coerces_int_from_string(self):
        # The UI submits everything as strings; an int template leaf must
        # round-trip as an int, not "8080".
        result = _merge_config({"port": 8080}, {"port": 8080}, {"port": "9090"})
        assert result == {"port": 9090}
        assert isinstance(result["port"], int)

    def test_merge_coerces_bool_from_string(self):
        result = _merge_config({"enabled": True}, {"enabled": True}, {"enabled": "false"})
        assert result == {"enabled": False}
        assert isinstance(result["enabled"], bool)

    def test_merge_coerces_float_from_string(self):
        result = _merge_config({"ratio": 0.5}, {"ratio": 0.5}, {"ratio": "1.25"})
        assert result == {"ratio": 1.25}

    def test_merge_string_leaf_unchanged(self):
        result = _merge_config({"host": "localhost"}, {"host": "localhost"}, {"host": "10.0.0.1"})
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
        result = _merge_config({"hosts": ["a"]}, {"hosts": ["a"]}, {"hosts": '["x", "y"]'})
        assert result == {"hosts": ["x", "y"]}
