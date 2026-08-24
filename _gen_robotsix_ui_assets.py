"""Fetch the robotsix-ui release assets into ``ui/static/``.

Pulls the compiled stylesheet **and** the framework-free JS bundle from their
GitHub Release assets at build time, so central-deploy never re-vendors a
stale, one-shot copy of either. The URL construction mirrors the
``robotsix_ui.css_url()`` / ``robotsix_ui.vanilla_js_url()`` helpers shipped
in the robotsix-ui repository.

Both files are needed, not just the stylesheet: ``vanilla.js`` exports
``mountConfigPanel`` (the fleet's only settings renderer) and
``mountAppShell`` (the shared top-level navigation chrome), and the
stylesheet is what styles both. Fetching one without the other yields
either an unstyled panel or a styled page with nothing to put in it.
"""

from __future__ import annotations

import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROBOTSIX_UI_VERSION = "v0.1.41"

_VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")

_STATIC_DIR = (
    Path(__file__).resolve().parent
    / "src"
    / "robotsix_central_deploy"
    / "ui"
    / "static"
)

#: Release asset name → local filename under ``ui/static/``.
_ASSETS = {
    "style.css": "robotsix-ui.css",
    "vanilla.js": "robotsix-ui-vanilla.js",
}


def asset_url(version: str, asset: str) -> str:
    """Return the GitHub release download URL for a robotsix-ui asset."""
    return (
        "https://github.com/damien-robotsix/robotsix-ui/"
        f"releases/download/{version}/{asset}"
    )


def css_url(version: str) -> str:
    """Return the GitHub release download URL for a robotsix-ui version."""
    return asset_url(version, "style.css")


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(  # noqa: S310
        url, headers={"User-Agent": "robotsix-central-deploy"}
    )
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                return response.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError) as exc:
            last_error = exc
            time.sleep(2**attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}") from last_error


def main(argv: list[str] | None = None) -> int:
    version = argv[0] if argv else ROBOTSIX_UI_VERSION
    if not _VERSION_RE.fullmatch(version):
        print(f"invalid robotsix-ui version: {version!r}", file=sys.stderr)
        return 2
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    for asset, filename in _ASSETS.items():
        url = asset_url(version, asset)
        data = _fetch(url)
        if not data:
            print(f"refusing to write empty {filename} from {url}", file=sys.stderr)
            return 1
        target = _STATIC_DIR / filename
        target.write_bytes(data)
        print(f"wrote {target} ({len(data)} bytes from {url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
