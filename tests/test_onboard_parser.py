"""Tests for onboard parser and fetcher."""

from __future__ import annotations

import pytest

from robotsix_central_deploy.onboard.models import (
    DerivedSpec,
    ParseError,
)
from robotsix_central_deploy.onboard.parser import _parse_go_duration, parse_compose
from robotsix_central_deploy.registry.models import (
    ConfigAssistSeed,
    PortMapping,
    VolumeMount,
)

# ---------------------------------------------------------------------------
# Valid compose
# ---------------------------------------------------------------------------

VALID_COMPOSE_YAML = """\
# central-deploy-contract-version: 1
services:
  cost-monitor:
    image: ghcr.io/damien-robotsix/cost-monitor:main
    labels:
      robotsix.deploy.claude-mount: "true"
    ports:
      - "8200:8200"
    volumes:
      - cost-data:/data
    environment:
      - OPENAI_API_KEY=
      - DATABASE_URL=
      - DEBUG=false
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8200/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
volumes:
  cost-data:
    labels:
      robotsix.deploy.stateful: "true"
"""


def _bytes(yaml_str: str) -> bytes:
    return yaml_str.encode("utf-8")


# ---------------------------------------------------------------------------
# _parse_go_duration
# ---------------------------------------------------------------------------


class TestGoDuration:
    def test_seconds_only(self):
        assert _parse_go_duration("30s") == 30

    def test_minutes_seconds(self):
        assert _parse_go_duration("1m30s") == 90

    def test_hours(self):
        assert _parse_go_duration("2h") == 7200

    def test_ms_ignored(self):
        assert _parse_go_duration("500ms") == 0

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _parse_go_duration("abc")


# ---------------------------------------------------------------------------
# parse_compose — valid
# ---------------------------------------------------------------------------


class TestParseComposeValid:
    def test_valid_contract(self):
        spec = parse_compose(
            _bytes(VALID_COMPOSE_YAML),
            name="cost-monitor",
            git_url="https://github.com/example/repo.git",
        )
        assert isinstance(spec, DerivedSpec)
        assert spec.name == "cost-monitor"
        assert spec.git_url == "https://github.com/example/repo.git"
        assert spec.image == "ghcr.io/damien-robotsix/cost-monitor:main"
        assert spec.claude_mount is True
        assert spec.stateful_volumes == ["cost-data"]
        assert spec.env == {"OPENAI_API_KEY": "", "DATABASE_URL": "", "DEBUG": "false"}
        assert spec.ports == [PortMapping(host=8200, container=8200, protocol="tcp")]
        assert spec.volume_mounts == [
            VolumeMount(host="cost-data", container="/data", read_only=False)
        ]
        assert spec.health_check is not None
        assert spec.health_check.test == [
            "CMD",
            "curl",
            "-f",
            "http://localhost:8200/health",
        ]
        assert spec.health_check.interval_seconds == 30
        assert spec.health_check.timeout_seconds == 10
        assert spec.health_check.retries == 3
        assert spec.health_check.start_period_seconds == 15


# ---------------------------------------------------------------------------
# Multi-service compose fixtures
# ---------------------------------------------------------------------------

TWO_SERVICE_COMPOSE_YAML = """\
# central-deploy-contract-version: 1
services:
  board:
    image: ghcr.io/damien-robotsix/auto-mail:main
    labels:
      robotsix.deploy.primary: "true"
    ports:
      - "8202:8080"
    environment:
      SMTP_HOST: ""
      AUTH_TOKEN: ""
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
  ingester:
    image: ghcr.io/damien-robotsix/auto-mail-ingester:main
    environment:
      BOARD_URL: ""
      IMAP_PASSWORD: ""
    volumes:
      - mail-spool:/data
volumes:
  mail-spool:
    labels:
      robotsix.deploy.stateful: "true"
"""

# ---------------------------------------------------------------------------
# parse_compose — invalid
# ---------------------------------------------------------------------------


class TestParseComposeInvalid:
    def test_missing_header(self):
        y = """\
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert "central-deploy-contract-version" in str(exc.value)

    def test_two_services_no_primary(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
  bar:
    image: ghcr.io/damien-robotsix/bar:main
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert "robotsix.deploy.primary" in str(exc.value).lower()

    def test_build_key_present(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    build: .
    image: ghcr.io/damien-robotsix/foo:main
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert "build:" in str(exc.value)

    def test_bind_mount_relative(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    volumes:
      - ./data:/data
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert "bind-mount" in str(exc.value)

    def test_bind_mount_absolute(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    volumes:
      - /host/path:/data
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert "bind-mount" in str(exc.value)

    def test_missing_image(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    ports:
      - "8080:8080"
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert "image:" in str(exc.value)

    def test_invalid_yaml(self):
        y = b"not: valid: yaml: ["
        with pytest.raises(ParseError) as exc:
            parse_compose(y, name="foo", git_url="https://x.com/r.git")
        assert "not valid YAML" in str(exc.value)

    def test_driver_not_local(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    volumes:
      - mydata:/data
volumes:
  mydata:
    driver: nfs
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert "driver must be 'local'" in str(exc.value)

    def test_volume_not_in_top_level(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    volumes:
      - missing-vol:/data
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert "missing-vol" in str(exc.value)
        assert "not declared in top-level volumes" in str(exc.value)

    def test_command_invalid_type(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    command: 42
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert "command:" in str(exc.value)

    def test_entrypoint_invalid_type(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    entrypoint: 42
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert "entrypoint:" in str(exc.value)


# ---------------------------------------------------------------------------
# parse_compose — labels
# ---------------------------------------------------------------------------


class TestParseComposeLabels:
    def test_claude_mount_true(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    labels:
      robotsix.deploy.claude-mount: "true"
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.claude_mount is True

    def test_claude_mount_not_present(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.claude_mount is False

    def test_host_docker_sock_true_on_sibling(self):
        """host-docker-sock label on a non-primary sibling parses to True."""
        y = """\
# central-deploy-contract-version: 1
services:
  app:
    image: ghcr.io/damien-robotsix/app:main
    labels:
      robotsix.deploy.primary: "true"
  socket-proxy:
    image: ghcr.io/damien-robotsix/socket-proxy:main
    labels:
      robotsix.deploy.host-docker-sock: "true"
"""
        spec = parse_compose(_bytes(y), name="app", git_url="https://x.com/r.git")
        assert spec.host_docker_sock is False
        assert len(spec.siblings) == 1
        assert spec.siblings[0].host_docker_sock is True

    def test_host_docker_sock_not_present(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.host_docker_sock is False

    def test_host_docker_sock_on_primary_raises(self):
        """host-docker-sock is rejected on the primary service."""
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    labels:
      robotsix.deploy.host-docker-sock: "true"
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert "robotsix.deploy.host-docker-sock" in str(exc.value)
        assert "primary" in str(exc.value).lower()

    def test_stateful_volume_label(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    volumes:
      - mydata:/data
volumes:
  mydata:
    labels:
      robotsix.deploy.stateful: "true"
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.stateful_volumes == ["mydata"]

    def test_stateful_volume_not_flagged(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    volumes:
      - mydata:/data
volumes:
  mydata: {}
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.stateful_volumes == []

    def test_config_target_resolves_volume_name(self):
        """robotsix.deploy.config-target resolves to the matching named-volume host name."""
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    labels:
      robotsix.deploy.config-target: "/home/app/config/config.yaml"
    volumes:
      - app-config:/home/app/config
volumes:
  app-config: {}
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.config_volume == "app-config"

    def test_config_target_no_match_adds_violation(self):
        """config-target dirname with no matching volume mount → ParseError."""
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    labels:
      robotsix.deploy.config-target: "/home/app/config/config.yaml"
    volumes:
      - other-vol:/data
volumes:
  other-vol: {}
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert "robotsix.deploy.config-target" in str(exc.value)
        assert "no matching volume mount" in str(exc.value)
        assert "/home/app/config" in str(exc.value)

    def test_no_config_target_yields_none(self):
        """When the label is absent, config_volume is None."""
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    volumes:
      - app-config:/app/config
volumes:
  app-config: {}
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.config_volume is None

    def test_config_target_with_subdir_path(self):
        """config-target can have deep paths; only the dirname must match the mount."""
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    labels:
      robotsix.deploy.config-target: "/etc/myapp/conf.d/config.yaml"
    volumes:
      - myapp-conf:/etc/myapp/conf.d
volumes:
  myapp-conf: {}
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.config_volume == "myapp-conf"


class TestParseComposeHealthcheck:
    def test_interval_30s(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    healthcheck:
      test: ["CMD", "echo"]
      interval: 30s
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.health_check is not None
        assert spec.health_check.interval_seconds == 30

    def test_interval_1m30s(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    healthcheck:
      test: ["CMD", "echo"]
      interval: 1m30s
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.health_check is not None
        assert spec.health_check.interval_seconds == 90

    def test_no_healthcheck(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.health_check is None


# ---------------------------------------------------------------------------
# parse_compose — environment
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# parse_compose — container_name
# ---------------------------------------------------------------------------


class TestParseComposeContainerName:
    def test_container_name_present(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    container_name: agent-comm
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.container_name == "agent-comm"

    def test_container_name_absent_defaults_empty(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.container_name == ""


# ---------------------------------------------------------------------------
# parse_compose — environment
# ---------------------------------------------------------------------------


class TestParseComposeEnv:
    def test_env_list(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    environment:
      - KEY=
      - KEY2=default
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.env == {"KEY": "", "KEY2": "default"}

    def test_env_dict_null(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    environment:
      KEY: null
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.env == {"KEY": ""}


# ---------------------------------------------------------------------------
# Multi-service parse tests
# ---------------------------------------------------------------------------


class TestMultiServiceParse:
    def test_multi_service_with_primary_label(self):
        """Two-service compose with board as primary and ingester as sibling."""
        spec = parse_compose(
            _bytes(TWO_SERVICE_COMPOSE_YAML),
            name="auto-mail",
            git_url="https://github.com/example/auto-mail.git",
        )
        assert isinstance(spec, DerivedSpec)
        assert spec.name == "auto-mail"
        assert spec.image == "ghcr.io/damien-robotsix/auto-mail:main"
        assert len(spec.siblings) == 1
        sib = spec.siblings[0]
        assert sib.service_key == "ingester"
        assert sib.container_name == "auto-mail-ingester"
        assert sib.image == "ghcr.io/damien-robotsix/auto-mail-ingester:main"
        assert sib.env == {"BOARD_URL": "", "IMAP_PASSWORD": ""}
        assert sib.volume_mounts == [
            VolumeMount(host="mail-spool", container="/data", read_only=False)
        ]
        assert spec.stateful_volumes == ["mail-spool"]
        # Primary still has its own ports
        assert spec.ports == [PortMapping(host=8202, container=8080, protocol="tcp")]
        assert spec.health_check is not None

    def test_multi_service_no_primary_raises(self):
        """2 services, neither has primary label → ParseError."""
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
  bar:
    image: ghcr.io/damien-robotsix/bar:main
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert "robotsix.deploy.primary" in str(exc.value).lower()
        assert "none found" in str(exc.value).lower()

    def test_multi_service_both_primary_raises(self):
        """2 services, both have primary label → ParseError."""
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    labels:
      robotsix.deploy.primary: "true"
  bar:
    image: ghcr.io/damien-robotsix/bar:main
    labels:
      robotsix.deploy.primary: "true"
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert "exactly one primary" in str(exc.value).lower()

    def test_single_service_implicit_primary(self):
        """Existing single-service YAML parses as before with siblings=[]."""
        spec = parse_compose(
            _bytes(VALID_COMPOSE_YAML),
            name="cost-monitor",
            git_url="https://github.com/example/repo.git",
        )
        assert isinstance(spec, DerivedSpec)
        assert spec.siblings == []
        assert spec.image == "ghcr.io/damien-robotsix/cost-monitor:main"
        assert spec.claude_mount is True

    def test_sibling_container_name_defaults(self):
        """Sibling without container_name: derives from <name>-<service_key>."""
        y = """\
# central-deploy-contract-version: 1
services:
  board:
    image: ghcr.io/damien-robotsix/myapp:main
    labels:
      robotsix.deploy.primary: "true"
  ingester:
    image: ghcr.io/damien-robotsix/myapp-ingester:main
"""
        spec = parse_compose(_bytes(y), name="myapp", git_url="https://x.com/r.git")
        assert len(spec.siblings) == 1
        assert spec.siblings[0].container_name == "myapp-ingester"

    def test_sibling_container_name_override(self):
        """Sibling with container_name: overrides the default."""
        y = """\
# central-deploy-contract-version: 1
services:
  board:
    image: ghcr.io/damien-robotsix/myapp:main
    labels:
      robotsix.deploy.primary: "true"
  ingester:
    image: ghcr.io/damien-robotsix/myapp-ingester:main
    container_name: custom-worker
"""
        spec = parse_compose(_bytes(y), name="myapp", git_url="https://x.com/r.git")
        assert len(spec.siblings) == 1
        assert spec.siblings[0].container_name == "custom-worker"

    def test_named_volumes_cross_service_validated(self):
        """Volume declared only in sibling but not in top-level volumes: → ParseError."""
        y = """\
# central-deploy-contract-version: 1
services:
  board:
    image: ghcr.io/damien-robotsix/myapp:main
    labels:
      robotsix.deploy.primary: "true"
  ingester:
    image: ghcr.io/damien-robotsix/myapp-ingester:main
    volumes:
      - undeclared-vol:/data
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="myapp", git_url="https://x.com/r.git")
        assert "undeclared-vol" in str(exc.value)
        assert "not declared in top-level volumes" in str(exc.value).lower()

    def test_multi_service_sibling_build_key_raises(self):
        """Sibling has build: field → ParseError mentioning sibling service key."""
        y = """\
# central-deploy-contract-version: 1
services:
  board:
    image: ghcr.io/damien-robotsix/myapp:main
    labels:
      robotsix.deploy.primary: "true"
  ingester:
    build: .
    image: ghcr.io/damien-robotsix/myapp-ingester:main
"""
        with pytest.raises(ParseError) as exc:
            parse_compose(_bytes(y), name="myapp", git_url="https://x.com/r.git")
        assert "build:" in str(exc.value)
        assert "ingester" in str(exc.value)

    def test_sibling_command_propagated(self):
        """Multi-service compose with command on primary and sibling."""
        y = """\
# central-deploy-contract-version: 1
services:
  board:
    image: ghcr.io/damien-robotsix/auto-mail:main
    labels:
      robotsix.deploy.primary: "true"
    command: "serve --port 8080"
  ingester:
    image: ghcr.io/damien-robotsix/auto-mail-ingester:main
    command: ["ingest", "--watch"]
"""
        spec = parse_compose(_bytes(y), name="auto-mail", git_url="https://x.com/r.git")
        assert spec.command == ["serve", "--port", "8080"]
        assert len(spec.siblings) == 1
        assert spec.siblings[0].command == ["ingest", "--watch"]


# ---------------------------------------------------------------------------
# parse_compose — command and entrypoint
# ---------------------------------------------------------------------------


class TestParseComposeCommandAndEntrypoint:
    def test_command_string_is_split(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    command: "serve --port 8080"
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.command == ["serve", "--port", "8080"]

    def test_command_list_is_kept(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    command: ["ingest", "--watch"]
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.command == ["ingest", "--watch"]

    def test_command_absent_is_none(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.command is None

    def test_entrypoint_string_is_split(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    entrypoint: "/usr/bin/env python -m app"
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.entrypoint == ["/usr/bin/env", "python", "-m", "app"]

    def test_entrypoint_absent_is_none(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.entrypoint is None

    def test_entrypoint_list_is_kept(self):
        y = """\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    entrypoint: ["python", "-m", "app"]
"""
        spec = parse_compose(_bytes(y), name="foo", git_url="https://x.com/r.git")
        assert spec.entrypoint == ["python", "-m", "app"]


# ---------------------------------------------------------------------------
# Config-assist seeds parser tests
# ---------------------------------------------------------------------------


def minimal_compose_with_label(label_key: str, label_value: str) -> bytes:
    """Build a minimal valid docker-compose YAML with a single label on the primary service."""
    return f"""\
# central-deploy-contract-version: 1
services:
  foo:
    image: ghcr.io/damien-robotsix/foo:main
    labels:
      {label_key}: "{label_value}"
""".encode("utf-8")


class TestParseComposeConfigAssistSeeds:
    def test_bare_keys(self):
        """Bare keys (no label) parse to ConfigAssistSeed with label=None."""
        compose = minimal_compose_with_label(
            "robotsix.deploy.config-assist-seeds",
            "accounts.0.auth.username,accounts.0.auth.password",
        )
        spec = parse_compose(compose, name="foo", git_url="https://x.com/r.git")
        assert spec.config_assist_seeds == [
            ConfigAssistSeed(key="accounts.0.auth.username"),
            ConfigAssistSeed(key="accounts.0.auth.password"),
        ]

    def test_with_labels(self):
        """key:label format parses correctly."""
        compose = minimal_compose_with_label(
            "robotsix.deploy.config-assist-seeds",
            "accounts.0.auth.username:Email,accounts.0.auth.password:Password",
        )
        spec = parse_compose(compose, name="foo", git_url="https://x.com/r.git")
        assert spec.config_assist_seeds == [
            ConfigAssistSeed(key="accounts.0.auth.username", label="Email"),
            ConfigAssistSeed(key="accounts.0.auth.password", label="Password"),
        ]

    def test_mixed(self):
        """Mix of bare keys and key:label entries."""
        compose = minimal_compose_with_label(
            "robotsix.deploy.config-assist-seeds",
            "accounts.0.auth.username:Email,accounts.0.auth.password",
        )
        spec = parse_compose(compose, name="foo", git_url="https://x.com/r.git")
        assert spec.config_assist_seeds == [
            ConfigAssistSeed(key="accounts.0.auth.username", label="Email"),
            ConfigAssistSeed(key="accounts.0.auth.password", label=None),
        ]

    def test_blank_label_normalises_to_none(self):
        """Trailing colon with no label text normalises to label=None."""
        compose = minimal_compose_with_label(
            "robotsix.deploy.config-assist-seeds",
            "accounts.0.auth.username:",
        )
        spec = parse_compose(compose, name="foo", git_url="https://x.com/r.git")
        assert spec.config_assist_seeds == [
            ConfigAssistSeed(key="accounts.0.auth.username", label=None),
        ]
