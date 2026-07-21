"""Unit tests for lifecycle/_config_utils.py — config merge, coercion, $ref
resolution, secret masking, and canonical hashing."""

from __future__ import annotations

import pytest

from robotsix_central_deploy.lifecycle._config_utils import (
    _canonical_hash,
    _coerce_by_schema,
    _deep_merge,
    _is_json_schema,
    _is_key_secret,
    _is_secret_prop,
    _mask_secrets,
    _mask_secrets_json_schema,
    _merge_config,
    _merge_config_flat,
    _merge_config_json_schema,
    _resolve_ref,
    _restore_secrets_from_current,
    _strip_secret_values,
)


# ---------------------------------------------------------------------------
# _deep_merge
# ---------------------------------------------------------------------------


class TestDeepMerge:
    def test_top_level_override(self):
        base = {"a": 1, "b": 2}
        override = {"b": 99}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 99}

    def test_nested_dict_merge(self):
        base = {"outer": {"inner_a": 1, "inner_b": 2}}
        override = {"outer": {"inner_b": 99, "inner_c": 3}}
        result = _deep_merge(base, override)
        assert result == {"outer": {"inner_a": 1, "inner_b": 99, "inner_c": 3}}

    def test_deeply_nested_merge(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        override = {"a": {"b": {"d": 99}}}
        result = _deep_merge(base, override)
        assert result == {"a": {"b": {"c": 1, "d": 99}}}

    def test_override_wins_over_nested(self):
        base = {"a": {"b": {"c": 1}}}
        override = {"a": "replaced"}
        result = _deep_merge(base, override)
        assert result == {"a": "replaced"}

    def test_new_key_added(self):
        base: dict[str, object] = {}
        override = {"new_key": "value"}
        result = _deep_merge(base, override)
        assert result == {"new_key": "value"}

    def test_empty_override(self):
        base = {"a": 1, "b": {"c": 2}}
        override: dict[str, object] = {}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": {"c": 2}}

    def test_empty_base(self):
        base: dict[str, object] = {}
        override = {"a": {"b": 2}}
        result = _deep_merge(base, override)
        assert result == {"a": {"b": 2}}

    def test_does_not_mutate_base(self):
        base = {"a": {"b": 1}}
        override = {"a": {"c": 2}}
        _deep_merge(base, override)
        assert base == {"a": {"b": 1}}

    def test_override_value_is_none(self):
        base = {"a": 1}
        override = {"a": None}
        result = _deep_merge(base, override)
        assert result == {"a": None}


# ---------------------------------------------------------------------------
# _is_json_schema
# ---------------------------------------------------------------------------


class TestIsJsonSchema:
    def test_has_properties_key(self):
        assert _is_json_schema({"type": "object", "properties": {"x": {}}}) is True

    def test_no_properties_key(self):
        assert _is_json_schema({"key": "value"}) is False

    def test_empty_dict(self):
        assert _is_json_schema({}) is False


# ---------------------------------------------------------------------------
# _is_secret_prop
# ---------------------------------------------------------------------------


class TestIsSecretProp:
    def test_secret_prop(self):
        assert _is_secret_prop({"format": "password", "writeOnly": True}) is True

    def test_not_password_format(self):
        assert _is_secret_prop({"format": "email", "writeOnly": True}) is False

    def test_not_write_only(self):
        assert _is_secret_prop({"format": "password", "writeOnly": False}) is False

    def test_neither(self):
        assert _is_secret_prop({"type": "string"}) is False

    def test_empty_dict(self):
        assert _is_secret_prop({}) is False


# ---------------------------------------------------------------------------
# _resolve_ref
# ---------------------------------------------------------------------------


class TestResolveRef:
    def test_resolves_local_ref(self):
        root = {
            "$defs": {
                "MyDef": {"type": "string", "format": "password", "writeOnly": True}
            }
        }
        prop = {"$ref": "#/$defs/MyDef"}
        result = _resolve_ref(prop, root)
        assert result == {"type": "string", "format": "password", "writeOnly": True}

    def test_no_ref_returns_original(self):
        prop = {"type": "integer"}
        result = _resolve_ref(prop, {})
        assert result == {"type": "integer"}

    def test_ref_not_string(self):
        prop = {"$ref": 123}
        result = _resolve_ref(prop, {})
        assert result == {"$ref": 123}

    def test_ref_not_starting_with_defs(self):
        prop = {"$ref": "https://example.com/schema.json"}
        result = _resolve_ref(prop, {})
        assert result == {"$ref": "https://example.com/schema.json"}

    def test_def_name_not_found(self):
        prop = {"$ref": "#/$defs/NonExistent"}
        result = _resolve_ref(prop, {"$defs": {}})
        assert result == {"$ref": "#/$defs/NonExistent"}

    def test_defs_missing_from_root(self):
        prop = {"$ref": "#/$defs/MyDef"}
        result = _resolve_ref(prop, {})
        assert result == {"$ref": "#/$defs/MyDef"}


# ---------------------------------------------------------------------------
# _coerce_by_schema
# ---------------------------------------------------------------------------


class TestCoerceBySchema:
    # --- integer ---
    def test_integer_from_string(self):
        assert _coerce_by_schema({"type": "integer"}, "42") == 42

    def test_integer_from_float(self):
        assert _coerce_by_schema({"type": "integer"}, 3.14) == 3

    def test_integer_from_int_is_noop(self):
        assert _coerce_by_schema({"type": "integer"}, 7) == 7

    def test_integer_bool_raises(self):
        with pytest.raises(ValueError, match="expected integer"):
            _coerce_by_schema({"type": "integer"}, True)

    def test_integer_unparseable_string_raises(self):
        with pytest.raises(ValueError, match="expected integer"):
            _coerce_by_schema({"type": "integer"}, "not_a_number")

    # --- number ---
    def test_number_from_string(self):
        assert _coerce_by_schema({"type": "number"}, "3.14") == 3.14

    def test_number_from_int(self):
        assert _coerce_by_schema({"type": "number"}, 5) == 5.0

    def test_number_bool_raises(self):
        with pytest.raises(ValueError, match="expected number"):
            _coerce_by_schema({"type": "number"}, False)

    def test_number_unparseable_raises(self):
        with pytest.raises(ValueError, match="expected number"):
            _coerce_by_schema({"type": "number"}, "abc")

    # --- boolean ---
    def test_boolean_from_true_strings(self):
        for s in ("true", "True", "TRUE", "1", "yes", "on"):
            assert _coerce_by_schema({"type": "boolean"}, s) is True

    def test_boolean_from_false_strings(self):
        for s in ("false", "False", "FALSE", "0", "no", "off", ""):
            assert _coerce_by_schema({"type": "boolean"}, s) is False

    def test_boolean_from_bool_is_noop(self):
        assert _coerce_by_schema({"type": "boolean"}, True) is True
        assert _coerce_by_schema({"type": "boolean"}, False) is False

    def test_boolean_invalid_string_raises(self):
        with pytest.raises(ValueError, match="expected boolean"):
            _coerce_by_schema({"type": "boolean"}, "maybe")

    def test_boolean_number_raises(self):
        with pytest.raises(ValueError, match="expected boolean"):
            _coerce_by_schema({"type": "boolean"}, 1)

    # --- string ---
    def test_string_from_int(self):
        assert _coerce_by_schema({"type": "string"}, 42) == "42"

    def test_string_from_float(self):
        assert _coerce_by_schema({"type": "string"}, 3.14) == "3.14"

    def test_string_from_bool(self):
        assert _coerce_by_schema({"type": "string"}, True) == "True"

    def test_string_from_string_noop(self):
        assert _coerce_by_schema({"type": "string"}, "hello") == "hello"

    # --- no type / unknown ---
    def test_no_type_passthrough(self):
        assert _coerce_by_schema({}, "anything") == "anything"
        assert _coerce_by_schema({}, 123) == 123


# ---------------------------------------------------------------------------
# _canonical_hash
# ---------------------------------------------------------------------------


class TestCanonicalHash:
    def test_deterministic_output(self):
        d = {"b": 2, "a": 1}
        h1 = _canonical_hash(d)
        h2 = _canonical_hash(d)
        assert h1 == h2

    def test_sort_keys_independence(self):
        h1 = _canonical_hash({"a": 1, "b": 2})
        h2 = _canonical_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_different_data_different_hash(self):
        h1 = _canonical_hash({"a": 1})
        h2 = _canonical_hash({"a": 2})
        assert h1 != h2

    def test_nested_sort_keys(self):
        h1 = _canonical_hash({"outer": {"b": 2, "a": 1}})
        h2 = _canonical_hash({"outer": {"a": 1, "b": 2}})
        assert h1 == h2

    def test_hex_length(self):
        h = _canonical_hash({"key": "value"})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_empty_dict(self):
        h = _canonical_hash({})
        assert len(h) == 64


# ---------------------------------------------------------------------------
# _mask_secrets_json_schema
# ---------------------------------------------------------------------------


class TestMaskSecretsJsonSchema:
    def test_masks_secret_value(self):
        schema = {
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "format": "password", "writeOnly": True}
            },
        }
        current = {"api_key": "super-secret-12345"}
        result = _mask_secrets_json_schema(schema, current)
        assert result == {"api_key": "***"}

    def test_empty_secret_becomes_empty_string(self):
        schema = {
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "format": "password", "writeOnly": True}
            },
        }
        result = _mask_secrets_json_schema(schema, {"api_key": ""})
        assert result == {"api_key": ""}

    def test_starred_secret_stays_empty(self):
        schema = {
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "format": "password", "writeOnly": True}
            },
        }
        result = _mask_secrets_json_schema(schema, {"api_key": "***"})
        assert result == {"api_key": ""}

    def test_non_secret_passthrough(self):
        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "port": {"type": "integer"},
            },
        }
        current = {"host": "example.com", "port": 8080}
        result = _mask_secrets_json_schema(schema, current)
        assert result == {"host": "example.com", "port": 8080}

    def test_missing_key_defaults_to_empty_string(self):
        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
            },
        }
        result = _mask_secrets_json_schema(schema, {})
        assert result == {"host": ""}

    def test_nested_object_properties(self):
        schema = {
            "type": "object",
            "properties": {
                "db": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "password": {
                            "type": "string",
                            "format": "password",
                            "writeOnly": True,
                        },
                    },
                }
            },
        }
        current = {"db": {"host": "db.example.com", "password": "secret-db-pass"}}
        result = _mask_secrets_json_schema(schema, current)
        assert result == {"db": {"host": "db.example.com", "password": "***"}}

    def test_resolves_ref_for_secrets(self):
        schema = {
            "type": "object",
            "$defs": {
                "secretDef": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                }
            },
            "properties": {
                "token": {"$ref": "#/$defs/secretDef"},
            },
        }
        result = _mask_secrets_json_schema(schema, {"token": "my-token"})
        assert result == {"token": "***"}

    def test_nested_object_with_ref(self):
        schema = {
            "type": "object",
            "$defs": {
                "dbSettings": {
                    "type": "object",
                    "properties": {
                        "password": {
                            "type": "string",
                            "format": "password",
                            "writeOnly": True,
                        },
                        "host": {"type": "string"},
                    },
                }
            },
            "properties": {
                "db": {"$ref": "#/$defs/dbSettings"},
            },
        }
        result = _mask_secrets_json_schema(
            schema, {"db": {"host": "db.example.com", "password": "pw123"}}
        )
        assert result == {"db": {"host": "db.example.com", "password": "***"}}


# ---------------------------------------------------------------------------
# _mask_secrets (top-level dispatcher)
# ---------------------------------------------------------------------------


class TestMaskSecrets:
    def test_json_schema_delegates(self):
        schema = {
            "type": "object",
            "properties": {
                "secret": {"type": "string", "format": "password", "writeOnly": True},
            },
        }
        result = _mask_secrets(schema, {"secret": "hide-me"})
        assert result == {"secret": "***"}

    def test_flat_dict_passthrough(self):
        schema = {"key": "value"}
        current = {"key": "the-value"}
        result = _mask_secrets(schema, current)
        assert result == {"key": "the-value"}


# ---------------------------------------------------------------------------
# _strip_secret_values
# ---------------------------------------------------------------------------


class TestStripSecretValues:
    def test_strips_secret_leaves(self):
        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "password": {"type": "string", "format": "password", "writeOnly": True},
            },
        }
        values = {"host": "example.com", "password": "should-be-removed"}
        result = _strip_secret_values(schema, values)
        assert result == {"host": "example.com"}

    def test_nested_object_strips_inner_secrets(self):
        schema = {
            "type": "object",
            "properties": {
                "db": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "password": {
                            "type": "string",
                            "format": "password",
                            "writeOnly": True,
                        },
                    },
                }
            },
        }
        values = {"db": {"host": "db.example.com", "password": "pw123"}}
        result = _strip_secret_values(schema, values)
        assert result == {"db": {"host": "db.example.com"}}

    def test_no_schema_returns_values(self):
        result = _strip_secret_values(None, {"a": 1})
        assert result == {"a": 1}

    def test_flat_schema_returns_values_unchanged(self):
        result = _strip_secret_values({"key": "val"}, {"key": "unchanged"})
        assert result == {"key": "unchanged"}

    def test_non_dict_values_returns_empty(self):
        result = _strip_secret_values(
            {"type": "object", "properties": {}}, "not_a_dict"
        )
        assert result == {}

    def test_resolves_ref_for_nested_secrets(self):
        schema = {
            "type": "object",
            "$defs": {
                "credDef": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                }
            },
            "properties": {
                "api_key": {"$ref": "#/$defs/credDef"},
                "host": {"type": "string"},
            },
        }
        values = {"api_key": "key123", "host": "example.com"}
        result = _strip_secret_values(schema, values)
        assert result == {"host": "example.com"}


# ---------------------------------------------------------------------------
# _merge_config (top-level dispatcher)
# ---------------------------------------------------------------------------


class TestMergeConfig:
    def test_delegates_to_json_schema_path(self):
        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
                "port": {"type": "integer", "default": 8080},
            },
        }
        existing = {"host": "old.example.com", "port": 3000}
        submitted = {"host": "new.example.com"}
        result = _merge_config(schema, existing, submitted)
        # submitted wins for host, existing wins for unset port (prefer_existing_for_unset=False → default)
        assert result == {"host": "new.example.com", "port": 8080}

    def test_delegates_to_flat_path(self):
        template = {"host": "localhost", "port": "8080"}
        existing = {"host": "old.example.com", "port": "3000"}
        submitted = {"host": "new.example.com"}
        result = _merge_config(template, existing, submitted)
        assert result == {"host": "new.example.com", "port": "8080"}

    def test_prefer_existing_for_unset_json_schema(self):
        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
                "port": {"type": "integer", "default": 8080},
            },
        }
        existing = {"host": "old.example.com", "port": 3000}
        submitted: dict[str, object] = {}
        result = _merge_config(
            schema, existing, submitted, prefer_existing_for_unset=True
        )
        assert result == {"host": "old.example.com", "port": 3000}

    def test_submitted_none_treated_as_unset(self):
        """A null from a cleared form field must not 422 integer coercion."""
        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
                "port": {"type": "integer", "default": 8080},
                "workers": {"type": "integer"},
            },
        }
        existing = {"host": "old.example.com", "port": 3000}
        submitted = {"host": "new.example.com", "port": None, "workers": None}
        result = _merge_config(schema, existing, submitted)
        # port: None → default (prefer_existing_for_unset=False);
        # workers: None, no default, not in existing → stays None
        assert result == {"host": "new.example.com", "port": 8080, "workers": None}

    def test_submitted_none_prefers_existing_when_flagged(self):
        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
                "port": {"type": "integer", "default": 8080},
            },
        }
        existing = {"host": "old.example.com", "port": 3000}
        submitted = {"host": "new.example.com", "port": None}
        result = _merge_config(
            schema, existing, submitted, prefer_existing_for_unset=True
        )
        assert result == {"host": "new.example.com", "port": 3000}

    def test_prefer_existing_for_unset_flat(self):
        template = {"host": "localhost", "port": "8080"}
        existing = {"host": "old.example.com", "port": "3000"}
        submitted: dict[str, object] = {}
        result = _merge_config(
            template, existing, submitted, prefer_existing_for_unset=True
        )
        assert result == {"host": "old.example.com", "port": "3000"}


# ---------------------------------------------------------------------------
# _merge_config_json_schema
# ---------------------------------------------------------------------------


class TestMergeConfigJsonSchema:
    def test_simple_merge(self):
        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
                "port": {"type": "integer", "default": 8080},
            },
        }
        existing = {"host": "old.example.com", "port": 3000}
        submitted = {"host": "new.example.com"}
        result = _merge_config_json_schema(schema, existing, submitted)
        assert result == {"host": "new.example.com", "port": 8080}

    def test_sentinel_preserves_existing_secret(self):
        schema = {
            "type": "object",
            "properties": {
                "api_key": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                },
                "host": {"type": "string", "default": "localhost"},
            },
        }
        existing = {"api_key": "real-key", "host": "old.example.com"}
        submitted = {"api_key": "***", "host": "new.example.com"}
        result = _merge_config_json_schema(schema, existing, submitted)
        assert result == {"api_key": "real-key", "host": "new.example.com"}

    def test_falls_back_to_default_when_unset(self):
        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
                "port": {"type": "integer", "default": 8080},
            },
        }
        existing: dict[str, object] = {}
        submitted: dict[str, object] = {}
        result = _merge_config_json_schema(schema, existing, submitted)
        assert result == {"host": "localhost", "port": 8080}

    def test_empty_string_when_no_default(self):
        schema = {
            "type": "object",
            "properties": {
                "optional": {"type": "string"},
            },
        }
        result = _merge_config_json_schema(schema, {}, {})
        assert result == {"optional": ""}

    def test_type_coercion_on_submitted(self):
        schema = {
            "type": "object",
            "properties": {
                "port": {"type": "integer", "default": 8080},
            },
        }
        result = _merge_config_json_schema(schema, {}, {"port": "3000"})
        assert result == {"port": 3000}

    def test_coercion_error_propagates(self):
        schema = {
            "type": "object",
            "properties": {
                "port": {"type": "integer"},
            },
        }
        with pytest.raises(ValueError, match="expected integer"):
            _merge_config_json_schema(schema, {}, {"port": "abc"})

    def test_prefer_existing_for_unset(self):
        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
                "port": {"type": "integer", "default": 8080},
            },
        }
        existing = {"host": "kept.example.com", "port": 9999}
        submitted: dict[str, object] = {}
        result = _merge_config_json_schema(
            schema, existing, submitted, prefer_existing_for_unset=True
        )
        assert result == {"host": "kept.example.com", "port": 9999}

    def test_nested_object_merge(self):
        schema = {
            "type": "object",
            "properties": {
                "db": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string", "default": "db.local"},
                        "port": {"type": "integer", "default": 5432},
                    },
                }
            },
        }
        existing = {"db": {"host": "db.example.com"}}
        submitted = {"db": {"port": "5555"}}
        result = _merge_config_json_schema(schema, existing, submitted)
        assert result == {"db": {"host": "db.local", "port": 5555}}

    def test_resolves_ref(self):
        schema = {
            "type": "object",
            "$defs": {
                "secretDef": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                }
            },
            "properties": {
                "token": {"$ref": "#/$defs/secretDef"},
                "host": {"type": "string", "default": "localhost"},
            },
        }
        existing = {"token": "real-token", "host": "old.example.com"}
        submitted = {"token": "***", "host": "new.example.com"}
        result = _merge_config_json_schema(schema, existing, submitted)
        assert result == {"token": "real-token", "host": "new.example.com"}


# ---------------------------------------------------------------------------
# _merge_config_flat
# ---------------------------------------------------------------------------


class TestMergeConfigFlat:
    def test_simple_flat_merge(self):
        template = {"host": "localhost", "port": "8080"}
        existing = {"host": "old.example.com", "port": "3000"}
        submitted = {"host": "new.example.com"}
        result = _merge_config_flat(template, existing, submitted)
        assert result == {"host": "new.example.com", "port": "8080"}

    def test_nested_dict_merge(self):
        template = {"db": {"host": "localhost", "port": "5432"}}
        existing = {"db": {"host": "db.example.com"}}
        submitted = {"db": {"port": "5555"}}
        result = _merge_config_flat(template, existing, submitted)
        assert result == {"db": {"host": "localhost", "port": "5555"}}

    def test_prefer_existing_for_unset(self):
        template = {"host": "localhost", "port": "8080"}
        existing = {"host": "kept.example.com", "port": "9999"}
        submitted: dict[str, object] = {}
        result = _merge_config_flat(
            template, existing, submitted, prefer_existing_for_unset=True
        )
        assert result == {"host": "kept.example.com", "port": "9999"}

    def test_falls_back_to_template_when_unset(self):
        template = {"host": "localhost"}
        existing: dict[str, object] = {}
        submitted: dict[str, object] = {}
        result = _merge_config_flat(template, existing, submitted)
        assert result == {"host": "localhost"}

    def test_array_of_objects_merge(self):
        template = {
            "items": [{"name": "default", "value": ""}],
        }
        existing = {
            "items": [{"name": "item1", "value": "old-val"}],
        }
        submitted = {
            "items": [{"name": "item1", "value": "new-val"}],
        }
        result = _merge_config_flat(template, existing, submitted)
        assert result == {"items": [{"name": "item1", "value": "new-val"}]}

    def test_array_of_objects_multi_item(self):
        template = {
            "items": [{"name": "", "value": ""}],
        }
        existing: dict[str, object] = {}
        submitted = {
            "items": [
                {"name": "a", "value": "1"},
                {"name": "b", "value": "2"},
            ],
        }
        result = _merge_config_flat(template, existing, submitted)
        assert result == {
            "items": [
                {"name": "a", "value": "1"},
                {"name": "b", "value": "2"},
            ],
        }

    def test_array_non_dict_item_passthrough(self):
        template = {
            "tags": ["default"],
        }
        existing: dict[str, object] = {}
        submitted = {"tags": ["alpha", "beta"]}
        result = _merge_config_flat(template, existing, submitted)
        assert result == {"tags": ["alpha", "beta"]}

    def test_non_dict_template_value_passthrough(self):
        template = {"version": "1.0.0"}
        existing: dict[str, object] = {}
        submitted: dict[str, object] = {}
        result = _merge_config_flat(template, existing, submitted)
        assert result == {"version": "1.0.0"}


# ---------------------------------------------------------------------------
# Nested $ref resolution (two $ref levels deep, mirroring chat's memory.llm)
# ---------------------------------------------------------------------------

# Pydantic-style schema: memory -> $defs/MemoryConfig, whose llm property is
# itself a $ref to $defs/LlmConfig. $defs only exists at the ROOT, so any
# walker resolving refs against the current sub-schema fails at level 2.
_NESTED_REF_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "memory": {"$ref": "#/$defs/MemoryConfig"},
    },
    "$defs": {
        "MemoryConfig": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "default": False},
                "llm": {"$ref": "#/$defs/LlmConfig"},
            },
        },
        "LlmConfig": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string", "default": ""},
                "api_key": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                },
            },
        },
    },
}


class TestNestedRefResolution:
    """Regression tests for the 2026-07-18 chat outage (second clobber).

    A partial update touching memory.llm replaced the nested object
    wholesale because the level-2 $ref failed to resolve against the
    sub-schema, dropping the unsubmitted nested secret.
    """

    def test_merge_preserves_nested_secret_behind_double_ref(self):
        existing = {
            "memory": {
                "enabled": True,
                "llm": {"endpoint": "https://api.example.com", "api_key": "sk-real"},
            },
        }
        submitted = {"memory": {"llm": {"endpoint": "https://other.example.com"}}}
        result = _merge_config(
            _NESTED_REF_SCHEMA, existing, submitted, prefer_existing_for_unset=True
        )
        assert result["memory"]["llm"]["endpoint"] == "https://other.example.com"
        assert result["memory"]["llm"]["api_key"] == "sk-real"
        assert result["memory"]["enabled"] is True

    def test_merge_recurses_into_double_ref_object(self):
        # Without prefer_existing_for_unset the nested object must still be
        # MERGED per-key (defaults for absent keys), never replaced by the
        # submitted dict verbatim.
        submitted = {"memory": {"llm": {"endpoint": "https://x.example.com"}}}
        result = _merge_config(_NESTED_REF_SCHEMA, {}, submitted)
        assert result["memory"]["llm"]["endpoint"] == "https://x.example.com"
        # Secret key present (empty), not missing from the dict entirely.
        assert "api_key" in result["memory"]["llm"]

    def test_mask_secrets_masks_secret_behind_double_ref(self):
        current = {
            "memory": {
                "enabled": True,
                "llm": {"endpoint": "https://api.example.com", "api_key": "sk-real"},
            },
        }
        masked = _mask_secrets(_NESTED_REF_SCHEMA, current)
        assert masked["memory"]["llm"]["api_key"] == "***"
        assert masked["memory"]["llm"]["endpoint"] == "https://api.example.com"

    def test_strip_secret_values_strips_secret_behind_double_ref(self):
        values = {
            "memory": {
                "enabled": True,
                "llm": {"endpoint": "https://api.example.com", "api_key": "sk-seed"},
            },
        }
        stripped = _strip_secret_values(_NESTED_REF_SCHEMA, values)
        assert "api_key" not in stripped["memory"]["llm"]
        assert stripped["memory"]["llm"]["endpoint"] == "https://api.example.com"


# ---------------------------------------------------------------------------
# _is_key_secret
# ---------------------------------------------------------------------------


class TestIsKeySecret:
    def test_top_level_secret_key(self):
        schema = {
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "format": "password", "writeOnly": True},
                "host": {"type": "string"},
            },
        }
        assert _is_key_secret(schema, "api_key") is True

    def test_top_level_non_secret_key(self):
        schema = {
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "format": "password", "writeOnly": True},
                "host": {"type": "string"},
            },
        }
        assert _is_key_secret(schema, "host") is False

    def test_nested_secret_key(self):
        schema = {
            "type": "object",
            "properties": {
                "db": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "password": {
                            "type": "string",
                            "format": "password",
                            "writeOnly": True,
                        },
                    },
                }
            },
        }
        assert _is_key_secret(schema, "db.password") is True

    def test_nested_non_secret_key(self):
        schema = {
            "type": "object",
            "properties": {
                "db": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "password": {
                            "type": "string",
                            "format": "password",
                            "writeOnly": True,
                        },
                    },
                }
            },
        }
        assert _is_key_secret(schema, "db.host") is False

    def test_array_item_secret_key(self):
        schema = {
            "type": "object",
            "properties": {
                "accounts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "password": {
                                "type": "string",
                                "format": "password",
                                "writeOnly": True,
                            },
                        },
                    },
                }
            },
        }
        assert _is_key_secret(schema, "accounts.0.password") is True

    def test_array_item_non_secret_key(self):
        schema = {
            "type": "object",
            "properties": {
                "accounts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "password": {
                                "type": "string",
                                "format": "password",
                                "writeOnly": True,
                            },
                        },
                    },
                }
            },
        }
        assert _is_key_secret(schema, "accounts.0.name") is False

    def test_unknown_key_returns_false(self):
        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
            },
        }
        assert _is_key_secret(schema, "nonexistent") is False

    def test_ref_resolved_secret_key(self):
        schema = {
            "type": "object",
            "$defs": {
                "secretDef": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                }
            },
            "properties": {
                "token": {"$ref": "#/$defs/secretDef"},
            },
        }
        assert _is_key_secret(schema, "token") is True

    def test_flat_schema_always_returns_false(self):
        assert _is_key_secret({"key": "value"}, "key") is False


# ---------------------------------------------------------------------------
# _restore_secrets_from_current
# ---------------------------------------------------------------------------


class TestRestoreSecretsFromCurrent:
    def test_restores_secret_value_from_current(self):
        schema = {
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "format": "password", "writeOnly": True},
                "host": {"type": "string"},
            },
        }
        restored = {"api_key": "", "host": "new.example.com"}
        current = {"api_key": "real-secret", "host": "old.example.com"}
        result = _restore_secrets_from_current(schema, restored, current)
        assert result["api_key"] == "real-secret"
        assert result["host"] == "new.example.com"

    def test_nested_secret_restored(self):
        schema = {
            "type": "object",
            "properties": {
                "db": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "password": {
                            "type": "string",
                            "format": "password",
                            "writeOnly": True,
                        },
                    },
                }
            },
        }
        restored = {"db": {"host": "new-db.example.com", "password": ""}}
        current = {"db": {"host": "old-db.example.com", "password": "real-db-pass"}}
        result = _restore_secrets_from_current(schema, restored, current)
        assert result["db"]["password"] == "real-db-pass"
        assert result["db"]["host"] == "new-db.example.com"

    def test_restored_missing_secret_added_from_current(self):
        schema = {
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "format": "password", "writeOnly": True},
                "host": {"type": "string"},
            },
        }
        restored = {"host": "new.example.com"}  # api_key missing in snapshot
        current = {"api_key": "real-secret", "host": "old.example.com"}
        result = _restore_secrets_from_current(schema, restored, current)
        # api_key was missing from snapshot but exists in current — it is added
        assert result["api_key"] == "real-secret"
        assert result["host"] == "new.example.com"

    def test_array_of_objects_secret_restored(self):
        schema = {
            "type": "object",
            "properties": {
                "accounts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "password": {
                                "type": "string",
                                "format": "password",
                                "writeOnly": True,
                            },
                        },
                    },
                }
            },
        }
        restored = {
            "accounts": [
                {"name": "alice", "password": ""},
                {"name": "bob", "password": ""},
            ]
        }
        current = {
            "accounts": [
                {"name": "alice", "password": "alice-pass"},
                {"name": "bob", "password": "bob-pass"},
            ]
        }
        result = _restore_secrets_from_current(schema, restored, current)
        assert result["accounts"][0]["password"] == "alice-pass"
        assert result["accounts"][0]["name"] == "alice"
        assert result["accounts"][1]["password"] == "bob-pass"
        assert result["accounts"][1]["name"] == "bob"

    def test_flat_schema_passthrough(self):
        schema = {"key": "value"}
        restored = {"key": "restored-val"}
        current = {"key": "current-val"}
        result = _restore_secrets_from_current(schema, restored, current)
        assert result["key"] == "restored-val"

    def test_ref_resolved_secret_restored(self):
        schema = {
            "type": "object",
            "$defs": {
                "secretDef": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                }
            },
            "properties": {
                "token": {"$ref": "#/$defs/secretDef"},
                "host": {"type": "string"},
            },
        }
        restored = {"token": "", "host": "new.example.com"}
        current = {"token": "real-token", "host": "old.example.com"}
        result = _restore_secrets_from_current(schema, restored, current)
        assert result["token"] == "real-token"
        assert result["host"] == "new.example.com"
