"""Caretaker background maintenance agent — daily passes over managed components."""

from __future__ import annotations


def __getattr__(name: str) -> object:
    if name == "CaretakerScheduler":
        from .scheduler import CaretakerScheduler

        return CaretakerScheduler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["CaretakerScheduler"]
