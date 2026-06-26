import pytest
from pydantic import ValidationError

from robotsix_central_deploy.registry import (
    ComponentConfig,
    HealthCheck,
    PortMapping,
    VolumeMount,
)


class TestPortMapping:
    def test_defaults(self):
        p = PortMapping(host=8080, container=8080)
        assert p.protocol == "tcp"

    def test_udp(self):
        p = PortMapping(host=53, container=53, protocol="udp")
        assert p.protocol == "udp"


class TestVolumeMount:
    def test_defaults(self):
        m = VolumeMount(host="/data", container="/app/data")
        assert m.read_only is False


class TestHealthCheck:
    def test_defaults(self):
        hc = HealthCheck(test=["CMD", "curl", "-f", "http://localhost/"])
        assert hc.interval_seconds == 30
        assert hc.timeout_seconds == 10
        assert hc.retries == 3
        assert hc.start_period_seconds == 10


class TestComponentConfig:
    def test_minimal_valid(self):
        c = ComponentConfig(id="my-svc", image="repo:latest", container_name="my-svc")
        assert c.ports == []
        assert c.mounts == []
        assert c.env == {}
        assert c.health_check is None

    def test_full(self):
        c = ComponentConfig(
            id="my-svc",
            image="repo:v1",
            container_name="my-svc",
            ports=[{"host": 8080, "container": 8080}],
            mounts=[{"host": "/data", "container": "/app"}],
            env={"FOO": "bar"},
            health_check={"test": ["CMD", "curl", "http://localhost/"]},
        )
        assert len(c.ports) == 1
        assert c.env["FOO"] == "bar"

    @pytest.mark.parametrize("bad_id", ["-start", "CamelCase", "", "has space"])
    def test_invalid_id_rejected(self, bad_id):
        with pytest.raises(ValidationError):
            ComponentConfig(id=bad_id, image="repo:latest", container_name="c")
