"""Tests for the shared service-name suggestion matcher."""

from __future__ import annotations

from robotsix_central_deploy.lifecycle._name_suggest import (
    MAX_SUGGESTIONS,
    suggest_names,
    with_suggestions,
)


class TestSuggestNames:
    def test_repo_name_finds_the_registered_short_name(self) -> None:
        """The reported case: `robotsix-invest` registered as `invest`.

        The pair shares no prefix, so difflib alone scores it below any usable
        cutoff — the substring pass is what makes it work.
        """
        assert suggest_names("robotsix-invest", ["invest", "chat", "mill"]) == [
            "invest"
        ]

    def test_short_name_finds_the_repo_name(self) -> None:
        """Matching is symmetric — either side may be the longer string."""
        assert suggest_names("invest", ["robotsix-invest", "chat"]) == [
            "robotsix-invest"
        ]

    def test_typo_is_caught_by_ratio(self) -> None:
        assert suggest_names("cost-moniter", ["cost-monitor", "chat"]) == [
            "cost-monitor"
        ]

    def test_nothing_close_yields_nothing(self) -> None:
        assert suggest_names("zzzzzz", ["invest", "chat", "mill"]) == []

    def test_empty_candidate_list(self) -> None:
        assert suggest_names("invest", []) == []

    def test_blank_candidates_are_ignored(self) -> None:
        assert suggest_names("invest", ["", "invest"]) == ["invest"]

    def test_capped_and_deduplicated(self) -> None:
        known = [f"svc-{i}" for i in range(10)]
        result = suggest_names("svc-", known)
        assert len(result) == MAX_SUGGESTIONS
        assert len(set(result)) == len(result)

    def test_case_insensitive_substring(self) -> None:
        assert suggest_names("ROBOTSIX-Invest", ["invest"]) == ["invest"]


class TestWithSuggestions:
    def test_appends_the_clause(self) -> None:
        detail = with_suggestions(
            "Service 'robotsix-invest' not found", "robotsix-invest", ["invest"]
        )
        assert detail == ("Service 'robotsix-invest' not found; did you mean 'invest'?")

    def test_quotes_every_candidate(self) -> None:
        detail = with_suggestions("nope", "svc-", ["svc-a", "svc-b"])
        assert "did you mean 'svc-a', 'svc-b'?" in detail

    def test_returns_detail_unchanged_when_nothing_matches(self) -> None:
        """The caller builds the message unconditionally, so this must be safe."""
        assert with_suggestions("Service 'x' not found", "zzzzzz", ["invest"]) == (
            "Service 'x' not found"
        )
