/* Mounts the fleet's shared config panel over central-deploy's own settings.
 *
 * The panel, the schema-driven inputs, the masked secrets, the changed-keys-
 * only save and the version history all come from robotsix-ui. Nothing in
 * this file re-implements any of it — that is the point. A component that
 * hand-rolls its settings form eventually reintroduces the bug where a masked
 * secret is posted back and overwrites the real credential.
 */
import { mountConfigPanel } from "/ui/static/robotsix-ui-vanilla.js";

const container = document.getElementById("settings-panel");
const notice = document.getElementById("restart-notice");

/* The CSRF cookie is httponly, so the token is handed to the page in a data
 * attribute instead. /config is deliberately not in the middleware's exempt
 * list: unlike the JSON API it is reached from the browser with only the SSO
 * cookie, which would otherwise ride along on a cross-site request. */
const csrfToken = container.dataset.csrfToken || "";

mountConfigPanel(container, {
  title: "Central Deploy Settings",
  headers: csrfToken ? { "x-csrftoken": csrfToken } : {},
  onSaved: () => {
    notice.hidden = false;
  },
});
