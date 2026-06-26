from pathlib import Path

import pytest

from robotsix_central_deploy.registry import ComponentRegistry, RegistryLoadError


class TestComponentRegistry:
    def _write_registry(self, tmp_path, data: str) -> Path:
        p = tmp_path / "components.yaml"
        p.write_text(data, encoding="utf-8")
        return p

    def test_load_valid_yaml(self, tmp_path):
        path = self._write_registry(
            tmp_path,
            """
components:
  - id: svc-a
    image: repo/svc-a:latest
    container_name: svc-a
  - id: svc-b
    image: repo/svc-b:v1
    container_name: svc-b
    ports:
      - host: 9000
        container: 9000
""",
        )
        registry = ComponentRegistry.from_yaml(path)
        assert len(registry.all()) == 2

    def test_get_by_id(self, tmp_path):
        path = self._write_registry(
            tmp_path,
            """
components:
  - id: my-svc
    image: repo:latest
    container_name: my-svc
""",
        )
        registry = ComponentRegistry.from_yaml(path)
        comp = registry.get("my-svc")
        assert comp is not None
        assert comp.image == "repo:latest"

    def test_get_missing_returns_none(self, tmp_path):
        path = self._write_registry(tmp_path, "components: []")
        registry = ComponentRegistry.from_yaml(path)
        assert registry.get("nope") is None

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(RegistryLoadError, match="not found"):
            ComponentRegistry.from_yaml(tmp_path / "no-such-file.yaml")

    def test_invalid_yaml_raises(self, tmp_path):
        path = self._write_registry(tmp_path, ":: bad : yaml :[")
        with pytest.raises(RegistryLoadError, match="Invalid YAML"):
            ComponentRegistry.from_yaml(path)

    def test_missing_components_key_raises(self, tmp_path):
        path = self._write_registry(tmp_path, "services: []")
        with pytest.raises(RegistryLoadError, match="top-level 'components'"):
            ComponentRegistry.from_yaml(path)

    def test_invalid_schema_raises(self, tmp_path):
        # Missing required 'image' field
        path = self._write_registry(
            tmp_path,
            """
components:
  - id: broken-svc
    container_name: broken
""",
        )
        with pytest.raises(RegistryLoadError, match="Invalid component entry at index 0"):
            ComponentRegistry.from_yaml(path)

    def test_seed_file_loads_all_six_services(self):
        """The bundled config/components.yaml must load without errors."""
        path = Path("config/components.yaml")
        registry = ComponentRegistry.from_yaml(path)
        ids = {c.id for c in registry.all()}
        expected = {"cost-monitor", "calendar-agent", "auto-mail", "chat", "broker", "radicale"}
        assert ids == expected
