from robotsix_central_deploy.registry import ComponentRegistry
from robotsix_central_deploy.registry.models import ComponentConfig


def _component(component_id: str, image: str = "repo:latest") -> ComponentConfig:
    return ComponentConfig.model_validate(
        {"id": component_id, "image": image, "container_name": component_id}
    )


class TestComponentRegistry:
    def test_all_returns_components_in_order(self):
        registry = ComponentRegistry([_component("svc-a"), _component("svc-b")])
        assert [c.id for c in registry.all()] == ["svc-a", "svc-b"]

    def test_get_by_id(self):
        registry = ComponentRegistry([_component("my-svc", image="repo:latest")])
        comp = registry.get("my-svc")
        assert comp is not None
        assert comp.image == "repo:latest"

    def test_get_missing_returns_none(self):
        registry = ComponentRegistry([])
        assert registry.get("nope") is None

    def test_register_adds_and_replaces(self):
        registry = ComponentRegistry([])
        registry.register(_component("svc", image="repo:v1"))
        registry.register(_component("svc", image="repo:v2"))
        comp = registry.get("svc")
        assert comp is not None
        assert comp.image == "repo:v2"
        assert len(registry.all()) == 1

    def test_unregister_removes_and_tolerates_absent(self):
        registry = ComponentRegistry([_component("svc")])
        registry.unregister("svc")
        assert registry.get("svc") is None
        registry.unregister("svc")  # no-op
