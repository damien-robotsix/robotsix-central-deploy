"""Guard the one-file config rule: no `ROBOTSIX_LIFECYCLE_*` value variables.

The fleet [config standard][std] gives a component exactly one config channel:
a single JSON file, located by `ROBOTSIX_CONFIG_FILE` — a variable that only
*locates* the file and never carries a value. `LifecycleConfig` is a plain
`BaseModel`, not `BaseSettings`, and nothing in `src/` reads `os.environ`.

The rule is easy to keep in code and easy to lose in prose. Before this guard,
the docs advertised 24 `ROBOTSIX_LIFECYCLE_*` variables — a mechanism that had
not existed since the robotsix_config migration. Four of them named settings no
model had, and 22 real fields went undocumented. An operator following those
docs configured nothing and had no way to tell.

A *glob* reference (`ROBOTSIX_LIFECYCLE_*`) is allowed and used deliberately —
`docker-compose.yml` and `docs/deployment.md` both say the family is dead, and
saying so is the opposite of the failure. What this module forbids is naming a
specific variable, which is what reads as an instruction to set one.

[std]: https://damien-robotsix.github.io/robotsix-standards/config-standard/
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: A concrete variable name — `ROBOTSIX_LIFECYCLE_` followed by at least one
#: name character. The bare `ROBOTSIX_LIFECYCLE_*` glob does not match, so
#: prose that documents the family as dead stays legal.
PHANTOM_VAR = re.compile(r"ROBOTSIX_LIFECYCLE_[A-Z0-9]")

#: Trees to scan.  Committed prose and code only.
SCANNED_TREES = ("docs", "src", "tests")

#: Top-level files that carry the same operator-facing promises.
SCANNED_FILES = ("README.md", "AGENT.md", "docker-compose.yml")

SCANNED_SUFFIXES = {".md", ".py", ".yml", ".yaml", ".json", ".toml", ".js"}

#: This file necessarily contains the pattern it forbids.
SELF = Path(__file__).name


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for tree in SCANNED_TREES:
        for path in (REPO_ROOT / tree).rglob("*"):
            if path.suffix not in SCANNED_SUFFIXES or not path.is_file():
                continue
            if "__pycache__" in path.parts or path.name == SELF:
                continue
            files.append(path)
    for name in SCANNED_FILES:
        path = REPO_ROOT / name
        if path.is_file():
            files.append(path)
    return files


def test_the_scan_is_not_vacuous() -> None:
    """A guard that walks nothing passes for the wrong reason."""
    files = _candidate_files()
    assert len(files) > 100, f"expected the repo's docs/src/tests, found {len(files)}"


def test_no_file_names_a_lifecycle_env_var() -> None:
    """Nothing may name a concrete `ROBOTSIX_LIFECYCLE_<NAME>` variable."""
    offenders: list[str] = []
    for path in _candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if PHANTOM_VAR.search(line):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()[:90]}")

    assert not offenders, (
        "Config values come from one JSON file located by ROBOTSIX_CONFIG_FILE; "
        "no ROBOTSIX_LIFECYCLE_* variable is read anywhere. Naming one reads as "
        "an instruction to set it.\n  " + "\n  ".join(offenders)
    )


def test_config_model_has_no_environment_channel() -> None:
    """`LifecycleConfig` must stay a plain model with no env overlay."""
    from pydantic import BaseModel

    from robotsix_central_deploy.lifecycle.config import LifecycleConfig

    assert issubclass(LifecycleConfig, BaseModel)
    # pydantic-settings would attach these; a plain BaseModel has neither.
    assert "env_prefix" not in LifecycleConfig.model_config
    assert "env_file" not in LifecycleConfig.model_config
    assert type(LifecycleConfig).__name__ == "ModelMetaclass", (
        "LifecycleConfig has grown a settings metaclass — an environment "
        "overlay is a second config channel, which the config standard forbids."
    )


@pytest.mark.parametrize("relative_path", ["src/robotsix_central_deploy", "tests"])
def test_no_module_reads_the_environment_for_config(relative_path: str) -> None:
    """No first-party module may reach into `os.environ` for a setting.

    ``tests`` is included because an ``os.environ`` write in a test is how the
    dead vocabulary survived: three test modules set variables nothing read,
    which made the mechanism look alive to anyone reading them.
    """
    offenders: list[str] = []
    for path in (REPO_ROOT / relative_path).rglob("*.py"):
        if "__pycache__" in path.parts or path.name == SELF:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "ROBOTSIX_LIFECYCLE" in line and ("environ" in line or "setenv" in line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")

    assert not offenders, (
        "these read or set a ROBOTSIX_LIFECYCLE_* variable, which nothing "
        f"consumes: {offenders}"
    )
