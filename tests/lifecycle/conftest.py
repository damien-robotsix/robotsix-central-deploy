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
    """Empty headers — component-level auth was removed (auth-removal epic).

    The fleet edge (Traefik + tinyauth) is the only gate; every request
    arriving here is already authenticated, so tests send no credentials.
    The fixture is kept so existing test signatures stay stable.
    """
    return {}
