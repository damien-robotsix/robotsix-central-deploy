"""Integration tests for onboard namespace-spec-volumes."""

from __future__ import annotations

# Import the server module itself (not just symbols) so we can set its globals.
import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.deps.seed import _namespace_spec_volumes
from robotsix_central_deploy.lifecycle.models import (
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.onboard.fetcher import RepoFiles
from robotsix_central_deploy.onboard.models import DerivedSpec, SiblingDerivedSpec
from robotsix_central_deploy.registry.models import (
    VolumeMount,
)

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
        result = _namespace_spec_volumes(spec, "mail")

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
        result = _namespace_spec_volumes(spec, "mail")
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
        result = _namespace_spec_volumes(spec, "zzztest")

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
        mail_result = _namespace_spec_volumes(spec, "mail")
        zzz_result = _namespace_spec_volumes(spec, "zzztest")

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


# ===================================================================
# TestPreflightConfigJsonValidation
# ===================================================================


class TestPreflightConfigJsonValidation:
    """Tests for config/config.json validation gates in ``onboard_preflight``.

    Validates that invalid JSON bytes and non-dict top-level values
    both raise HTTP 422 before the spec is returned to the caller.
    """

    @staticmethod
    def _mock_parse_compose(repo_bytes: bytes, name: str, git_url: str) -> DerivedSpec:
        """Return a minimal DerivedSpec — config_json validation fires before
        the config_schema/config_volume precondition check.

        .. note::

           ``_resolve_compose_backbone`` (shared with ``_resolve_deploy_contract``)
           validates the config-standard precondition inside the backbone.
           The mock must supply a ``config_volume`` so the backbone passes
           through to the per-caller validation gates under test.
        """
        return DerivedSpec.model_construct(
            name="my-svc",
            git_url="https://github.com/org/my-svc",
            image="ghcr.io/org/my-svc:main",
            ports=[],
            volume_mounts=[],
            env={},
            claude_mount=False,
            siblings=[],
            config_volume="test-config-vol",
        )

    async def test_invalid_json_in_config_json_returns_422(
        self, client, auth_headers, monkeypatch
    ):
        """POST /onboard/preflight with invalid config/config.json bytes → 422."""
        from robotsix_central_deploy.onboard import fetcher as fetcher_mod
        from robotsix_central_deploy.onboard import parser as parser_mod

        monkeypatch.setattr(
            fetcher_mod,
            "fetch_repo_files",
            lambda git_url, timeout_sec=30, github_token=None: RepoFiles(
                compose_bytes=b"services:\n  app:\n    image: img",
                config_schema_json=b'{"type": "object"}',
                config_json=b"invalid { json",
                config_json_template=None,
            ),
        )
        monkeypatch.setattr(parser_mod, "parse_compose", self._mock_parse_compose)

        resp = await client.post(
            "/onboard/preflight",
            json={
                "name": "my-svc",
                "git_url": "https://github.com/org/my-svc",
            },
            headers=auth_headers,
        )

        assert resp.status_code == 422
        body = resp.json()
        # The http_exception_handler copies dict detail into content and
        # sets "detail": "" when not present, so the real error is in "error".
        assert "not valid JSON" in body["error"]

    async def test_non_dict_config_json_returns_422(
        self, client, auth_headers, monkeypatch
    ):
        """POST /onboard/preflight with a JSON array as config/config.json → 422."""
        from robotsix_central_deploy.onboard import fetcher as fetcher_mod
        from robotsix_central_deploy.onboard import parser as parser_mod

        monkeypatch.setattr(
            fetcher_mod,
            "fetch_repo_files",
            lambda git_url, timeout_sec=30, github_token=None: RepoFiles(
                compose_bytes=b"services:\n  app:\n    image: img",
                config_schema_json=b'{"type": "object"}',
                config_json=b'["item1", "item2"]',
                config_json_template=None,
            ),
        )
        monkeypatch.setattr(parser_mod, "parse_compose", self._mock_parse_compose)

        resp = await client.post(
            "/onboard/preflight",
            json={
                "name": "my-svc",
                "git_url": "https://github.com/org/my-svc",
            },
            headers=auth_headers,
        )

        assert resp.status_code == 422
        body = resp.json()
        # The http_exception_handler copies dict detail into content and
        # sets "detail": "" when not present, so the real error is in "error".
        assert "top-level JSON object" in body["error"]
