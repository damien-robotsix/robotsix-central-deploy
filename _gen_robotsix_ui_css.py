"""Generate ``src/robotsix_central_deploy/ui/static/robotsix-ui.css``.

Fetches the compiled robotsix-ui stylesheet from its GitHub Release asset at
build time so central-deploy never re-vendors a stale, one-shot copy. The URL
construction mirrors the ``robotsix_ui.css_url()`` helper shipped in the
robotsix-ui repository.
"""

from __future__ import annotations

import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROBOTSIX_UI_VERSION = "v0.1.30"

_VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")

_STATIC_DIR = (
    Path(__file__).resolve().parent
    / "src"
    / "robotsix_central_deploy"
    / "ui"
    / "static"
)
_TARGET = _STATIC_DIR / "robotsix-ui.css"


def css_url(version: str) -> str:
    """Return the GitHub release download URL for a robotsix-ui version."""
    return (
        "https://github.com/damien-robotsix/robotsix-ui/"
        f"releases/download/{version}/style.css"
    )


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
    url = css_url(version)
    data = _fetch(url)
    if not data:
        print(f"refusing to write empty stylesheet from {url}", file=sys.stderr)
        return 1
    _TARGET.parent.mkdir(parents=True, exist_ok=True)
    _TARGET.write_bytes(data)
    print(f"wrote {_TARGET} ({len(data)} bytes from {url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
