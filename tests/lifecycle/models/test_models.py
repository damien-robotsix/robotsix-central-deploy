"""Tests for the service state machine and model schemas."""

import pytest

from robotsix_central_deploy.lifecycle.models import (
    TRANSITIONS,
    ServiceRecord,
    ServiceState,
    UpdateState,
    can_transition,
)


class TestServiceStateEnum:
    """Ensure the seven states are present."""

    def test_all_states_exist(self):
        assert {s.value for s in ServiceState} == {
            "stopped",
            "starting",
            "running",
            "stopping",
            "restarting",
            "failed",
            "unknown",
        }

    def test_state_is_string_comparable(self):
        assert ServiceState.RUNNING == "running"
        assert ServiceState.RUNNING.value == "running"


class TestTransitions:
    """State machine transition rules."""

    @pytest.mark.parametrize(
        "src,dst,allowed",
        [
            # STOPPED → STARTING only
            (ServiceState.STOPPED, ServiceState.STARTING, True),
            (ServiceState.STOPPED, ServiceState.RUNNING, False),
            (ServiceState.STOPPED, ServiceState.STOPPING, False),
            (ServiceState.STOPPED, ServiceState.RESTARTING, False),
            # STARTING → RUNNING or FAILED
            (ServiceState.STARTING, ServiceState.RUNNING, True),
            (ServiceState.STARTING, ServiceState.FAILED, True),
            (ServiceState.STARTING, ServiceState.STOPPED, False),
            # RUNNING → STOPPING or RESTARTING
            (ServiceState.RUNNING, ServiceState.STOPPING, True),
            (ServiceState.RUNNING, ServiceState.RESTARTING, True),
            (ServiceState.RUNNING, ServiceState.STARTING, False),
            # STOPPING → STOPPED or FAILED
            (ServiceState.STOPPING, ServiceState.STOPPED, True),
            (ServiceState.STOPPING, ServiceState.FAILED, True),
            (ServiceState.STOPPING, ServiceState.RUNNING, False),
            # RESTARTING → STOPPING only
            (ServiceState.RESTARTING, ServiceState.STOPPING, True),
            (ServiceState.RESTARTING, ServiceState.RUNNING, False),
            # FAILED → STARTING only
            (ServiceState.FAILED, ServiceState.STARTING, True),
            (ServiceState.FAILED, ServiceState.STOPPING, False),
            # UNKNOWN → STARTING or STOPPING
            (ServiceState.UNKNOWN, ServiceState.STARTING, True),
            (ServiceState.UNKNOWN, ServiceState.STOPPING, True),
            (ServiceState.UNKNOWN, ServiceState.RUNNING, False),
        ],
    )
    def test_transition_allowed(self, src, dst, allowed):
        assert can_transition(src, dst) == allowed

    def test_all_transitions_are_declared(self):
        """Every state must have at least one outgoing transition."""
        for state in ServiceState:
            assert state in TRANSITIONS, f"no transitions declared for {state}"
            assert len(TRANSITIONS[state]) > 0, f"empty transition set for {state}"


class TestServiceRecord:
    def test_defaults(self):
        rec = ServiceRecord(name="test-svc")
        assert rec.name == "test-svc"
        assert rec.image == ""
        assert rec.state == ServiceState.UNKNOWN
        assert rec.last_error == ""
        assert rec.updated_at > 0

    def test_to_status(self):
        rec = ServiceRecord(name="svc", image="img:v1", state=ServiceState.RUNNING)
        status = rec.to_status()
        assert status.name == "svc"
        assert status.state == ServiceState.RUNNING
        assert status.image == "img:v1"

    def test_to_list_item(self):
        # Primary service — component_id propagates as empty string
        rec = ServiceRecord(name="svc", state=ServiceState.STOPPED)
        item = rec.to_list_item()
        assert item.name == "svc"
        assert item.state == ServiceState.STOPPED
        assert item.component_id == ""

        # Sibling service — component_id propagates as primary name
        sib = ServiceRecord(
            name="svc-ingester", state=ServiceState.RUNNING, component_id="svc"
        )
        sib_item = sib.to_list_item()
        assert sib_item.component_id == "svc"


class TestUpdateState:
    """Ensure the four update states are present."""

    def test_all_states_exist(self):
        assert {s.value for s in UpdateState} == {
            "unknown",
            "up-to-date",
            "update-available",
            "auth-error",
        }

    def test_auth_error_is_string_comparable(self):
        assert UpdateState.AUTH_ERROR == "auth-error"
        assert UpdateState.AUTH_ERROR.value == "auth-error"


class TestServiceRecordAuthError:
    def test_to_status_auth_error_when_flag_set(self):
        """When registry_auth_error is True, update_state is auth-error."""
        rec = ServiceRecord(
            name="svc",
            image="ghcr.io/o/img:main",
            state=ServiceState.RUNNING,
            registry_auth_error=True,
        )
        status = rec.to_status()
        assert status.update_state == UpdateState.AUTH_ERROR

    def test_to_status_unknown_when_no_auth_error(self):
        """Without registry_auth_error, missing digest gives UNKNOWN."""
        rec = ServiceRecord(
            name="svc",
            image="ghcr.io/o/img:main",
            state=ServiceState.RUNNING,
            registry_auth_error=False,
        )
        status = rec.to_status()
        assert status.update_state == UpdateState.UNKNOWN

    def test_auth_error_preserves_over_known_digests(self):
        """registry_auth_error takes precedence regardless of digest state."""
        rec = ServiceRecord(
            name="svc",
            image="ghcr.io/o/img:main",
            state=ServiceState.RUNNING,
            deployed_image_digest="sha256:aaa",
            latest_registry_digest="sha256:aaa",
            registry_auth_error=True,
        )
        status = rec.to_status()
        assert status.update_state == UpdateState.AUTH_ERROR
