"""Shared default values for settings fields present in both
``SystemSettings`` and ``LifecycleConfig``.

Single source of truth — changing a default here updates both models.
This eliminates the synchronization fragility that caused a production
bug on 2026-07-05 when ``rate_limit_api_per_hour`` defaults drifted apart.
"""

from __future__ import annotations

from typing import Any, TypedDict


class _SettingsDefaults(TypedDict):
    auth_username: str
    auth_password: str
    disk_warn_pct: float
    registry_check_interval: int
    log_level: str
    gateway_base_domain: str
    caretaker_enabled: bool
    caretaker_interval_hours: int
    mill_component_id: str
    image_auto_prune: bool
    llmio_tier_config: dict[str, Any]
    claude_auth_refresh_interval: int
    rate_limit_login_per_minute: int
    rate_limit_api_per_hour: int
    rate_limit_login_max_attempts: int
    rate_limit_login_lockout_seconds: int
    volume_audit_enabled: bool
    volume_audit_interval_seconds: int
    volume_audit_growth_threshold_pct: float
    volume_audit_min_delta_bytes: int


SETTINGS_DEFAULTS: _SettingsDefaults = {
    "auth_username": "",
    "auth_password": "",
    "disk_warn_pct": 10.0,
    "registry_check_interval": 300,
    "log_level": "INFO",
    "gateway_base_domain": "",
    "caretaker_enabled": False,
    "caretaker_interval_hours": 24,
    "mill_component_id": "mill",
    "image_auto_prune": False,
    "llmio_tier_config": {},
    "claude_auth_refresh_interval": 1800,
    "rate_limit_login_per_minute": 10,
    "rate_limit_api_per_hour": 20000,
    "rate_limit_login_max_attempts": 20,
    "rate_limit_login_lockout_seconds": 300,
    "volume_audit_enabled": False,
    "volume_audit_interval_seconds": 3600,
    "volume_audit_growth_threshold_pct": 10.0,
    "volume_audit_min_delta_bytes": 10_485_760,
}
