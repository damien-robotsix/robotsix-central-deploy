"""Registry loader: reads and validates component configs and env/secrets from disk."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from robotsix_central_deploy.lifecycle._yaml_utils import (
    InvalidConfigStructureError,
    YamlParseError,
    YamlReadError,
    read_yaml_file,
)

from .models import ComponentConfig


class RegistryLoadError(ValueError):
    """Raised when the component registry file is absent or invalid."""


class ComponentRegistry:
    """In-memory index of declared components, loaded from YAML."""

    def __init__(self, components: list[ComponentConfig]) -> None:
        self._index: dict[str, ComponentConfig] = {c.id: c for c in components}

    # -- factory ------------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: Path) -> "ComponentRegistry":
        """
        Load and validate a component registry YAML file.

        Expected file shape::

            components:
              - id: my-service
                image: ghcr.io/your-org/my-service:latest
                container_name: my-service
                ports:
                  - host: 8080
                    container: 8080
                mounts: []
                env: {}
                health_check:
                  test: ["CMD", "curl", "-f", "http://localhost:8080/health"]

        Raises ``RegistryLoadError`` on missing file, YAML syntax errors,
        or Pydantic validation failures.
        """
        if not path.exists():
            raise RegistryLoadError(f"Registry file not found: {path}")
        try:
            raw = read_yaml_file(path)
        except YamlReadError as exc:
            raise RegistryLoadError(f"Failed to read {path}: {exc}") from exc
        except YamlParseError as exc:
            raise RegistryLoadError(f"Invalid YAML in {path}: {exc}") from exc
        except InvalidConfigStructureError as exc:
            raise RegistryLoadError(f"Invalid structure in {path}: {exc}") from exc

        if not isinstance(raw, dict) or "components" not in raw:
            raise RegistryLoadError(
                f"Registry file {path} must have a top-level 'components' list"
            )

        components: list[ComponentConfig] = []
        from pydantic import ValidationError

        for i, entry in enumerate(raw["components"]):
            try:
                components.append(ComponentConfig.model_validate(entry))
            except ValidationError as exc:
                raise RegistryLoadError(
                    f"Invalid component entry at index {i} in {path}: {exc}"
                ) from exc
        return cls(components)

    # -- query --------------------------------------------------------------

    def register(self, config: ComponentConfig) -> None:
        """Add or replace a component in the in-memory index."""
        self._index[config.id] = config

    def unregister(self, id: str) -> None:
        """Remove *id* from the in-memory index. No-op if absent."""
        self._index.pop(id, None)

    def get(self, component_id: str) -> Optional[ComponentConfig]:
        """Return the component with *component_id*, or ``None``."""
        return self._index.get(component_id)

    def all(self) -> list[ComponentConfig]:
        """Return all registered components in declaration order."""
        return list(self._index.values())
