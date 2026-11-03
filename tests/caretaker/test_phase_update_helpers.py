"""Unit tests for the helpers ``phase_update`` is composed of."""

from __future__ import annotations

# Import lifecycle models first to avoid the caretaker ↔ lifecycle import cycle
# (see the note in tests/caretaker/test_phases.py).
from robotsix_central_deploy.lifecycle.models import (  # isort: skip
    DeployOutcome,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.caretaker.phases import (
    SKIP_BUSY,
    SKIP_KNOWN_BAD,
    SKIP_NO_CONFIG,
    SKIP_NO_UPDATE,
    SKIP_NOT_SELECTED,
    SKIP_OPTED_OUT,
    SKIP_SELF,
    SKIP_SIBLING,
    _apply_successful_deploy,
    _resolve_update_image_ref,
    _update_skip_reason,
)
from robotsix_central_deploy.registry.models import ComponentConfig


def _record(**overrides) -> ServiceRecord:
    fields = {
        "name": "svc",
        "image": "repo:v1",
        "container_name": "svc",
        "deployed_image_digest": "sha256:abc",
        "latest_registry_digest": "sha256:def",
        "update_available": True,
    }
    fields.update(overrides)
    return ServiceRecord(**fields)


def _config(**overrides) -> ComponentConfig:
    fields = {"id": "svc", "image": "repo:v1", "container_name": "svc"}
    fields.update(overrides)
    return ComponentConfig(**fields)


def _reason(record, config=None, **kwargs):
    params = {
        "self_container_name": "",
        "only_components": None,
        "busy_components": None,
        "stale_sibling_parents": set(),
    }
    params.update(kwargs)
    return _update_skip_reason(
        record, _config() if config is None else config, **params
    )


class TestUpdateSkipReason:
    def test_eligible_record_is_not_skipped(self):
        assert _reason(_record()) is None

    def test_sibling_record_skipped(self):
        assert _reason(_record(name="svc-db", component_id="svc")) == SKIP_SIBLING

    def test_not_in_only_components_skipped(self):
        assert _reason(_record(), only_components={"other"}) == SKIP_NOT_SELECTED

    def test_in_only_components_not_skipped(self):
        assert _reason(_record(), only_components={"svc"}) is None

    def test_self_container_skipped(self):
        assert _reason(_record(), self_container_name="svc") == SKIP_SELF

    def test_other_self_container_not_skipped(self):
        assert _reason(_record(), self_container_name="central-deploy") is None

    def test_no_update_skipped(self):
        assert _reason(_record(update_available=False)) == SKIP_NO_UPDATE

    def test_stale_sibling_makes_parent_eligible(self):
        record = _record(update_available=False)
        assert _reason(record, stale_sibling_parents={"svc"}) is None

    def test_known_bad_digest_skipped(self):
        record = _record(failed_update_digest="sha256:def")
        assert _reason(record) == SKIP_KNOWN_BAD

    def test_newer_digest_than_known_bad_not_skipped(self):
        record = _record(failed_update_digest="sha256:old-bad")
        assert _reason(record) is None

    def test_known_bad_with_stale_sibling_still_eligible(self):
        record = _record(failed_update_digest="sha256:def")
        assert _reason(record, stale_sibling_parents={"svc"}) is None

    def test_busy_component_deferred(self):
        assert _reason(_record(), busy_components={"svc": "busy"}) == SKIP_BUSY

    def test_missing_config_skipped(self):
        assert (
            _update_skip_reason(
                _record(),
                None,
                self_container_name="",
                only_components=None,
                busy_components=None,
                stale_sibling_parents=set(),
            )
            == SKIP_NO_CONFIG
        )

    def test_opted_out_skipped(self):
        config = _config(auto_update_enabled=False)
        assert _reason(_record(), config) == SKIP_OPTED_OUT

    def test_rules_apply_in_order(self):
        """A sibling record is reported as such even when every rule matches."""
        record = _record(component_id="svc", update_available=False)
        assert (
            _reason(
                record,
                _config(auto_update_enabled=False),
                only_components=set(),
                self_container_name="svc",
                busy_components={"svc": "busy"},
            )
            == SKIP_SIBLING
        )


class TestResolveUpdateImageRef:
    def test_pins_configured_repo_to_registry_digest(self):
        record = _record(image="repo:stale-tag")
        assert _resolve_update_image_ref(_config(image="ghcr.io/o/r:main"), record) == (
            "ghcr.io/o/r@sha256:def",
            "ghcr.io/o/r:main",
        )

    def test_falls_back_to_configured_tag_without_digest(self):
        record = _record(latest_registry_digest="")
        assert _resolve_update_image_ref(_config(image="repo:main"), record) == (
            "repo:main",
            "repo:main",
        )


class TestApplySuccessfulDeploy:
    def test_resets_record_fields(self):
        record = _record(
            image="repo:old",
            failed_update_digest="sha256:bad",
            last_error="unhealthy",
        )
        outcome = DeployOutcome(
            deployed_digest="sha256:def",
            previous_digest="sha256:abc",
            state=ServiceState.RUNNING,
        )
        _apply_successful_deploy(record, outcome, "repo:main")
        assert record.state == ServiceState.RUNNING
        assert record.image == "repo:main"
        assert record.image_revision == "sha256:def"
        assert record.deployed_image_digest == "sha256:def"
        assert record.previous_image_digest == "sha256:abc"
        assert record.update_available is False
        assert record.failed_update_digest == ""
        assert record.update_pending_since is None
        assert record.last_error == ""
