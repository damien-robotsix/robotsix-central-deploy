"""Integration tests for onboard namespace-spec-volumes."""

from __future__ import annotations


from robotsix_central_deploy.lifecycle.models import (
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.onboard.models import DerivedSpec, SiblingDerivedSpec
from robotsix_central_deploy.registry.models import (
    VolumeMount,
)

# Import the server module itself (not just symbols) so we can set its globals.
from robotsix_central_deploy.lifecycle import server as server_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_store(*names: str, image: str = "", deployed_digest: str = "") -> None:
    """Populate the server's store with records for testing."""
    s = server_mod.app.state.store
    assert s is not None
    for name in names:
        rec = ServiceRecord(
            name=name, state=ServiceState.STOPPED, image=image or f"{name}:latest"
        )
        if deployed_digest:
            rec.deployed_image_digest = deployed_digest
        await s.put(rec)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------


class TestNamespaceSpecVolumes:
    """Unit tests for the volume-namespacing helper."""

    def test_renames_primary_volume_mounts(self):
        spec = DerivedSpec.model_construct(
            name="test-svc",
            git_url="https://github.com/org/test.git",
            image="ghcr.io/org/test:main",
            ports=[],
            volume_mounts=[
                VolumeMount(host="auto-mail-config", container="/config"),
                VolumeMount(host="auto-mail-data", container="/data"),
            ],
            env={},
            claude_mount=False,
            config_volume="auto-mail-config",
            siblings=[],
        )
        result = server_mod._namespace_spec_volumes(spec, "mail")

        assert result.volume_mounts[0].host == "mail-auto-mail-config"
        assert result.volume_mounts[0].container == "/config"
        assert result.volume_mounts[1].host == "mail-auto-mail-data"
        assert result.volume_mounts[1].container == "/data"
        assert result.config_volume == "mail-auto-mail-config"

    def test_config_volume_none_is_preserved(self):
        spec = DerivedSpec.model_construct(
            name="test-svc",
            git_url="https://github.com/org/test.git",
            image="ghcr.io/org/test:main",
            ports=[],
            volume_mounts=[VolumeMount(host="vol1", container="/vol1")],
            env={},
            claude_mount=False,
            config_volume=None,
            siblings=[],
        )
        result = server_mod._namespace_spec_volumes(spec, "mail")
        assert result.config_volume is None

    def test_renames_sibling_volume_mounts(self):
        spec = DerivedSpec.model_construct(
            name="test-svc",
            git_url="https://github.com/org/test.git",
            image="ghcr.io/org/test:main",
            ports=[],
            volume_mounts=[VolumeMount(host="shared-vol", container="/shared")],
            env={},
            claude_mount=False,
            siblings=[
                SiblingDerivedSpec.model_construct(
                    service_key="worker",
                    container_name="worker",
                    image="ghcr.io/org/worker:main",
                    mounts=[
                        VolumeMount(host="worker-data", container="/data"),
                    ],
                ),
                SiblingDerivedSpec.model_construct(
                    service_key="cache",
                    container_name="cache",
                    image="ghcr.io/org/cache:main",
                    mounts=[
                        VolumeMount(host="cache-data", container="/cache"),
                    ],
                ),
            ],
        )
        result = server_mod._namespace_spec_volumes(spec, "zzztest")

        assert result.volume_mounts[0].host == "zzztest-shared-vol"
        assert result.siblings[0].mounts[0].host == "zzztest-worker-data"
        assert result.siblings[1].mounts[0].host == "zzztest-cache-data"

    def test_second_component_gets_different_names(self):
        """Same image onboarded twice produces disjoint volume names."""
        spec = DerivedSpec.model_construct(
            name="test-svc",
            git_url="https://github.com/org/test.git",
            image="ghcr.io/org/test:main",
            ports=[],
            volume_mounts=[
                VolumeMount(host="auto-mail-config", container="/config"),
                VolumeMount(host="auto-mail-data", container="/data"),
                VolumeMount(host="auto-mail-logs", container="/logs"),
            ],
            env={},
            claude_mount=False,
            config_volume="auto-mail-config",
            siblings=[],
        )
        mail_result = server_mod._namespace_spec_volumes(spec, "mail")
        zzz_result = server_mod._namespace_spec_volumes(spec, "zzztest")

        mail_hosts = {m.host for m in mail_result.volume_mounts}
        zzz_hosts = {m.host for m in zzz_result.volume_mounts}
        assert mail_hosts == {
            "mail-auto-mail-config",
            "mail-auto-mail-data",
            "mail-auto-mail-logs",
        }
        assert zzz_hosts == {
            "zzztest-auto-mail-config",
            "zzztest-auto-mail-data",
            "zzztest-auto-mail-logs",
        }
        assert mail_hosts.isdisjoint(zzz_hosts)


# ---------------------------------------------------------------------------
# GET /chat/components
# ---------------------------------------------------------------------------
