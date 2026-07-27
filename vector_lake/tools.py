"""Backward-compatible lazy public tool exports."""

from vector_lake.tool_registry import __all__
from vector_lake.tool_registry import __getattr__ as _registry_getattr


def __getattr__(name: str):
    """Resolve once and retain the value on the compatibility module."""
    value = _registry_getattr(name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
