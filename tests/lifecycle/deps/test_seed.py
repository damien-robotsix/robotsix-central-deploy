"""Direct tests for seed helpers in lifecycle.deps.seed."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from robotsix_central_deploy.lifecycle.deps.seed import (
    _build_component_config_from_spec,
    _derive_account_id,
    _namespace_spec_volumes,
    _prune_unset,
    _relocate_account_seed_values,
    _resolve_placeholders,
    _seed_for_detect,
    _seed_list_item,
    _validate_account_ids,
    _validate_config_or_422,
)
from robotsix_central_deploy.onboard.models import DerivedSpec
from robotsix_central_deploy.registry.models import (
    ConfigAssistSeed,
    VolumeMount,
)


# ---------------------------------------------------------------------------
# Helpers to build test fixtures
# ---------------------------------------------------------------------------


def _make_derived_spec(**overrides) -> DerivedSpec:
    """Minimal valid DerivedSpec with sensible defaults for testing."""
    defaults: dict = {
        "name": "test-svc",
        "git_url": "https://github.com/org/test-svc",
        "image": "ghcr.io/org/test-svc:main",
        "ports": [],
        "volume_mounts": [],
        "env": {},
        "claude_mount": False,
        "host_docker_sock": False,
        "health_check": None,
        "command": None,
        "entrypoint": None,
        "tmpfs": [],
        "mem_limit": "512m",
        "container_name": "",
        "siblings": [],
        "config_schema": None,
        "config_example_values": None,
        "config_volume": None,
        "config_assist_command": None,
        "config_assist_seeds": [],
        "llmio_tier_level": None,
        "allow_chat_access": False,
        "user": None,
    }
    defaults.update(overrides)
    return DerivedSpec(**defaults)


# ===================================================================
# _namespace_spec_volumes
# ===================================================================


class TestNamespaceSpecVolumes:
    """Tests for ``_namespace_spec_volumes`` — volume host renaming."""

    def test_primary_volume_hosts_prefixed(self) -> None:
        """Primary volume mount hosts get the component-name prefix."""
        spec = _make_derived_spec(
            name="mail",
            volume_mounts=[
                VolumeMount(host="data", container="/data"),
                VolumeMount(host="logs", container="/logs"),
            ],
        )
        result = _namespace_spec_volumes(spec, "mail")
        assert result.volume_mounts[0].host == "mail-data"
        assert result.volume_mounts[1].host == "mail-logs"

    def test_sibling_volume_hosts_prefixed(self) -> None:
        """Sibling volume mount hosts also get the component-name prefix."""
        from robotsix_central_deploy.registry.models import ServiceConfig

        spec = _make_derived_spec(
            name="mail",
            volume_mounts=[],
            siblings=[
                ServiceConfig(
                    service_key="worker",
                    container_name="mail-worker",
                    image="ghcr.io/org/mail-worker:main",
                    mounts=[VolumeMount(host="queue", container="/queue")],
                )
            ],
        )
        result = _namespace_spec_volumes(spec, "mail")
        assert result.siblings[0].mounts[0].host == "mail-queue"

    def test_config_volume_renamed_when_matching(self) -> None:
        """config_volume is renamed if it matches a renamed host."""
        spec = _make_derived_spec(
            name="mail",
            volume_mounts=[
                VolumeMount(host="auto-mail-config", container="/config"),
            ],
            config_volume="auto-mail-config",
        )
        result = _namespace_spec_volumes(spec, "mail")
        assert result.config_volume == "mail-auto-mail-config"

    def test_config_volume_unchanged_when_no_match(self) -> None:
        """config_volume is left alone when it doesn't match any renamed host."""
        spec = _make_derived_spec(
            name="mail",
            volume_mounts=[
                VolumeMount(host="data", container="/data"),
            ],
            config_volume="other-volume",
        )
        result = _namespace_spec_volumes(spec, "mail")
        assert result.config_volume == "other-volume"

    def test_config_volume_none_unchanged(self) -> None:
        """config_volume=None stays None."""
        spec = _make_derived_spec(
            name="mail",
            volume_mounts=[VolumeMount(host="data", container="/data")],
            config_volume=None,
        )
        result = _namespace_spec_volumes(spec, "mail")
        assert result.config_volume is None

    def test_empty_volumes_noop(self) -> None:
        """A spec with no volumes or siblings returns unchanged (except name)."""
        spec = _make_derived_spec(name="web")
        result = _namespace_spec_volumes(spec, "web")
        assert result.volume_mounts == []
        assert result.siblings == []


# ===================================================================
# _build_component_config_from_spec
# ===================================================================


class TestBuildComponentConfigFromSpec:
    """Tests for ``_build_component_config_from_spec``."""

    def test_basic_conversion(self) -> None:
        """A minimal DerivedSpec yields a valid ComponentConfig."""
        spec = _make_derived_spec(name="mail", image="ghcr.io/org/mail:main")
        cfg = _build_component_config_from_spec(
            spec, git_url="https://github.com/org/mail"
        )
        assert cfg.id == "mail"
        assert cfg.image == "ghcr.io/org/mail:main"
        assert cfg.container_name == "mail"  # falls back to spec.name
        assert cfg.git_url == "https://github.com/org/mail"

    def test_container_name_override(self) -> None:
        """container_name from spec overrides the default (spec.name)."""
        spec = _make_derived_spec(
            name="mail", container_name="mail-primary", image="img:latest"
        )
        cfg = _build_component_config_from_spec(spec, git_url="u")
        assert cfg.container_name == "mail-primary"

    def test_named_volumes_from_mounts(self) -> None:
        """named_volumes is derived from primary + sibling mount hosts."""
        from robotsix_central_deploy.registry.models import ServiceConfig

        spec = _make_derived_spec(
            name="mail",
            image="img:latest",
            volume_mounts=[
                VolumeMount(host="mail-data", container="/data"),
            ],
            siblings=[
                ServiceConfig(
                    service_key="w",
                    container_name="mail-w",
                    image="img:latest",
                    mounts=[VolumeMount(host="mail-queue", container="/queue")],
                )
            ],
        )
        cfg = _build_component_config_from_spec(spec, git_url="u")
        assert set(cfg.named_volumes) == {"mail-data", "mail-queue"}

    def test_overrides_applied(self) -> None:
        """**overrides are passed through to ComponentConfig."""
        spec = _make_derived_spec(name="mail", image="img:latest")
        cfg = _build_component_config_from_spec(
            spec,
            git_url="u",
            caretaker_auto_update=False,
        )
        assert cfg.caretaker_auto_update is False

    def test_config_assist_seeds_preserved(self) -> None:
        """config_assist_seeds from spec are passed through."""
        seeds = [
            ConfigAssistSeed(key="accounts.0.auth.username"),
            ConfigAssistSeed(key="accounts.0.auth.password"),
        ]
        spec = _make_derived_spec(
            name="mail", image="img:latest", config_assist_seeds=seeds
        )
        cfg = _build_component_config_from_spec(spec, git_url="u")
        assert len(cfg.config_assist_seeds) == 2
        assert cfg.config_assist_seeds[0].key == "accounts.0.auth.username"

    def test_siblings_preserved(self) -> None:
        """Siblings from spec are deep-copied into ComponentConfig."""
        from robotsix_central_deploy.registry.models import ServiceConfig

        spec = _make_derived_spec(
            name="mail",
            image="img:latest",
            siblings=[
                ServiceConfig(
                    service_key="worker",
                    container_name="mail-worker",
                    image="img:latest",
                )
            ],
        )
        cfg = _build_component_config_from_spec(spec, git_url="u")
        assert len(cfg.siblings) == 1
        assert cfg.siblings[0].service_key == "worker"


# ===================================================================
# _validate_config_or_422
# ===================================================================


class TestValidateConfigOr422:
    """Tests for ``_validate_config_or_422`` — JSON Schema validation."""

    def test_valid_config_passes(self) -> None:
        """No exception when values satisfy the schema."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "port": {"type": "integer", "minimum": 1},
            },
            "required": ["name"],
        }
        _validate_config_or_422(schema, {"name": "test", "port": 8080})

    def test_missing_required_raises_422(self) -> None:
        """HTTP 422 is raised when a required property is missing."""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        with pytest.raises(HTTPException) as exc_info:
            _validate_config_or_422(schema, {})
        assert exc_info.value.status_code == 422

    def test_wrong_type_raises_422(self) -> None:
        """HTTP 422 is raised when a value has the wrong type."""
        schema = {
            "type": "object",
            "properties": {"port": {"type": "integer"}},
        }
        with pytest.raises(HTTPException) as exc_info:
            _validate_config_or_422(schema, {"port": "not-a-number"})
        assert exc_info.value.status_code == 422

    def test_nested_error_includes_path(self) -> None:
        """Validation error detail includes the JSON path when nested."""
        schema = {
            "type": "object",
            "properties": {
                "server": {
                    "type": "object",
                    "properties": {"port": {"type": "integer"}},
                    "required": ["port"],
                }
            },
        }
        with pytest.raises(HTTPException) as exc_info:
            _validate_config_or_422(schema, {"server": {}})
        assert exc_info.value.status_code == 422
        assert "server" in exc_info.value.detail["error"]


# ===================================================================
# _validate_account_ids
# ===================================================================


class TestValidateAccountIds:
    """Tests for ``_validate_account_ids`` — account-id character check."""

    def test_valid_ids_pass(self) -> None:
        """Valid account IDs (alnum, dot, dash, underscore) pass."""
        _validate_account_ids({"accounts": [{"id": "user.name"}, {"id": "user_name"}]})

    def test_no_accounts_passes(self) -> None:
        """A dict without 'accounts' key passes."""
        _validate_account_ids({"other": "value"})

    def test_empty_accounts_passes(self) -> None:
        """An empty accounts list passes."""
        _validate_account_ids({"accounts": []})

    def test_at_sign_rejected(self) -> None:
        """An account ID containing '@' raises HTTP 422."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_account_ids({"accounts": [{"id": "user@example.com"}]})
        assert exc_info.value.status_code == 422
        assert "user@example.com" in exc_info.value.detail

    def test_space_rejected(self) -> None:
        """An account ID containing a space raises HTTP 422."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_account_ids({"accounts": [{"id": "user name"}]})
        assert exc_info.value.status_code == 422

    def test_non_dict_item_skipped(self) -> None:
        """Non-dict items in the accounts list are skipped."""
        _validate_account_ids({"accounts": ["not-a-dict", {"id": "valid"}]})

    def test_empty_id_passes(self) -> None:
        """An empty string id is skipped (falsy)."""
        _validate_account_ids({"accounts": [{"id": ""}]})


# ===================================================================
# _prune_unset
# ===================================================================


class TestPruneUnset:
    """Tests for ``_prune_unset`` — removing template-default empty fields."""

    def test_empty_string_pruned_when_absent(self) -> None:
        """Empty string is removed when key was not in existing."""
        result = _prune_unset({"name": ""}, {})
        assert "name" not in result

    def test_empty_string_kept_when_present(self) -> None:
        """Empty string is kept when key was in existing."""
        result = _prune_unset({"name": ""}, {"name": "old"})
        assert result == {"name": ""}

    def test_none_pruned_when_absent(self) -> None:
        """None is removed when key was not in existing."""
        result = _prune_unset({"name": None}, {})
        assert "name" not in result

    def test_non_empty_scalar_kept(self) -> None:
        """Non-empty scalar values are always kept."""
        result = _prune_unset({"port": 8080, "enabled": False}, {})
        assert result == {"port": 8080, "enabled": False}

    def test_nested_dict_pruned_when_empty(self) -> None:
        """Nested dict that becomes fully empty is removed."""
        result = _prune_unset({"server": {"host": "", "port": None}}, {})
        assert "server" not in result

    def test_nested_dict_kept_when_non_empty(self) -> None:
        """Nested dict with at least one non-empty value is kept."""
        result = _prune_unset({"server": {"host": "", "port": 8080}}, {})
        assert result == {"server": {"port": 8080}}

    def test_nested_dict_kept_when_key_in_existing(self) -> None:
        """Nested dict is kept (even empty) when key was in existing."""
        result = _prune_unset({"server": {"host": ""}}, {"server": {"host": "old"}})
        assert "server" in result

    def test_list_of_dicts_recursed(self) -> None:
        """List-of-dicts items are recursively pruned (empty dicts kept in list)."""
        result = _prune_unset(
            {"accounts": [{"name": "", "id": "abc"}, {"name": ""}]},
            {},
        )
        # First item keeps "id" only; second item prunes to empty dict.
        assert result == {"accounts": [{"id": "abc"}, {}]}

    def test_list_of_dicts_with_existing_indices(self) -> None:
        """Existing list items provide fallback for pruning decisions."""
        result = _prune_unset(
            {"accounts": [{"name": ""}, {"name": ""}]},
            {"accounts": [{"name": "keep-me"}, {}]},
        )
        # First item: name="" but present in existing → kept.
        # Second item: name="" and absent in existing → pruned to empty dict.
        assert result == {"accounts": [{"name": ""}, {}]}


# ===================================================================
# _seed_list_item
# ===================================================================


class TestSeedListItem:
    """Tests for ``_seed_list_item`` — list-item branch of _seed_for_detect."""

    def test_dict_list_seeded(self) -> None:
        """A list-of-dicts template seeds each submitted item."""
        tval = [{"name": "", "port": 0}]
        val = [{"name": "web", "port": 8080}, {"name": "db", "port": 5432}]
        result = _seed_list_item(tval, val, [])
        assert len(result) == 2
        assert result[0] == {"name": "web", "port": 8080}
        assert result[1] == {"name": "db", "port": 5432}

    def test_non_dict_list_returns_sentinel(self) -> None:
        """When tval is not a list of dicts, returns _SEED_LIST_NO_MATCH."""
        tval = ["a", "b"]
        val = ["x"]
        from robotsix_central_deploy.lifecycle.deps.seed import _SEED_LIST_NO_MATCH

        result = _seed_list_item(tval, val, [])
        assert result is _SEED_LIST_NO_MATCH

    def test_empty_template_list_returns_sentinel(self) -> None:
        """Empty tval list returns sentinel."""
        val = ["x"]
        from robotsix_central_deploy.lifecycle.deps.seed import _SEED_LIST_NO_MATCH

        result = _seed_list_item([], val, [])
        assert result is _SEED_LIST_NO_MATCH

    def test_existing_values_used_for_secrets(self) -> None:
        """Existing values are used for '***' sentinel in list items."""
        tval = [{"password": ""}]
        val = [{"password": "***"}]
        ex_val = [{"password": "secret123"}]
        result = _seed_list_item(tval, val, ex_val)
        assert result[0] == {"password": "secret123"}

    def test_non_dict_items_preserved(self) -> None:
        """Non-dict items in val are kept as-is."""
        tval = [{"key": ""}]
        val = ["plain-string", {"key": "value"}]
        result = _seed_list_item(tval, val, [])
        assert result == ["plain-string", {"key": "value"}]


# ===================================================================
# _seed_for_detect
# ===================================================================


class TestSeedForDetect:
    """Tests for ``_seed_for_detect`` — building sparse seed config."""

    def test_empty_submitted_returns_empty(self) -> None:
        """Empty submitted dict returns empty dict."""
        result = _seed_for_detect({}, {}, {})
        assert result == {}

    def test_empty_string_skipped(self) -> None:
        """Empty string (template default) is skipped."""
        result = _seed_for_detect({"name": ""}, {}, {"name": ""})
        assert "name" not in result

    def test_secret_sentinel_uses_existing(self) -> None:
        """'***' sentinel is replaced with existing value."""
        result = _seed_for_detect({}, {"password": "s3cret"}, {"password": "***"})
        assert result == {"password": "s3cret"}

    def test_secret_sentinel_no_existing_uses_empty(self) -> None:
        """'***' sentinel with no existing value becomes empty string."""
        result = _seed_for_detect({}, {}, {"password": "***"})
        assert result == {"password": ""}

    def test_regular_string_kept(self) -> None:
        """A regular non-empty string is kept as-is."""
        result = _seed_for_detect({}, {}, {"name": "my-app"})
        assert result == {"name": "my-app"}

    def test_nested_dict_recursed(self) -> None:
        """Nested dict values are recursively seeded."""
        template = {"server": {"host": "", "port": 0}}
        existing = {"server": {"host": "old-host"}}
        submitted = {"server": {"host": "new-host", "port": 3000}}
        result = _seed_for_detect(template, existing, submitted)
        assert result == {"server": {"host": "new-host", "port": 3000}}

    def test_nested_dict_empty_result_omitted(self) -> None:
        """A nested dict that seeds to empty is omitted."""
        result = _seed_for_detect(
            {"server": {"host": ""}}, {}, {"server": {"host": ""}}
        )
        assert "server" not in result

    def test_list_value_handled(self) -> None:
        """List values are passed to _seed_list_item."""
        template = {"accounts": [{"name": "", "id": ""}]}
        submitted = {"accounts": [{"name": "alice", "id": "a1"}]}
        result = _seed_for_detect(template, {}, submitted)
        assert "accounts" in result
        assert result["accounts"] == [{"name": "alice", "id": "a1"}]

    def test_bool_int_float_kept(self) -> None:
        """Non-string scalar values are kept as-is."""
        result = _seed_for_detect({}, {}, {"enabled": True, "count": 5, "ratio": 1.5})
        assert result == {"enabled": True, "count": 5, "ratio": 1.5}

    def test_key_not_in_template_still_seeded(self) -> None:
        """Keys absent from template are still included from submitted."""
        result = _seed_for_detect({}, {}, {"extra": "value"})
        assert result == {"extra": "value"}


# ===================================================================
# _derive_account_id
# ===================================================================


class TestDeriveAccountId:
    """Tests for ``_derive_account_id`` — slug-based account-id derivation."""

    def test_from_username_seed(self) -> None:
        """Derives account ID from a username seed value."""
        seeds = [ConfigAssistSeed(key="accounts.0.auth.username")]
        partial = {"accounts": [{"auth": {"username": "Alice"}}]}
        result = _derive_account_id(seeds, partial, 0)
        assert result == "alice"

    def test_from_email_seed(self) -> None:
        """Derives account ID from an email seed value."""
        seeds = [ConfigAssistSeed(key="accounts.0.auth.email")]
        partial = {"accounts": [{"auth": {"email": "Alice@Example.com"}}]}
        result = _derive_account_id(seeds, partial, 0)
        # Slugification: lower + non-alnum → '-'
        assert result == "alice-example-com"

    def test_slug_truncated_to_40_chars(self) -> None:
        """Derived slug is truncated to 40 characters."""
        seeds = [ConfigAssistSeed(key="accounts.0.auth.username")]
        long_name = "a" * 60
        partial = {"accounts": [{"auth": {"username": long_name}}]}
        result = _derive_account_id(seeds, partial, 0)
        assert len(result) == 40
        assert result == "a" * 40

    def test_fallback_when_no_matching_seed(self) -> None:
        """Falls back to 'accounts-{n}' when no username/email seed exists."""
        seeds = [ConfigAssistSeed(key="accounts.0.some.other.field")]
        partial = {"accounts": [{"some": {"other": {"field": "x"}}}]}
        result = _derive_account_id(seeds, partial, 0)
        assert result == "accounts-0"

    def test_fallback_when_value_missing(self) -> None:
        """Falls back when the navigated value is missing."""
        seeds = [ConfigAssistSeed(key="accounts.0.auth.username")]
        partial = {}
        result = _derive_account_id(seeds, partial, 0)
        assert result == "accounts-0"

    def test_uses_index_n_not_zero(self) -> None:
        """The seed key's '0' index is replaced with the given n."""
        seeds = [ConfigAssistSeed(key="accounts.0.auth.username")]
        partial = {"accounts": [{}, {"auth": {"username": "Bob"}}]}
        result = _derive_account_id(seeds, partial, 1)
        assert result == "bob"

    def test_special_chars_slugified(self) -> None:
        """Special characters are replaced with dashes."""
        seeds = [ConfigAssistSeed(key="accounts.0.auth.username")]
        partial = {"accounts": [{"auth": {"username": "hello world!"}}]}
        result = _derive_account_id(seeds, partial, 0)
        assert result == "hello-world"


# ===================================================================
# _resolve_placeholders
# ===================================================================


class TestResolvePlaceholders:
    """Tests for ``_resolve_placeholders`` — placeholder substitution."""

    def test_simple_placeholder(self) -> None:
        """A simple {key} placeholder is resolved."""
        result = _resolve_placeholders("hello {name}", {"name": "world"})
        assert result == "hello world"

    def test_nested_path(self) -> None:
        """A dotted path {a.b.c} is resolved recursively."""
        result = _resolve_placeholders(
            "{accounts.0.auth.username}",
            {"accounts": [{"auth": {"username": "alice"}}]},
        )
        assert result == "alice"

    def test_unresolved_left_as_is(self) -> None:
        """An unresolvable placeholder is left unchanged."""
        result = _resolve_placeholders("{missing}", {})
        assert result == "{missing}"

    def test_partial_path_unresolved(self) -> None:
        """A path where an intermediate key is missing is left unchanged."""
        result = _resolve_placeholders("{a.b.c}", {"a": {}})
        assert result == "{a.b.c}"

    def test_int_value_stringified(self) -> None:
        """Integer values are converted to strings."""
        result = _resolve_placeholders("{port}", {"port": 8080})
        assert result == "8080"

    def test_bool_value_stringified(self) -> None:
        """Boolean values are converted to strings."""
        result = _resolve_placeholders("{flag}", {"flag": True})
        assert result == "True"

    def test_list_index_out_of_range_unresolved(self) -> None:
        """List index out of range leaves placeholder unchanged."""
        result = _resolve_placeholders("{items.5}", {"items": [1, 2]})
        assert result == "{items.5}"

    def test_list_index_not_int_unresolved(self) -> None:
        """Non-integer list index leaves placeholder unchanged."""
        result = _resolve_placeholders("{items.x}", {"items": [1, 2]})
        assert result == "{items.x}"

    def test_multiple_placeholders(self) -> None:
        """Multiple placeholders in one string are all resolved."""
        result = _resolve_placeholders(
            "{greeting} {name}!", {"greeting": "Hello", "name": "World"}
        )
        assert result == "Hello World!"

    def test_no_placeholders_returns_original(self) -> None:
        """A string with no placeholders is returned unchanged."""
        result = _resolve_placeholders("no placeholders here", {})
        assert result == "no placeholders here"

    def test_float_value_stringified(self) -> None:
        """Float values are converted to strings."""
        result = _resolve_placeholders("{ratio}", {"ratio": 3.14})
        assert result == "3.14"


# ===================================================================
# _relocate_account_seed_values
# ===================================================================


class TestRelocateAccountSeedValues:
    """Tests for ``_relocate_account_seed_values`` — moving seed values."""

    def test_basic_relocation(self) -> None:
        """A seed value is moved from src_idx to dst_idx."""
        values = {"accounts": [{"auth": {"username": "alice"}}, {}]}
        seeds = [ConfigAssistSeed(key="accounts.0.auth.username")]
        _relocate_account_seed_values(values, seeds, src_idx=0, dst_idx=1)
        assert "username" not in values["accounts"][0]["auth"]
        assert values["accounts"][1]["auth"]["username"] == "alice"

    def test_secret_sentinel_not_relocated(self) -> None:
        """'***' sentinel values are NOT relocated."""
        values = {"accounts": [{"auth": {"password": "***"}}, {}]}
        seeds = [ConfigAssistSeed(key="accounts.0.auth.password")]
        _relocate_account_seed_values(values, seeds, src_idx=0, dst_idx=1)
        # Should still be at source, not moved.
        assert values["accounts"][0]["auth"]["password"] == "***"
        assert "password" not in values["accounts"][1].get("auth", {})

    def test_destination_already_has_value_skipped(self) -> None:
        """When destination already has a non-empty value, skip the move."""
        values = {
            "accounts": [
                {"auth": {"username": "alice"}},
                {"auth": {"username": "bob"}},
            ]
        }
        seeds = [ConfigAssistSeed(key="accounts.0.auth.username")]
        _relocate_account_seed_values(values, seeds, src_idx=0, dst_idx=1)
        # Destination still has "bob" — not overwritten.
        assert values["accounts"][0]["auth"]["username"] == "alice"
        assert values["accounts"][1]["auth"]["username"] == "bob"

    def test_seed_not_matching_source_skipped(self) -> None:
        """Seeds not targeting src_idx are skipped."""
        values = {"accounts": [{"auth": {"username": "alice"}}, {}]}
        seeds = [ConfigAssistSeed(key="accounts.1.auth.username")]
        _relocate_account_seed_values(values, seeds, src_idx=0, dst_idx=1)
        assert values["accounts"][0]["auth"]["username"] == "alice"

    def test_accounts_list_padded(self) -> None:
        """The accounts list is padded when indices are out of range."""
        values: dict = {}
        seeds = [ConfigAssistSeed(key="accounts.0.auth.username")]
        _relocate_account_seed_values(values, seeds, src_idx=0, dst_idx=3)
        assert "accounts" in values
        assert len(values["accounts"]) == 4
