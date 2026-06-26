"""Registry check subpackage — detects outdated container images by comparing
the deployed manifest digest against the registry's current manifest."""

from .checker import RegistryChecker

__all__ = ["RegistryChecker"]
