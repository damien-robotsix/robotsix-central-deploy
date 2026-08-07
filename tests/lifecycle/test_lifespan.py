"""Unit tests for lifespan helpers.

- ``_parse_self_contract_settings`` — YAML contract → SystemSettings extraction.
- ``_seed_ovh_website_credentials`` — OVH SFTP credential seeding into EnvStore.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from robotsix_central_deploy.lifecycle.config import LifecycleConfig, OvhSftpConfig
from robotsix_central_deploy.lifecycle.deps.lifespan import (
    _parse_self_contract_settings,
    _seed_ovh_website_credentials,
)
from robotsix_central_deploy.registry.env_store import EnvStore
from robotsix_central_deploy.registry.secret_key import SecretKeyManager


# ---------------------------------------------------------------------------
# _parse_self_contract_settings
# ---------------------------------------------------------------------------


class TestParseSelfContractSettings:
    """Direct unit tests for ``_parse_self_contract_settings``."""

    def _make_config(self, contract_path: Path) -> LifecycleConfig:
        """Return a LifecycleConfig pointing at *contract_path*."""
        return LifecycleConfig(self_contract_path=str(contract_path))  # type: ignore[call-arg]

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        cfg = self._make_config(tmp_path / "nonexistent.yml")
        assert _parse_self_contract_settings(cfg) is None

    def test_yaml_parse_error_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yml"
        path.write_text(": invalid yaml: :", encoding="utf-8")
        cfg = self._make_config(path)
        assert _parse_self_contract_settings(cfg) is None

    def test_non_mapping_root_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "list.yml"
        path.write_text("- item: 1\n", encoding="utf-8")
        cfg = self._make_config(path)
        assert _parse_self_contract_settings(cfg) is None

    def test_empty_services_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "empty_svc.yml"
        path.write_text("services: {}\n", encoding="utf-8")
        cfg = self._make_config(path)
        assert _parse_self_contract_settings(cfg) is None

    def test_services_not_a_dict_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "list_svc.yml"
        path.write_text("services:\n  - not-a-dict\n", encoding="utf-8")
        cfg = self._make_config(path)
        assert _parse_self_contract_settings(cfg) is None

    def test_primary_service_selection(self, tmp_path: Path) -> None:
        path = tmp_path / "primary.yml"
        doc = {
            "services": {
                "aux": {
                    "labels": {
                        "robotsix.deploy.settings.log-level": "DEBUG",
                    },
                },
                "main": {
                    "labels": {
                        "robotsix.deploy.primary": "true",
                        "robotsix.deploy.settings.log-level": "WARNING",
                    },
                },
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        cfg = self._make_config(path)
        result = _parse_self_contract_settings(cfg)
        assert result is not None
        assert result.log_level == "WARNING"

    def test_fallback_to_first_service(self, tmp_path: Path) -> None:
        path = tmp_path / "no_primary.yml"
        doc = {
            "services": {
                "first": {
                    "labels": {
                        "robotsix.deploy.settings.log-level": "ERROR",
                    },
                },
                "second": {
                    "labels": {
                        "robotsix.deploy.settings.log-level": "DEBUG",
                    },
                },
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        cfg = self._make_config(path)
        result = _parse_self_contract_settings(cfg)
        assert result is not None
        assert result.log_level == "ERROR"

    def test_primary_label_not_on_a_dict_service(self, tmp_path: Path) -> None:
        """If a service value is not a dict, primary detection skips it.

        Falls back to ``next(iter(services))`` — when that first service
        is not a mapping either, the function returns None.
        """
        path = tmp_path / "primitive_svc.yml"
        doc = {
            "services": {
                "bad": "just-a-string",
                "good": {
                    "labels": {
                        "robotsix.deploy.settings.log-level": "INFO",
                    },
                },
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        cfg = self._make_config(path)
        result = _parse_self_contract_settings(cfg)
        assert result is None

    def test_service_not_a_mapping_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "scalar_svc.yml"
        doc = {"services": {"only": "not-a-mapping"}}
        path.write_text(yaml.dump(doc), encoding="utf-8")
        cfg = self._make_config(path)
        assert _parse_self_contract_settings(cfg) is None

    # -- Type coercion tests --------------------------------------------------

    def test_str_settings(self, tmp_path: Path) -> None:
        path = tmp_path / "str.yml"
        doc = {
            "services": {
                "srv": {
                    "labels": {
                        "robotsix.deploy.settings.auth-username": "op",
                        "robotsix.deploy.settings.auth-password": "secret",
                        "robotsix.deploy.settings.log-level": "DEBUG",
                        "robotsix.deploy.settings.gateway-base-domain": "deploy.example.com",
                        "robotsix.deploy.settings.mill-component-id": "mill-01",
                    },
                }
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        result = _parse_self_contract_settings(self._make_config(path))
        assert result is not None
        assert result.auth_username == "op"
        assert result.auth_password == "secret"
        assert result.log_level == "DEBUG"
        assert result.gateway_base_domain == "deploy.example.com"
        assert result.mill_component_id == "mill-01"

    def test_int_settings(self, tmp_path: Path) -> None:
        path = tmp_path / "int.yml"
        doc = {
            "services": {
                "srv": {
                    "labels": {
                        "robotsix.deploy.settings.registry-check-interval": "120",
                        "robotsix.deploy.settings.caretaker-interval-hours": "48",
                        "robotsix.deploy.settings.claude-auth-refresh-interval": "3600",
                        "robotsix.deploy.settings.rate-limit-login-per-minute": "5",
                        "robotsix.deploy.settings.rate-limit-api-per-hour": "1000",
                        "robotsix.deploy.settings.rate-limit-login-max-attempts": "3",
                        "robotsix.deploy.settings.rate-limit-login-lockout-seconds": "600",
                        "robotsix.deploy.settings.volume-audit-interval-seconds": "7200",
                        "robotsix.deploy.settings.volume-audit-min-delta-bytes": "52428800",
                    },
                }
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        result = _parse_self_contract_settings(self._make_config(path))
        assert result is not None
        assert result.registry_check_interval == 120
        assert result.caretaker_interval_hours == 48
        assert result.claude_auth_refresh_interval == 3600
        assert result.rate_limit_login_per_minute == 5
        assert result.rate_limit_api_per_hour == 1000
        assert result.rate_limit_login_max_attempts == 3
        assert result.rate_limit_login_lockout_seconds == 600
        assert result.volume_audit_interval_seconds == 7200
        assert result.volume_audit_min_delta_bytes == 52428800

    def test_invalid_int_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "bad_int.yml"
        doc = {
            "services": {
                "srv": {
                    "labels": {
                        "robotsix.deploy.settings.registry-check-interval": "not-a-number",
                        "robotsix.deploy.settings.log-level": "INFO",
                    },
                }
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        result = _parse_self_contract_settings(self._make_config(path))
        assert result is not None
        # The invalid int is skipped; only log_level is set.
        assert result.registry_check_interval == 300  # default
        assert result.log_level == "INFO"

    def test_float_settings(self, tmp_path: Path) -> None:
        path = tmp_path / "float.yml"
        doc = {
            "services": {
                "srv": {
                    "labels": {
                        "robotsix.deploy.settings.disk-warn-pct": "25.5",
                        "robotsix.deploy.settings.volume-audit-growth-threshold-pct": "15.0",
                    },
                }
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        result = _parse_self_contract_settings(self._make_config(path))
        assert result is not None
        assert result.disk_warn_pct == 25.5
        assert result.volume_audit_growth_threshold_pct == 15.0

    def test_invalid_float_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "bad_float.yml"
        doc = {
            "services": {
                "srv": {
                    "labels": {
                        "robotsix.deploy.settings.disk-warn-pct": "twelve",
                        "robotsix.deploy.settings.log-level": "WARNING",
                    },
                }
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        result = _parse_self_contract_settings(self._make_config(path))
        assert result is not None
        assert result.disk_warn_pct == 10.0  # default
        assert result.log_level == "WARNING"

    def test_bool_settings(self, tmp_path: Path) -> None:
        path = tmp_path / "bool.yml"
        doc = {
            "services": {
                "srv": {
                    "labels": {
                        "robotsix.deploy.settings.caretaker-enabled": "true",
                        "robotsix.deploy.settings.image-auto-prune": "1",
                        "robotsix.deploy.settings.volume-audit-enabled": "yes",
                        "robotsix.deploy.settings.chat-agent-registration-enabled": "false",
                    },
                }
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        result = _parse_self_contract_settings(self._make_config(path))
        assert result is not None
        assert result.caretaker_enabled is True
        assert result.image_auto_prune is True
        assert result.volume_audit_enabled is True
        assert result.chat_agent_registration_enabled is False

    def test_json_setting(self, tmp_path: Path) -> None:
        path = tmp_path / "json.yml"
        tier_config = {"tiers": {"free": 10, "pro": 100}}
        doc = {
            "services": {
                "srv": {
                    "labels": {
                        "robotsix.deploy.settings.llmio-tier-config": json.dumps(
                            tier_config
                        ),
                    },
                }
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        result = _parse_self_contract_settings(self._make_config(path))
        assert result is not None
        assert result.llmio_tier_config == tier_config

    def test_invalid_json_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "bad_json.yml"
        doc = {
            "services": {
                "srv": {
                    "labels": {
                        "robotsix.deploy.settings.llmio-tier-config": "{not json",
                        "robotsix.deploy.settings.log-level": "CRITICAL",
                    },
                }
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        result = _parse_self_contract_settings(self._make_config(path))
        assert result is not None
        assert result.llmio_tier_config == {}  # default
        assert result.log_level == "CRITICAL"

    def test_unknown_label_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "unknown.yml"
        doc = {
            "services": {
                "srv": {
                    "labels": {
                        "robotsix.deploy.settings.some-future-field": "hello",
                        "robotsix.deploy.settings.log-level": "ERROR",
                    },
                }
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        result = _parse_self_contract_settings(self._make_config(path))
        assert result is not None
        assert result.log_level == "ERROR"
        # Unknown field not in SystemSettings, but the function still
        # returns the known fields.

    def test_empty_settings_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "no_labels.yml"
        doc = {"services": {"srv": {"labels": {"some.other.label": "value"}}}}
        path.write_text(yaml.dump(doc), encoding="utf-8")
        result = _parse_self_contract_settings(self._make_config(path))
        assert result is None

    def test_labels_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "no_labels.yml"
        doc = {"services": {"srv": {"image": "nginx"}}}
        path.write_text(yaml.dump(doc), encoding="utf-8")
        result = _parse_self_contract_settings(self._make_config(path))
        assert result is None

    def test_systemsettings_construction_failure(self, tmp_path: Path) -> None:
        """When SystemSettings(**kwargs) raises, the function returns None."""
        path = tmp_path / "bad_log.yml"
        doc = {
            "services": {
                "srv": {
                    "labels": {
                        "robotsix.deploy.settings.log-level": "INVALID_LEVEL",
                    },
                }
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        result = _parse_self_contract_settings(self._make_config(path))
        assert result is None

    def test_full_integration_multiple_types(self, tmp_path: Path) -> None:
        """Several settings of different types extracted together."""
        tier_config = {"free": 5}
        path = tmp_path / "full.yml"
        doc = {
            "services": {
                "srv": {
                    "labels": {
                        "robotsix.deploy.settings.auth-username": "admin",
                        "robotsix.deploy.settings.log-level": "WARNING",
                        "robotsix.deploy.settings.gateway-base-domain": "deploy.example.com",
                        "robotsix.deploy.settings.registry-check-interval": "60",
                        "robotsix.deploy.settings.disk-warn-pct": "20.0",
                        "robotsix.deploy.settings.caretaker-enabled": "true",
                        "robotsix.deploy.settings.llmio-tier-config": json.dumps(
                            tier_config
                        ),
                        "robotsix.deploy.settings.volume-audit-interval-seconds": "1800",
                        "robotsix.deploy.settings.volume-audit-growth-threshold-pct": "5.5",
                    },
                }
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        result = _parse_self_contract_settings(self._make_config(path))
        assert result is not None
        assert result.auth_username == "admin"
        assert result.log_level == "WARNING"
        assert result.gateway_base_domain == "deploy.example.com"
        assert result.registry_check_interval == 60
        assert result.disk_warn_pct == 20.0
        assert result.caretaker_enabled is True
        assert result.llmio_tier_config == tier_config
        assert result.volume_audit_interval_seconds == 1800
        assert result.volume_audit_growth_threshold_pct == 5.5

    def test_primary_fallback_when_primary_is_scalar(self, tmp_path: Path) -> None:
        """When a service with primary label is not a dict, skip it."""
        path = tmp_path / "primary_scalar.yml"
        doc = {
            "services": {
                "bad": "scalar",
                "good": {
                    "labels": {
                        "robotsix.deploy.settings.log-level": "DEBUG",
                    },
                },
            }
        }
        path.write_text(yaml.dump(doc), encoding="utf-8")
        result = _parse_self_contract_settings(self._make_config(path))
        # "bad" is first, not a dict → returns None
        assert result is None


# ---------------------------------------------------------------------------
# _seed_ovh_website_credentials
# ---------------------------------------------------------------------------


class TestSeedOvhWebsiteCredentials:
    """Unit tests for ``_seed_ovh_website_credentials``."""

    def _make_env_store(self, tmp_path: Path) -> EnvStore:
        km = SecretKeyManager(tmp_path / "secrets.key")
        return EnvStore(tmp_path / "env.json", km)

    def _make_config(
        self,
        host: str = "sftp.example.com",
        port: int = 22,
        user: str = "ovh-user",
        password: str = "s3cret",  # noqa: S107
    ) -> LifecycleConfig:
        return LifecycleConfig(  # type: ignore[call-arg]
            ovh_sftp=OvhSftpConfig(
                host=host,
                port=port,
                user=user,
                password=SecretStr(password),
            )
        )

    @pytest.mark.asyncio
    async def test_not_fully_configured_early_return(self, tmp_path: Path) -> None:
        """When any field is empty, nothing is seeded."""
        cfg = self._make_config(host="")
        store = self._make_env_store(tmp_path)
        await _seed_ovh_website_credentials(store, cfg)
        existing = await store.get("ovh-website-credentials")
        assert existing.env == {}
        assert existing.secret_tokens == {}

    @pytest.mark.asyncio
    async def test_seeds_credentials_on_first_boot(self, tmp_path: Path) -> None:
        """First call with full config seeds env + secret tokens."""
        cfg = self._make_config()
        store = self._make_env_store(tmp_path)
        await _seed_ovh_website_credentials(store, cfg)
        existing = await store.get("ovh-website-credentials")
        assert existing.env == {
            "OVH_SFTP_HOST": "sftp.example.com",
            "OVH_SFTP_PORT": "22",
            "OVH_SFTP_USER": "ovh-user",
        }
        assert "OVH_SFTP_PASSWORD" in existing.secret_tokens
        assert existing.env_scopes == {
            "OVH_SFTP_HOST": "website:ovh",
            "OVH_SFTP_PORT": "website:ovh",
            "OVH_SFTP_USER": "website:ovh",
        }
        assert existing.secret_scopes == {"OVH_SFTP_PASSWORD": "website:ovh"}

    @pytest.mark.asyncio
    async def test_idempotent_does_not_overwrite(self, tmp_path: Path) -> None:
        """Second call with different values does not overwrite."""
        cfg1 = self._make_config(password="first-pw")
        store = self._make_env_store(tmp_path)
        await _seed_ovh_website_credentials(store, cfg1)
        first = await store.get("ovh-website-credentials")

        # Second call with different password
        cfg2 = self._make_config(password="second-pw")
        await _seed_ovh_website_credentials(store, cfg2)
        second = await store.get("ovh-website-credentials")

        # Values unchanged from first seeding.
        assert second.env == first.env
        assert second.secret_tokens == first.secret_tokens

    @pytest.mark.asyncio
    async def test_already_seeded_with_env_entries_is_skipped(
        self, tmp_path: Path
    ) -> None:
        """When the entry already has env entries, seeding is skipped."""
        cfg = self._make_config()
        store = self._make_env_store(tmp_path)

        # Pre-seed with different values
        await store.upsert(
            "ovh-website-credentials",
            env={"OVH_SFTP_HOST": "pre-existing.example.com", "OVH_SFTP_PORT": "2222"},
            secrets={"OVH_SFTP_PASSWORD": "pre-existing-pw"},
            env_scopes={
                "OVH_SFTP_HOST": "website:ovh",
                "OVH_SFTP_PORT": "website:ovh",
            },
            secret_scopes={"OVH_SFTP_PASSWORD": "website:ovh"},
        )

        await _seed_ovh_website_credentials(store, cfg)
        existing = await store.get("ovh-website-credentials")
        assert existing.env["OVH_SFTP_HOST"] == "pre-existing.example.com"

    @pytest.mark.asyncio
    async def test_port_stringified_correctly(self, tmp_path: Path) -> None:
        """Port is stored as its string representation."""
        cfg = self._make_config(port=2222)
        store = self._make_env_store(tmp_path)
        await _seed_ovh_website_credentials(store, cfg)
        existing = await store.get("ovh-website-credentials")
        assert existing.env["OVH_SFTP_PORT"] == "2222"

    @pytest.mark.asyncio
    async def test_partial_config_missing_user(self, tmp_path: Path) -> None:
        """User empty triggers early return."""
        cfg = self._make_config(user="")
        store = self._make_env_store(tmp_path)
        await _seed_ovh_website_credentials(store, cfg)
        existing = await store.get("ovh-website-credentials")
        assert existing.env == {}
        assert existing.secret_tokens == {}

    @pytest.mark.asyncio
    async def test_partial_config_missing_password(self, tmp_path: Path) -> None:
        """Password empty triggers early return."""
        cfg = self._make_config(password="")
        store = self._make_env_store(tmp_path)
        await _seed_ovh_website_credentials(store, cfg)
        existing = await store.get("ovh-website-credentials")
        assert existing.env == {}
        assert existing.secret_tokens == {}
