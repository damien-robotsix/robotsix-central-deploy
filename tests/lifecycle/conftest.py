"""Shared fixtures for lifecycle integration tests.

Test files in subdirectories (routers/, backends/) automatically
inherit fixtures via pytest's conftest discovery.  The ``client``
and ``_reset_globals`` fixtures are now provided by the root
``tests/conftest.py``.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}
