"""Tests for central-deploy's own config surface (``/config``).

The deploy plane is itself a component, so it owes the same four routes every
other component owes (robotsix-standards config-ownership). What is tested
hardest here is the write path: a UI renders a secret masked, the operator
edits a neighbouring field, the form posts everything back, and a careless
merge writes the mask over the live credential. These tests pin the behaviour
that must not regress.

There is deliberately no authentication coverage here. Component-level auth was
removed with the ``api_key`` config field (PR #775): ``lifecycle/auth.py``'s
``verify_auth`` is now a no-op stub and the fleet edge (Traefik + tinyauth) is
the only gate, per robotsix-standards ``component-standard.md``. The secret used
below is therefore an ordinary secret field, chosen only because it is one — not
because it guards these routes.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod

#: A top-level ``SecretStr`` that still exists on ``LifecycleConfig``. These
#: tests used ``api_key`` until PR #775 deleted it; nothing here depends on
#: which field it is, only that it is a top-level secret.
SECRET_FIELD = "board_api_token"
SECRET_VALUE = "tok-live"
#: A second top-level secret, left unset, for the "do not mask what is not
#: there" case. Must be a different field from SECRET_FIELD.
UNSET_SECRET_FIELD = "ghcr_pull_token"
MASK = "**********"


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    """Point robotsix_config at a throwaway config file for the test."""
    path = tmp_path / "self_config" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "log_level": "INFO",
                SECRET_FIELD: SECRET_VALUE,
                "langfuse_projects": {
                    "prod": {"public_key": "pk-live", "secret_key": "sk-live"}
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(path))
    return path


def _on_disk(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestGetConfig:
    async def test_returns_values_schema_and_version(
        self, client: AsyncClient, config_file
    ):
        response = await client.get("/config")
        assert response.status_code == 200
        body = response.json()
        assert body["config"]["log_level"] == "INFO"
        assert body["schema"]["properties"]["port"]["type"] == "integer"
        assert body["version"] == 0

    async def test_defaults_fill_keys_the_file_omits(
        self, client: AsyncClient, config_file
    ):
        """ "Effective" means the values in use, not the file's sparse contents."""
        body = (await client.get("/config")).json()
        assert body["config"]["port"] == 8100

    async def test_top_level_secret_is_masked(self, client: AsyncClient, config_file):
        body = (await client.get("/config")).json()
        assert body["config"][SECRET_FIELD] == MASK

    async def test_secret_under_a_map_key_is_masked(
        self, client: AsyncClient, config_file
    ):
        """The canonical fleet shape: langfuse_projects.<name>.secret_key.

        A secret nested behind a data-named path must not ride out over HTTP
        just because the model cannot name its path statically.
        """
        body = (await client.get("/config")).json()
        project = body["config"]["langfuse_projects"]["prod"]
        assert project["secret_key"] == MASK
        assert project["public_key"] == "pk-live"

    async def test_unset_secret_is_not_masked(self, client: AsyncClient, config_file):
        """Masking an unset secret would claim a credential exists, and the
        mask would then be posted back as 'unchanged' — inventing one."""
        body = (await client.get("/config")).json()
        assert body["config"][UNSET_SECRET_FIELD] == ""


class TestPutConfig:
    async def test_partial_update_leaves_other_keys_alone(
        self, client: AsyncClient, config_file
    ):
        response = await client.put("/config", json={"log_level": "DEBUG"})
        assert response.status_code == 200
        assert response.json()["config"]["log_level"] == "DEBUG"
        assert _on_disk(config_file)[SECRET_FIELD] == SECRET_VALUE

    async def test_resubmitted_mask_preserves_the_stored_secret(
        self, client: AsyncClient, config_file
    ):
        """The mask means 'unchanged', never 'set it to literal asterisks'."""
        await client.put("/config", json={SECRET_FIELD: MASK, "port": 8200})
        assert _on_disk(config_file)[SECRET_FIELD] == SECRET_VALUE
        assert _on_disk(config_file)["port"] == 8200

    async def test_blank_secret_preserves_the_stored_secret(
        self, client: AsyncClient, config_file
    ):
        await client.put("/config", json={SECRET_FIELD: ""})
        assert _on_disk(config_file)[SECRET_FIELD] == SECRET_VALUE

    async def test_a_real_secret_change_is_written(
        self, client: AsyncClient, config_file
    ):
        await client.put("/config", json={SECRET_FIELD: "rotated"})
        assert _on_disk(config_file)[SECRET_FIELD] == "rotated"

    async def test_editing_beside_a_map_secret_preserves_the_credential(
        self, client: AsyncClient, config_file
    ):
        """A map node diffs whole, so the panel resubmits the masked secret
        alongside the public key the operator actually edited."""
        await client.put(
            "/config",
            json={
                "langfuse_projects": {
                    "prod": {"public_key": "pk-new", "secret_key": MASK}
                }
            },
        )
        stored = _on_disk(config_file)["langfuse_projects"]["prod"]
        assert stored["secret_key"] == "sk-live"
        assert stored["public_key"] == "pk-new"

    async def test_invalid_update_is_rejected_and_nothing_is_written(
        self, client: AsyncClient, config_file
    ):
        response = await client.put("/config", json={"port": "not-a-port"})
        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/problem+json")
        assert "port" not in _on_disk(config_file)

    async def test_rejection_detail_names_the_field_and_leaks_no_path(
        self, client: AsyncClient, config_file
    ):
        """The panel places a message inline only when the detail opens with
        '<key>: '. And the server's filesystem layout is not the client's
        business."""
        body = (await client.put("/config", json={"port": "nope"})).json()
        assert body["detail"].startswith("port: ")
        assert str(config_file) not in body["detail"]
        assert body["type"] == "urn:robotsix:error:config-validation"

    async def test_non_object_body_is_rejected(self, client: AsyncClient, config_file):
        response = await client.put("/config", json=["nope"])
        assert response.status_code == 422

    async def test_malformed_json_is_rejected_not_a_500(
        self, client: AsyncClient, config_file
    ):
        response = await client.put(
            "/config",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422

    async def test_no_op_update_records_no_version(
        self, client: AsyncClient, config_file
    ):
        response = await client.put("/config", json={"log_level": "INFO"})
        assert response.status_code == 200
        assert response.json()["version"] == 0


class TestConfigVersions:
    async def test_versions_are_newest_first(self, client: AsyncClient, config_file):
        await client.put("/config", json={"log_level": "DEBUG"})
        await client.put("/config", json={"port": 8200})
        versions = (await client.get("/config/versions")).json()["versions"]
        numbers = [entry["version"] for entry in versions]
        assert numbers == sorted(numbers, reverse=True)
        assert versions[0]["changed_keys"] == ["port"]

    async def test_history_is_empty_before_the_first_write(
        self, client: AsyncClient, config_file
    ):
        response = await client.get("/config/versions")
        assert response.status_code == 200
        assert response.json()["versions"] == []

    async def test_a_secret_change_is_named_but_not_stored(
        self, client: AsyncClient, config_file
    ):
        await client.put("/config", json={SECRET_FIELD: "rotated"})
        versions = (await client.get("/config/versions")).json()["versions"]
        assert versions[0]["changed_keys"] == [f"{SECRET_FIELD} (secret)"]
        sidecar = config_file.with_suffix(".json.versions")
        assert "rotated" not in sidecar.read_text(encoding="utf-8")


class TestConfigRollback:
    async def test_rollback_restores_values_as_a_new_version(
        self, client: AsyncClient, config_file
    ):
        await client.put("/config", json={"log_level": "DEBUG"})
        versions = (await client.get("/config/versions")).json()["versions"]
        first = min(entry["version"] for entry in versions)

        response = await client.post("/config/rollback", json={"version": first})
        assert response.status_code == 200
        assert response.json()["config"]["log_level"] == "INFO"
        assert response.json()["version"] > max(e["version"] for e in versions)

    async def test_rollback_does_not_truncate_the_history(
        self, client: AsyncClient, config_file
    ):
        await client.put("/config", json={"log_level": "DEBUG"})
        before = (await client.get("/config/versions")).json()["versions"]
        await client.post(
            "/config/rollback",
            json={"version": min(e["version"] for e in before)},
        )
        after = (await client.get("/config/versions")).json()["versions"]
        assert len(after) == len(before) + 1

    async def test_rollback_carries_live_secrets_forward(
        self, client: AsyncClient, config_file
    ):
        """The history stores no secrets, so a restored snapshot arrives with
        none. Writing it as-is would wipe the live credential."""
        await client.put("/config", json={"log_level": "DEBUG"})
        versions = (await client.get("/config/versions")).json()["versions"]
        await client.post(
            "/config/rollback",
            json={"version": min(e["version"] for e in versions)},
        )
        assert _on_disk(config_file)[SECRET_FIELD] == SECRET_VALUE

    async def test_unknown_version_is_404(self, client: AsyncClient, config_file):
        response = await client.post("/config/rollback", json={"version": 999})
        assert response.status_code == 404


class TestNoComponentLevelAuth:
    """These routes must stay edge-gated, never self-gated.

    Until PR #775 each class here had a ``test_requires_auth`` asserting 401
    without an ``X-API-Key``. That guard was deliberately removed:
    ``lifecycle/auth.py``'s ``verify_auth`` is a no-op stub and the fleet edge
    (Traefik + tinyauth) is the only gate, per robotsix-standards
    ``component-standard.md`` ("Authentication is centralized — components ship
    none").

    This is the inverse of the old assertion, and it earns its place: it fails
    if someone re-introduces a component-level guard, which would both violate
    the standard and 401 a caller the edge had already authenticated. Asserting
    a specific success code would be brittle for no gain, so it only asserts
    that nothing answers 401/403.
    """

    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("get", "/config", None),
            ("put", "/config", {}),
            ("get", "/config/versions", None),
            ("post", "/config/rollback", {"version": 1}),
        ],
    )
    async def test_route_does_not_gate_on_credentials(
        self, client: AsyncClient, config_file, method, path, payload
    ):
        call = getattr(client, method)
        response = await (call(path) if payload is None else call(path, json=payload))
        assert response.status_code not in (401, 403), (
            f"{method.upper()} {path} rejected an uncredentialed caller — "
            "component-level auth must not come back; the edge is the gate."
        )


class TestRouteRegistration:
    def test_the_four_standard_routes_exist(self):
        """config-ownership.md fixes these paths; the shared panel's client
        hard-codes them, so a rename here silently breaks every consumer."""
        schema = server_mod.app.openapi()["paths"]
        assert "get" in schema["/config"]
        assert "put" in schema["/config"]
        assert "get" in schema["/config/versions"]
        assert "post" in schema["/config/rollback"]

    def test_config_writes_are_not_csrf_exempt(self):
        """Every other write path here is exempt because it is authenticated
        by a header a browser will not attach cross-site. /config is reached
        from the settings page with only the SSO cookie, so the exemption
        would hand a cross-site form post the operator's own privileges."""
        for pattern in server_mod._CSRF_EXEMPT_URLS:
            assert not pattern.match("/config")
            assert not pattern.match("/config/rollback")
