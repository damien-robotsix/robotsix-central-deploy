"""Tests for onboard parser and fetcher."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from robotsix_central_deploy.onboard.fetcher import FetchError, fetch_compose_bytes
from robotsix_central_deploy.onboard.models import DerivedSpec, ParseError
from robotsix_central_deploy.onboard.parser import _parse_go_duration, parse_compose
from robotsix_central_deploy.registry.models import PortMapping, VolumeMount

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

    def test_two_services(self):
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
        assert "exactly one service" in str(exc.value).lower()

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


# ---------------------------------------------------------------------------
# parse_compose — healthcheck
# ---------------------------------------------------------------------------

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
# fetch_compose_bytes
# ---------------------------------------------------------------------------

class TestFetchComposeBytes:
    def test_non_https_url(self):
        with pytest.raises(FetchError, match="only https://"):
            fetch_compose_bytes("file:///etc/passwd")

    def test_git_clone_failure(self):
        with mock.patch("subprocess.run") as m_run:
            m_run.return_value = mock.Mock(
                returncode=128,
                stderr=b"fatal: repository 'https://example.com/fake.git' not found\n",
            )
            with pytest.raises(FetchError, match="git clone failed"):
                fetch_compose_bytes("https://example.com/fake.git")

    def test_clone_success_no_compose_file(self):
        with mock.patch("subprocess.run") as m_run:
            m_run.return_value = mock.Mock(returncode=0, stderr=b"")
            with mock.patch(
                "robotsix_central_deploy.onboard.fetcher.tempfile.TemporaryDirectory"
            ) as m_tmp:
                m_tmp.return_value.__enter__.return_value = "/tmp/fake_dir"
                with mock.patch.object(Path, "is_file", return_value=False):
                    with pytest.raises(FetchError, match="docker-compose.yml not found"):
                        fetch_compose_bytes("https://example.com/fake.git")
