"""pyyaml-based helpers replacing robotsix_yaml_config primitives."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class YamlReadError(OSError):
    """Raised when a YAML file cannot be opened or read."""


class YamlParseError(ValueError):
    """Raised when file content cannot be parsed as YAML."""


class InvalidConfigStructureError(ValueError):
    """Raised when parsed YAML is not a mapping."""


def read_yaml_file(path: Path | str) -> dict[str, Any]:
    """Read *path*, parse as YAML, and return a dict.

    Raises:
        YamlReadError: if the file cannot be opened.
        YamlParseError: if the content is not valid YAML.
        InvalidConfigStructureError: if the top-level value is not a dict.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise YamlReadError(f"Cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise YamlParseError(f"YAML parse error in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidConfigStructureError(
            f"Expected a mapping at top level of {path}, got {type(data).__name__}"
        )
    return data
