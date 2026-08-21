"""Tests for central-deploy's own config surface (``/config``).

The deploy plane is itself a component, so it owes the same four routes every
other component owes (robotsix-standards config-ownership). What is tested
hardest here is the write path: a UI renders a secret masked, the operator
edits a neighbouring field, the form posts everything back, and a careless
merge writes the mask over the live credential. These tests pin the behaviour
that must not regress.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod

API_KEY = "test-key"
HEADERS = {"X-API-Key": API_KEY}
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
                "api_key": API_KEY,
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
        response = await client.get("/config", headers=HEADERS)
        assert response.status_code == 200
        body = response.json()
        assert body["config"]["log_level"] == "INFO"
        assert body["schema"]["properties"]["port"]["type"] == "integer"
        assert body["version"] == 0

    async def test_defaults_fill_keys_the_file_omits(
        self, client: AsyncClient, config_file
    ):
        """ "Effective" means the values in use, not the file's sparse contents."""
        body = (await client.get("/config", headers=HEADERS)).json()
        assert body["config"]["port"] == 8100

    async def test_top_level_secret_is_masked(self, client: AsyncClient, config_file):
        body = (await client.get("/config", headers=HEADERS)).json()
        assert body["config"]["api_key"] == MASK

    async def test_secret_under_a_map_key_is_masked(
        self, client: AsyncClient, config_file
    ):
        """The canonical fleet shape: langfuse_projects.<name>.secret_key.

        A secret nested behind a data-named path must not ride out over HTTP
        just because the model cannot name its path statically.
        """
        body = (await client.get("/config", headers=HEADERS)).json()
        project = body["config"]["langfuse_projects"]["prod"]
        assert project["secret_key"] == MASK
        assert project["public_key"] == "pk-live"

    async def test_unset_secret_is_not_masked(self, client: AsyncClient, config_file):
        """Masking an unset secret would claim a credential exists, and the
        mask would then be posted back as 'unchanged' — inventing one."""
        body = (await client.get("/config", headers=HEADERS)).json()
        assert body["config"]["board_api_token"] == ""

    async def test_requires_auth(self, client: AsyncClient, config_file):
        assert (await client.get("/config")).status_code == 401


class TestPutConfig:
    async def test_partial_update_leaves_other_keys_alone(
        self, client: AsyncClient, config_file
    ):
        response = await client.put(
            "/config", json={"log_level": "DEBUG"}, headers=HEADERS
        )
        assert response.status_code == 200
        assert response.json()["config"]["log_level"] == "DEBUG"
        assert _on_disk(config_file)["api_key"] == API_KEY

    async def test_resubmitted_mask_preserves_the_stored_secret(
        self, client: AsyncClient, config_file
    ):
        """The mask means 'unchanged', never 'set it to literal asterisks'."""
        await client.put(
            "/config", json={"api_key": MASK, "port": 8200}, headers=HEADERS
        )
        assert _on_disk(config_file)["api_key"] == API_KEY
        assert _on_disk(config_file)["port"] == 8200

    async def test_blank_secret_preserves_the_stored_secret(
        self, client: AsyncClient, config_file
    ):
        await client.put("/config", json={"api_key": ""}, headers=HEADERS)
        assert _on_disk(config_file)["api_key"] == API_KEY

    async def test_a_real_secret_change_is_written(
        self, client: AsyncClient, config_file
    ):
        await client.put("/config", json={"api_key": "rotated"}, headers=HEADERS)
        assert _on_disk(config_file)["api_key"] == "rotated"

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
            headers=HEADERS,
        )
        stored = _on_disk(config_file)["langfuse_projects"]["prod"]
        assert stored["secret_key"] == "sk-live"
        assert stored["public_key"] == "pk-new"

    async def test_invalid_update_is_rejected_and_nothing_is_written(
        self, client: AsyncClient, config_file
    ):
        response = await client.put(
            "/config", json={"port": "not-a-port"}, headers=HEADERS
        )
        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/problem+json")
        assert "port" not in _on_disk(config_file)

    async def test_rejection_detail_names_the_field_and_leaks_no_path(
        self, client: AsyncClient, config_file
    ):
        """The panel places a message inline only when the detail opens with
        '<key>: '. And the server's filesystem layout is not the client's
        business."""
        body = (
            await client.put("/config", json={"port": "nope"}, headers=HEADERS)
        ).json()
        assert body["detail"].startswith("port: ")
        assert str(config_file) not in body["detail"]
        assert body["type"] == "urn:robotsix:error:config-validation"

    async def test_non_object_body_is_rejected(self, client: AsyncClient, config_file):
        response = await client.put("/config", json=["nope"], headers=HEADERS)
        assert response.status_code == 422

    async def test_malformed_json_is_rejected_not_a_500(
        self, client: AsyncClient, config_file
    ):
        response = await client.put(
            "/config",
            content=b"{not json",
            headers={**HEADERS, "Content-Type": "application/json"},
        )
        assert response.status_code == 422

    async def test_no_op_update_records_no_version(
        self, client: AsyncClient, config_file
    ):
        response = await client.put(
            "/config", json={"log_level": "INFO"}, headers=HEADERS
        )
        assert response.status_code == 200
        assert response.json()["version"] == 0

    async def test_requires_auth(self, client: AsyncClient, config_file):
        assert (await client.put("/config", json={})).status_code == 401


class TestConfigVersions:
    async def test_versions_are_newest_first(self, client: AsyncClient, config_file):
        await client.put("/config", json={"log_level": "DEBUG"}, headers=HEADERS)
        await client.put("/config", json={"port": 8200}, headers=HEADERS)
        versions = (await client.get("/config/versions", headers=HEADERS)).json()[
            "versions"
        ]
        numbers = [entry["version"] for entry in versions]
        assert numbers == sorted(numbers, reverse=True)
        assert versions[0]["changed_keys"] == ["port"]

    async def test_history_is_empty_before_the_first_write(
        self, client: AsyncClient, config_file
    ):
        response = await client.get("/config/versions", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["versions"] == []

    async def test_a_secret_change_is_named_but_not_stored(
        self, client: AsyncClient, config_file
    ):
        await client.put("/config", json={"api_key": "rotated"}, headers=HEADERS)
        versions = (await client.get("/config/versions", headers=HEADERS)).json()[
            "versions"
        ]
        assert versions[0]["changed_keys"] == ["api_key (secret)"]
        sidecar = config_file.with_suffix(".json.versions")
        assert "rotated" not in sidecar.read_text(encoding="utf-8")

    async def test_requires_auth(self, client: AsyncClient, config_file):
        assert (await client.get("/config/versions")).status_code == 401


class TestConfigRollback:
    async def test_rollback_restores_values_as_a_new_version(
        self, client: AsyncClient, config_file
    ):
        await client.put("/config", json={"log_level": "DEBUG"}, headers=HEADERS)
        versions = (await client.get("/config/versions", headers=HEADERS)).json()[
            "versions"
        ]
        first = min(entry["version"] for entry in versions)

        response = await client.post(
            "/config/rollback", json={"version": first}, headers=HEADERS
        )
        assert response.status_code == 200
        assert response.json()["config"]["log_level"] == "INFO"
        assert response.json()["version"] > max(e["version"] for e in versions)

    async def test_rollback_does_not_truncate_the_history(
        self, client: AsyncClient, config_file
    ):
        await client.put("/config", json={"log_level": "DEBUG"}, headers=HEADERS)
        before = (await client.get("/config/versions", headers=HEADERS)).json()[
            "versions"
        ]
        await client.post(
            "/config/rollback",
            json={"version": min(e["version"] for e in before)},
            headers=HEADERS,
        )
        after = (await client.get("/config/versions", headers=HEADERS)).json()[
            "versions"
        ]
        assert len(after) == len(before) + 1

    async def test_rollback_carries_live_secrets_forward(
        self, client: AsyncClient, config_file
    ):
        """The history stores no secrets, so a restored snapshot arrives with
        none. Writing it as-is would wipe the live credential."""
        await client.put("/config", json={"log_level": "DEBUG"}, headers=HEADERS)
        versions = (await client.get("/config/versions", headers=HEADERS)).json()[
            "versions"
        ]
        await client.post(
            "/config/rollback",
            json={"version": min(e["version"] for e in versions)},
            headers=HEADERS,
        )
        assert _on_disk(config_file)["api_key"] == API_KEY

    async def test_unknown_version_is_404(self, client: AsyncClient, config_file):
        response = await client.post(
            "/config/rollback", json={"version": 999}, headers=HEADERS
        )
        assert response.status_code == 404

    async def test_requires_auth(self, client: AsyncClient, config_file):
        response = await client.post("/config/rollback", json={"version": 1})
        assert response.status_code == 401


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
