"""Unit tests for _yaml_utils.py."""

from pathlib import Path

import pytest

from robotsix_central_deploy._yaml_utils import (
    InvalidConfigStructureError,
    YamlParseError,
    YamlReadError,
    read_yaml_file,
)


def test_read_yaml_file_success(tmp_path: Path) -> None:
    path = tmp_path / "good.yaml"
    path.write_text("key: value\n")
    result = read_yaml_file(path)
    assert result == {"key": "value"}


def test_read_yaml_file_missing_file_raises_yaml_read_error() -> None:
    with pytest.raises(YamlReadError) as exc_info:
        read_yaml_file("/nonexistent/path.yaml")
    assert "Cannot read" in str(exc_info.value)


def test_read_yaml_file_malformed_yaml_raises_yaml_parse_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(": invalid yaml :::\n")
    with pytest.raises(YamlParseError) as exc_info:
        read_yaml_file(path)
    assert "YAML parse error" in str(exc_info.value)


def test_read_yaml_file_non_dict_raises_invalid_config_structure_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- item1\n- item2\n")
    with pytest.raises(InvalidConfigStructureError) as exc_info:
        read_yaml_file(path)
    assert "Expected a mapping" in str(exc_info.value)
