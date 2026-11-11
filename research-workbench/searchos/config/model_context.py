"""Task-local model bindings for the private workbench."""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_models: ContextVar[dict[str, Any] | None] = ContextVar("workbench_models", default=None)
_default_factory: Callable[[str], Any] | None = None


def set_default_model_factory(factory: Callable[[str], Any] | None) -> None:
    global _default_factory
    _default_factory = factory


def resolve_model_override(role: str) -> Any:
    models = _models.get()
    if models is not None and role in models:
        return models[role]
    return _default_factory(role) if _default_factory is not None else None


@contextmanager
def activate_models(models: dict[str, Any]):
    token = _models.set(models)
    try:
        yield
    finally:
        _models.reset(token)
