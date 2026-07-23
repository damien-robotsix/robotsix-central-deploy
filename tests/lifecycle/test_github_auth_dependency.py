"""Regression guard for the robotsix-github-auth runtime dependency.

central-deploy mints GitHub App installation tokens via the shared
``robotsix_github_auth`` library. It must be a *real* declared dependency
(not mocked/stubbed away) or the deploy server raises
``ModuleNotFoundError`` on the first mint. This imports the genuine module
so a green CI can never hide a missing/incompatible dependency, and pins
the return contract (``InstallationToken.token`` is a str).
"""


import pytest


def test_robotsix_github_auth_is_a_real_installed_dependency() -> None:
    """The shared library imports and exposes the expected token contract."""
    pytest.importorskip("robotsix_github_auth")
    import robotsix_github_auth
    from robotsix_github_auth import InstallationToken, mint_installation_token

    assert callable(mint_installation_token)
    assert "token" in InstallationToken.__dataclass_fields__
    del robotsix_github_auth
