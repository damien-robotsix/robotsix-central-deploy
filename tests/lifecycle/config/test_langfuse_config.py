"""Unit tests for the canonical Langfuse credential-block reader."""

from __future__ import annotations

from robotsix_central_deploy.lifecycle._langfuse_config import (
    _extract_langfuse_project_creds,
    extract_langfuse_block,
)


class TestNoLangfuseBlock:
    """Configs that declare no canonical block at all."""

    def test_empty_config_returns_nothing(self):
        assert extract_langfuse_block({}) == (None, [])

    def test_missing_langfuse_key_returns_nothing(self):
        assert extract_langfuse_block({"server_port": 8080}) == (None, [])

    def test_langfuse_not_a_mapping_returns_nothing(self):
        assert extract_langfuse_block({"langfuse": "https://lf"}) == (None, [])

    def test_block_without_projects_returns_host_only(self):
        host, projects = extract_langfuse_block(
            {"langfuse": {"host": "https://lf.example.com"}}
        )
        assert host == "https://lf.example.com"
        assert projects == []

    def test_projects_not_a_mapping_is_ignored(self):
        host, projects = extract_langfuse_block(
            {"langfuse": {"host": "https://lf", "projects": ["a", "b"]}}
        )
        assert host == "https://lf"
        assert projects == []


class TestHost:
    """`langfuse.host` handling."""

    def test_empty_host_is_none(self):
        host, _ = extract_langfuse_block({"langfuse": {"host": ""}})
        assert host is None

    def test_absent_host_is_none(self):
        host, _ = extract_langfuse_block({"langfuse": {"projects": {}}})
        assert host is None

    def test_non_string_host_is_none(self):
        host, _ = extract_langfuse_block({"langfuse": {"host": 1234}})
        assert host is None


class TestProjects:
    """`langfuse.projects` extraction."""

    def test_reads_a_full_project(self):
        _, projects = extract_langfuse_block(
            {
                "langfuse": {
                    "host": "https://lf",
                    "projects": {
                        "robotsix-chat": {
                            "public_key": "pk-lf-1",
                            "secret_key": "sk-lf-1",
                            "project_id": "cm-1",
                        }
                    },
                }
            }
        )
        assert len(projects) == 1
        entry = projects[0]
        assert entry.alias == "robotsix-chat"
        assert entry.public_key == "pk-lf-1"
        assert entry.secret_key == "sk-lf-1"
        assert entry.project_id == "cm-1"

    def test_project_id_is_optional(self):
        _, projects = extract_langfuse_block(
            {
                "langfuse": {
                    "projects": {
                        "robotsix-mill": {
                            "public_key": "pk",
                            "secret_key": "sk",
                        }
                    }
                }
            }
        )
        assert projects[0].project_id is None

    def test_empty_project_id_is_none(self):
        _, projects = extract_langfuse_block(
            {
                "langfuse": {
                    "projects": {
                        "p": {"public_key": "pk", "secret_key": "sk", "project_id": ""}
                    }
                }
            }
        )
        assert projects[0].project_id is None

    def test_multiple_projects_per_component(self):
        """The one-project-per-function rule means components declare several."""
        _, projects = extract_langfuse_block(
            {
                "langfuse": {
                    "projects": {
                        "robotsix-chat": {"public_key": "pk-a", "secret_key": "sk-a"},
                        "robotsix-chat-cognee": {
                            "public_key": "pk-b",
                            "secret_key": "sk-b",
                        },
                    }
                }
            }
        )
        assert {p.alias for p in projects} == {"robotsix-chat", "robotsix-chat-cognee"}

    def test_half_filled_projects_are_dropped(self):
        _, projects = extract_langfuse_block(
            {
                "langfuse": {
                    "projects": {
                        "good": {"public_key": "pk", "secret_key": "sk"},
                        "no-secret": {"public_key": "pk", "secret_key": ""},
                        "no-public": {"public_key": "", "secret_key": "sk"},
                        "both-empty": {"public_key": "", "secret_key": ""},
                        "missing-keys": {},
                    }
                }
            }
        )
        assert [p.alias for p in projects] == ["good"]

    def test_null_keys_are_dropped(self):
        _, projects = extract_langfuse_block(
            {"langfuse": {"projects": {"p": {"public_key": None, "secret_key": None}}}}
        )
        assert projects == []

    def test_non_mapping_project_entry_is_skipped(self):
        _, projects = extract_langfuse_block(
            {
                "langfuse": {
                    "projects": {
                        "bogus": "pk-and-sk",
                        "good": {"public_key": "pk", "secret_key": "sk"},
                    }
                }
            }
        )
        assert [p.alias for p in projects] == ["good"]


class TestNoBackwardCompatibility:
    """Pre-standard shapes are deliberately NOT read.

    Per the config-ownership standard there is no env-var or legacy-shape
    fallback: a component that has not migrated reports no projects, which
    is the intended visible failure rather than a silent one.
    """

    def test_flat_top_level_shape_is_ignored(self):
        assert extract_langfuse_block(
            {
                "langfuse_base_url": "https://lf",
                "langfuse_projects": {"p": {"public_key": "pk", "secret_key": "sk"}},
            }
        ) == (None, [])

    def test_flat_per_key_shape_is_ignored(self):
        """mill's pre-migration `secrets.langfuse_*` layout yields nothing."""
        assert extract_langfuse_block(
            {
                "secrets": {
                    "langfuse_public_key": "pk",
                    "langfuse_secret_key": "sk",
                    "langfuse_base_url": "https://lf",
                }
            }
        ) == (None, [])

    def test_single_project_langfuse_block_is_ignored(self):
        """chat's pre-migration `langfuse.{public_key,secret_key}` yields nothing."""
        host, projects = extract_langfuse_block(
            {
                "langfuse": {
                    "host": "https://lf",
                    "public_key": "pk",
                    "secret_key": "sk",
                }
            }
        )
        assert host == "https://lf"
        assert projects == []


class TestExtractLangfuseProjectCreds:
    """Tests for ``_extract_langfuse_project_creds``, the helper that wraps
    ``extract_langfuse_block`` and returns ``LangfuseProjectCreds`` dicts
    for the auto-discovery reconciliation path."""

    def test_returns_langfuse_project_creds_dict(self):
        creds = _extract_langfuse_project_creds(
            {
                "langfuse": {
                    "host": "https://lf.example.com",
                    "projects": {
                        "proj-a": {"public_key": "pk-a", "secret_key": "sk-a"},
                        "proj-b": {"public_key": "pk-b", "secret_key": "sk-b"},
                    },
                },
            }
        )
        assert set(creds.keys()) == {"proj-a", "proj-b"}
        assert creds["proj-a"].public_key == "pk-a"
        assert creds["proj-a"].secret_key.get_secret_value() == "sk-a"

    def test_empty_when_no_langfuse_block(self):
        assert _extract_langfuse_project_creds({}) == {}

    def test_host_is_discarded(self):
        """The creds dict does not carry host — only alias→creds."""
        creds = _extract_langfuse_project_creds(
            {
                "langfuse": {
                    "host": "https://different.example.com",
                    "projects": {
                        "p": {"public_key": "pk", "secret_key": "sk"},
                    },
                },
            }
        )
        assert "p" in creds
        assert creds["p"].public_key == "pk"
