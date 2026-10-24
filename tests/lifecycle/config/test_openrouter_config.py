"""Unit tests for the canonical OpenRouter credential-block reader."""

from __future__ import annotations

from robotsix_central_deploy.lifecycle._openrouter_config import (
    extract_openrouter_keys,
)


class TestExtractOpenrouterKeys:
    def test_reads_canonical_block(self) -> None:
        assert extract_openrouter_keys(
            {"openrouter": {"keys": {"a": "sk-or-1", "b": "sk-or-2"}}}
        ) == {"a": "sk-or-1", "b": "sk-or-2"}

    def test_missing_block_returns_empty(self) -> None:
        assert extract_openrouter_keys({}) == {}
        assert extract_openrouter_keys({"openrouter": None}) == {}
        assert extract_openrouter_keys({"openrouter": {}}) == {}

    def test_empty_keys_are_dropped(self) -> None:
        assert extract_openrouter_keys(
            {"openrouter": {"keys": {"a": "", "b": "sk-or-2"}}}
        ) == {"b": "sk-or-2"}

    def test_non_string_keys_are_dropped(self) -> None:
        """A malformed value is unconfigured, not coerced into a credential."""
        assert extract_openrouter_keys(
            {"openrouter": {"keys": {"a": {"nested": "x"}, "b": 42, "c": "sk-or-3"}}}
        ) == {"c": "sk-or-3"}

    def test_no_fallback_to_pre_standard_shapes(self) -> None:
        """chat's `llmio_api_key` / mill's `secrets.openrouter_api_key` are ignored.

        The no-fallback rule is deliberate: an unmigrated component reports no
        keys, which is visible, rather than silently working through a legacy
        shape nobody maintains.
        """
        assert extract_openrouter_keys({"llmio_api_key": "sk-or-legacy"}) == {}
        assert (
            extract_openrouter_keys({"secrets": {"openrouter_api_key": "sk-or-legacy"}})
            == {}
        )

    def test_keys_not_a_dict_returns_empty(self) -> None:
        assert extract_openrouter_keys({"openrouter": {"keys": ["a", "b"]}}) == {}
