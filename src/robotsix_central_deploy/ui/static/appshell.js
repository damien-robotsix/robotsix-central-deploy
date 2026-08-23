/* Mounts the shared AppShell navigation shell from robotsix-ui.
 *
 * Every RobotSix UI uses the same top-level chrome — brand, ordered nav
 * links, the standard Settings entry — styled entirely by the --rsu-*
 * design tokens from robotsix-ui.css. The page passes nothing except the
 * options object; the bundle does the rest.
 */
import { mountAppShell } from "/ui/static/robotsix-ui-vanilla.js";

(function () {
  const container = document.getElementById("appshell-container");
  if (!container) return;

  const pathname = window.location.pathname;

  if (pathname === "/ui") {
    mountAppShell(container, {
      brand: "Robotsix Deploy",
      navItems: [
        { href: "/ui", label: "Dashboard", active: true },
      ],
      settingsHref: "/ui/settings",
      rightSlot: `
<button id="add-component-btn" data-action="openOnboardModal" class="btn-header">+ Add Component</button>
<button id="claude-auth-btn" data-action="showClaudeAuthSection" class="btn-header">Claude Auth</button>
<button id="self-update-btn" data-action="triggerSelfUpdate" title="A newer server image is available on the registry" class="btn-update-server hidden">⬆ Update server</button>
<button id="logout-btn" data-action="doLogout" class="btn-header">Logout</button>
<span id="refresh-time">Last refreshed: --:--:--</span>
`,
    });
  } else if (pathname === "/ui/settings") {
    mountAppShell(container, {
      brand: "Robotsix Deploy",
      navItems: [
        { href: "/ui", label: "Dashboard" },
        { href: "/ui/settings", label: "Settings", active: true },
      ],
      settingsHref: "/ui/settings",
    });
  }
})();