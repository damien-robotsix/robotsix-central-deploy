"""Registry loader: in-memory index of declared components."""

from __future__ import annotations

from typing import Optional

from .models import ComponentConfig


class ComponentRegistry:
    """In-memory index of declared components, loaded from YAML."""

    def __init__(self, components: list[ComponentConfig]) -> None:
        self._index: dict[str, ComponentConfig] = {c.id: c for c in components}

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
