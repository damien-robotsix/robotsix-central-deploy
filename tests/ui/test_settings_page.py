"""Tests for the Settings page — the mount point for the shared config panel.

The page itself is deliberately thin: markup, a CSRF token, and a module
script. What matters is that it keeps mounting robotsix-ui's panel rather than
growing a hand-rolled settings form, and that the CSP and CSRF constraints it
lives under are not quietly broken.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod

_UI_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "robotsix_central_deploy"
    / "ui"
)
_SETTINGS_HTML = (_UI_DIR / "settings.html").read_text(encoding="utf-8")
_SETTINGS_JS = (_UI_DIR / "static" / "settings.js").read_text(encoding="utf-8")
_DASHBOARD_HTML = (_UI_DIR / "dashboard.html").read_text(encoding="utf-8")
_APPSHELL_JS = (_UI_DIR / "static" / "appshell.js").read_text(encoding="utf-8")


class TestSettingsRoute:
    async def test_page_is_served(self, client: AsyncClient):
        response = await client.get("/ui/settings")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")

    async def test_page_carries_no_auth_dependency(self, client: AsyncClient):
        """Like the dashboard: the edge authenticates through tinyauth SSO
        before the request arrives, and there is no login page here."""
        response = await client.get("/ui/settings")
        assert response.status_code != 401

    async def test_csrf_placeholder_is_substituted(self, client: AsyncClient):
        body = (await client.get("/ui/settings")).text
        assert "{{csrf_token}}" not in body

    async def test_page_mounts_the_panel_container(self, client: AsyncClient):
        body = (await client.get("/ui/settings")).text
        assert 'id="settings-panel"' in body
        assert "/ui/static/settings.js" in body

    async def test_dashboard_links_to_settings(self):
        """The settings link is rendered by the shared AppShell JS bundle,
        not as a static <a> element in the dashboard markup."""
        assert "/ui/static/appshell.js" in _DASHBOARD_HTML
        assert 'settingsHref: "/ui/settings"' in _APPSHELL_JS


class TestAppShellRightSlotIsADomNode:
    """The dashboard's header controls live in AppShell's `rightSlot`.

    `mountAppShell` renders a **string** rightSlot with `textContent` and only
    a **Node** with `appendChild`.  Handing it markup as a string therefore
    escapes it: the buttons show up as literal source text and never enter the
    DOM, so every `getElementById` in dashboard.js that looks for one returns
    null and the dashboard dies with "can't access property ..., is null".
    """

    #: Header element ids that exist nowhere but the rightSlot markup.
    _HEADER_IDS = (
        "add-component-btn",
        "claude-auth-btn",
        "self-update-btn",
        "logout-btn",
        "refresh-time",
    )

    def test_right_slot_is_not_handed_a_bare_string(self):
        """A backtick (or quote) straight after `rightSlot:` is the bug."""
        match = re.search(r"rightSlot:\s*(\S)", _APPSHELL_JS)
        assert match is not None, "the dashboard shell declares no rightSlot"
        assert match.group(1) not in "`'\"", (
            "rightSlot is a string literal — mountAppShell will render it as "
            "escaped text and the header buttons will never exist in the DOM"
        )

    def test_right_slot_markup_is_parsed_into_dom(self):
        assert 'createElement("template")' in _APPSHELL_JS
        assert ".content" in _APPSHELL_JS

    @pytest.mark.parametrize("element_id", _HEADER_IDS)
    def test_header_control_is_declared(self, element_id: str):
        assert f'id="{element_id}"' in _APPSHELL_JS

    @pytest.mark.parametrize("element_id", _HEADER_IDS)
    def test_dashboard_lookup_has_a_target(self, element_id: str):
        """Each id dashboard.js resolves is one the shell actually mounts."""
        dashboard_js = (_UI_DIR / "static" / "dashboard.js").read_text(encoding="utf-8")
        if f"getElementById('{element_id}')" not in dashboard_js:
            pytest.skip(f"dashboard.js does not look up {element_id}")
        assert f'id="{element_id}"' in _APPSHELL_JS


class TestSettingsPageConstraints:
    """The page lives under `script-src 'self'` and behind CSRF."""

    _INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)

    def test_no_inline_script_block(self):
        """Preset.BALANCED sets script-src 'self', which blocks inline
        <script> outright — the CSRF token travels in a data attribute."""
        assert self._INLINE_SCRIPT_RE.search(_SETTINGS_HTML) is None

    def test_no_inline_event_handlers(self):
        assert re.search(r"\son(click|change|submit)=", _SETTINGS_HTML) is None

    def test_token_is_handed_over_in_a_data_attribute(self):
        assert 'data-csrf-token="{{csrf_token}}"' in _SETTINGS_HTML

    def test_script_sends_the_token_on_writes(self):
        """/config is not CSRF-exempt, so a save without this header is a 403
        the operator has no way to act on."""
        assert "x-csrftoken" in _SETTINGS_JS


class TestPanelIsNotReimplemented:
    """The fleet has exactly one settings renderer. This page mounts it.

    A component that hand-rolls the form eventually reintroduces the bug the
    shared panel exists to prevent: a masked secret posted back over the real
    credential.
    """

    def test_script_imports_the_shared_bundle(self):
        assert "/ui/static/robotsix-ui-vanilla.js" in _SETTINGS_JS
        assert "mountConfigPanel" in _SETTINGS_JS

    def test_page_links_the_shared_stylesheet(self):
        assert 'href="/ui/static/robotsix-ui.css"' in _SETTINGS_HTML

    def test_page_declares_no_form_inputs_of_its_own(self):
        """Every input on this page is rendered by the panel at runtime."""
        assert "<input" not in _SETTINGS_HTML
        assert "<select" not in _SETTINGS_HTML

    def test_local_css_does_not_restyle_panel_internals(self):
        """settings.css frames the page; how a config panel looks is
        robotsix-ui's call, fleet-wide."""
        css = (_UI_DIR / "static" / "settings.css").read_text(encoding="utf-8")
        assert ".rsu-" not in css


class TestSettingsWritesAreCsrfProtected:
    """The one piece of this page that can fail silently.

    ``/config`` is reached from the browser with only the SSO cookie, so it is
    deliberately left out of the CSRF exemptions the header-authenticated JSON
    API enjoys. Two ways that breaks: the page embeds a token the middleware
    will not accept (every save 403s), or the exemption creeps back in (a
    cross-site form post rides the operator's session).

    The client must speak https — the CSRF cookie is ``Secure``, and over
    plain http it is never sent, which sends ``asgi_csrf`` down its
    "no cookies, nothing to protect" path and hides the whole mechanism.
    """

    @pytest.fixture(autouse=True)
    def _require_csrf(self):
        if not server_mod._HAS_CSRF:
            pytest.skip("CSRF middleware not available (asgi-csrf not installed)")

    @pytest.fixture
    def config_file(self, monkeypatch, tmp_path):
        path = tmp_path / "csrf_settings" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"log_level": "INFO"}), encoding="utf-8")
        monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(path))
        return path

    @pytest.fixture
    async def https_client(self):
        transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
        async with AsyncClient(
            transport=transport, base_url="https://testserver"
        ) as client:
            yield client

    async def _token(self, https_client: AsyncClient) -> str:
        page = await https_client.get("/ui/settings")
        match = re.search(r'data-csrf-token="([^"]*)"', page.text)
        assert match is not None
        return match.group(1)

    async def test_embedded_token_matches_the_cookie_the_middleware_set(
        self, https_client: AsyncClient, config_file
    ):
        """A token minted from a different secret would 403 every save."""
        token = await self._token(https_client)
        assert token
        assert https_client.cookies.get("csrftoken") == token

    async def test_write_with_the_token_is_accepted(
        self, https_client: AsyncClient, config_file
    ):
        token = await self._token(https_client)
        response = await https_client.put(
            "/config",
            json={"log_level": "DEBUG"},
            headers={"x-csrftoken": token, "X-API-Key": "test-key"},
        )
        assert response.status_code == 200
        assert json.loads(config_file.read_text())["log_level"] == "DEBUG"

    async def test_write_without_the_token_is_refused_and_changes_nothing(
        self, https_client: AsyncClient, config_file
    ):
        """Credentials do not substitute for the token: the request is
        otherwise perfectly authenticated and still must not land."""
        await self._token(https_client)
        response = await https_client.put(
            "/config",
            json={"log_level": "WARNING"},
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 403
        assert json.loads(config_file.read_text())["log_level"] == "INFO"

    async def test_rollback_without_the_token_is_refused(
        self, https_client: AsyncClient, config_file
    ):
        await self._token(https_client)
        response = await https_client.post("/config/rollback", json={"version": 1})
        assert response.status_code == 403
