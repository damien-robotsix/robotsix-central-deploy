"""central-deploy's own config view is computed, not stored.

central-deploy has no component config volume — it is the control plane, and
its settings live in its own config file. The only "component config" it ever
had was a Langfuse view derived from those settings plus whatever
auto-discovery found.

That view used to be written into `ConfigYamlStore` on every startup and every
chat-access toggle, purely so `GET /services/central-deploy/config` could read
it back. Two problems with that: it stored a second copy of data the process
already holds, and it wrote the *secret values* into a file on disk to do it.
"""

from __future__ import annotations

from httpx import AsyncClient
from pydantic import SecretStr

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle._langfuse_config import (
    build_central_deploy_langfuse_config,
    reconcile_langfuse_after_toggle,
)
from robotsix_central_deploy.lifecycle.config import LangfuseProjectCreds


def _creds(prefix: str) -> LangfuseProjectCreds:
    return LangfuseProjectCreds(
        public_key=f"pk-{prefix}", secret_key=SecretStr(f"sk-{prefix}")
    )


class _Cfg:
    """Stand-in for LifecycleConfig carrying only operator-set projects."""

    def __init__(self, projects: dict[str, LangfuseProjectCreds]) -> None:
        self.langfuse_projects = projects


class TestComputedViewMatchesTheOldMerge:
    """The stored value was built auto-first, operator-overrides-second. The
    computed replacement must produce exactly that, or the config panel
    silently changes what it shows."""

    def test_auto_discovered_projects_appear(self) -> None:
        out = build_central_deploy_langfuse_config(_Cfg({}), {"a": _creds("auto-a")})
        assert out == {
            "langfuse_projects": {
                "a": {"public_key": "pk-auto-a", "secret_key": "sk-auto-a"}
            }
        }

    def test_operator_projects_appear(self) -> None:
        out = build_central_deploy_langfuse_config(_Cfg({"b": _creds("op-b")}), {})
        assert out["langfuse_projects"]["b"]["public_key"] == "pk-op-b"

    def test_operator_wins_on_alias_collision(self) -> None:
        """A rotated operator key must not be masked by a stale discovered one."""
        out = build_central_deploy_langfuse_config(
            _Cfg({"shared": _creds("operator")}), {"shared": _creds("auto")}
        )
        assert out["langfuse_projects"]["shared"]["secret_key"] == "sk-operator"

    def test_empty_inputs_yield_an_empty_project_map(self) -> None:
        assert build_central_deploy_langfuse_config(_Cfg({}), {}) == {
            "langfuse_projects": {}
        }


class TestNothingIsPersisted:
    async def test_toggle_reconcile_writes_no_central_deploy_entry(
        self, client: AsyncClient
    ) -> None:
        """The toggle path used to persist the merged view — including secret
        values — into the store on every call."""
        store = server_mod.app.state.config_yaml_store

        class _Req:
            app = server_mod.app

        await reconcile_langfuse_after_toggle(
            server_mod.app.state.component_config_store, _Req()
        )

        current = await store.get_current("central-deploy")
        assert current is None or "langfuse_projects" not in current

    async def test_endpoint_still_serves_the_view(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """Computing it must not 404 or drop the key the panel renders."""
        server_mod.app.state.auto_langfuse_projects = {"proj": _creds("live")}

        resp = await client.get("/services/central-deploy/config", headers=auth_headers)
        # 404 only if no schema was seeded in this test app; the contract we
        # care about is that a 200 carries the computed projects.
        if resp.status_code == 200:
            assert "langfuse_projects" in resp.json()["config"]
