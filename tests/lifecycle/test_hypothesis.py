"""Property-based tests for lifecycle models and endpoints.

Uses Hypothesis to fuzz the state machine, endpoint query parameters,
and enum serialisation paths that hand-picked examples may miss.
"""

from __future__ import annotations

import pytest

pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from hypothesis.stateful import (  # noqa: E402
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
)

from robotsix_central_deploy.lifecycle.models import (
    ExecutionBackendType,
    HealthStatus,
    ServiceState,
    StoreBackend,
    UpdateState,
    VolumeEntryType,
    can_transition,
)


# ---------------------------------------------------------------------------
# 1. State-machine transition completeness (RuleBasedStateMachine)
# ---------------------------------------------------------------------------


class ServiceStateMachine(RuleBasedStateMachine):
    """Explore all reachable state sequences via can_transition()."""

    def __init__(self) -> None:
        super().__init__()
        self.state: ServiceState = ServiceState.UNKNOWN

    # -- externally-triggered actions ----------------------------------------

    @rule()
    @precondition(lambda self: can_transition(self.state, ServiceState.STARTING))
    def start(self) -> None:
        self.state = ServiceState.STARTING

    @rule()
    @precondition(lambda self: can_transition(self.state, ServiceState.STOPPING))
    def stop(self) -> None:
        self.state = ServiceState.STOPPING

    @rule()
    @precondition(lambda self: can_transition(self.state, ServiceState.RESTARTING))
    def restart(self) -> None:
        self.state = ServiceState.RESTARTING

    # -- internal transitions (outcomes of start/stop/restart operations) ----

    @rule()
    @precondition(lambda self: can_transition(self.state, ServiceState.RUNNING))
    def become_running(self) -> None:
        self.state = ServiceState.RUNNING

    @rule()
    @precondition(lambda self: can_transition(self.state, ServiceState.FAILED))
    def become_failed(self) -> None:
        self.state = ServiceState.FAILED

    @rule()
    @precondition(lambda self: can_transition(self.state, ServiceState.STOPPED))
    def become_stopped(self) -> None:
        self.state = ServiceState.STOPPED

    @invariant()
    def state_is_valid(self) -> None:
        """Every reachable state must be a recognised ServiceState member."""
        assert isinstance(self.state, ServiceState)


TestServiceStateMachine = ServiceStateMachine.TestCase
TestServiceStateMachine.settings = settings(max_examples=200)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 2. Input validation fuzzing
# ---------------------------------------------------------------------------


@given(tail=st.integers())
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
async def test_tail_validation(tail: int, client, auth_headers: dict[str, str]) -> None:
    """tail must be in [1, 10000]; out-of-band values must produce 422."""
    resp = await client.get(
        "/services/svc-a/logs", params={"tail": tail}, headers=auth_headers
    )
    if 1 <= tail <= 10000:
        assert resp.status_code in (200, 404)
    else:
        assert resp.status_code == 422


@given(since=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=80))
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
async def test_since_validation(
    since: str, client, auth_headers: dict[str, str]
) -> None:
    """since has no range constraint — any string must be accepted."""
    resp = await client.get(
        "/services/svc-a/logs", params={"since": since}, headers=auth_headers
    )
    assert resp.status_code in (200, 404)


@given(follow=st.booleans())
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
async def test_follow_validation(
    follow: bool, client, auth_headers: dict[str, str]
) -> None:
    """follow is a plain bool with no constraints."""
    resp = await client.get(
        "/services/svc-a/logs",
        params={"follow": str(follow).lower()},
        headers=auth_headers,
    )
    assert resp.status_code in (200, 404)


# ---------------------------------------------------------------------------
# 3. Enum roundtrip serialisation
# ---------------------------------------------------------------------------

ALL_ENUMS: list[type] = [
    ServiceState,
    HealthStatus,
    UpdateState,
    StoreBackend,
    ExecutionBackendType,
    VolumeEntryType,
]


def _roundtrip_one(cls: type, value: str) -> None:
    instance = cls(value)
    assert instance.value == value
    assert cls(value) is instance  # enum singleton identity


@pytest.mark.parametrize("enum_cls", ALL_ENUMS)
@given(value=st.data())
@settings(max_examples=50)
def test_enum_roundtrip(enum_cls: type, value: st.DataObject) -> None:
    """Every enum value roundtrips through .value."""
    member = value.draw(st.sampled_from(list(enum_cls)))
    _roundtrip_one(enum_cls, member.value)


@pytest.mark.parametrize("enum_cls", ALL_ENUMS)
@given(bad=st.data())
@settings(max_examples=50)
def test_enum_rejects_invalid_string(enum_cls: type, bad: st.DataObject) -> None:
    """An unrecognised string must raise ValueError."""
    valid_values = {m.value for m in enum_cls}
    invalid = bad.draw(st.text().filter(lambda s: s not in valid_values))
    with pytest.raises(ValueError):
        enum_cls(invalid)


def test_enum_rejects_empty_string() -> None:
    """Empty string is never a valid enum value."""
    for cls in ALL_ENUMS:
        with pytest.raises(ValueError):
            cls("")
