"""Immutable task-local settings shared by search tools and middleware."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class EffectiveRunConfig:
    enable_web_image_read: bool
    web_image_read_mode: Literal["auto", "native", "vision"]
    jina_retain_images: Literal["alt", "all"]
    explore_max_waves: int
    source: Literal["product", "eval"] = "product"
    explore_on_start: bool = False
    enable_p1_evidence_reasoning: bool = False
    enable_p2_context_memory: bool = False
    enable_adaptive_collection: bool = False
    p2_context_window: int = 0
    p2_summary_fraction: float = 0.08
    p2_max_experiences: int = 3
    p2_experience_max_chars: int = 2400

    @classmethod
    def from_settings(cls, *, for_eval: bool = False) -> EffectiveRunConfig:
        from searchos.config.settings import settings

        return cls(
            enable_web_image_read=False if for_eval else settings.enable_web_image_read,
            web_image_read_mode="auto" if for_eval else settings.web_image_read_mode,
            jina_retain_images="alt" if for_eval else settings.effective_jina_retain_images(),
            explore_max_waves=settings.explore_max_waves,
            source="eval" if for_eval else "product",
            explore_on_start=for_eval and settings.enable_explore,
            enable_p1_evidence_reasoning=settings.enable_p1_evidence_reasoning,
            enable_p2_context_memory=settings.enable_p2_context_memory,
            enable_adaptive_collection=settings.enable_adaptive_collection,
            p2_context_window=settings.p2_context_window,
            p2_summary_fraction=settings.p2_summary_fraction,
            p2_max_experiences=settings.p2_max_experiences,
            p2_experience_max_chars=settings.p2_experience_max_chars,
        )

    @property
    def enhancements_enabled(self) -> bool:
        return (self.enable_p1_evidence_reasoning or self.enable_p2_context_memory
                or self.enable_adaptive_collection)

    def with_modules(self, manifest: dict[str, object]) -> EffectiveRunConfig:
        """Freeze a task's selected modules without mutating process settings."""
        selected = normalize_module_config(manifest)["enabled"]
        return replace(
            self,
            enable_p1_evidence_reasoning="evidence_revision" in selected,
            enable_p2_context_memory="context_memory" in selected,
            enable_adaptive_collection="adaptive_collection" in selected,
        )

    def module_config(self) -> dict[str, object]:
        selected = []
        if self.enable_p1_evidence_reasoning:
            selected.append("evidence_revision")
        if self.enable_p2_context_memory:
            selected.append("context_memory")
        if self.enable_adaptive_collection:
            selected.append("adaptive_collection")
        return normalize_module_config({"enabled": selected})

    def snapshot(self) -> dict[str, object]:
        return asdict(self)


def normalize_module_config(manifest: dict[str, object] | None = None) -> dict[str, object]:
    """Stable persisted module contract; unsupported versions never run silently."""
    manifest = manifest or {}
    if manifest.get("schema_version", 1) != 1:
        raise ValueError("Unsupported module configuration schema")
    if str(manifest.get("implementation_version", "1")) != "1":
        raise ValueError("Unsupported module implementation version")
    selected = manifest.get("enabled", [])
    allowed = ("evidence_revision", "context_memory", "adaptive_collection")
    if not isinstance(selected, list) or any(item not in allowed for item in selected):
        raise ValueError("Unknown research module")
    return {"schema_version": 1, "enabled": [name for name in allowed if name in selected],
            "implementation_version": "1"}


def uses_grounded_findings(state: Any) -> bool:
    """Hybrid keeps its existing policy; enhanced Local uses the same source gates."""
    mode = (state.source_scope or {}).get("mode")
    if mode == "hybrid":
        return True
    return (mode == "local" and state.research_mode == "findings"
            and bool(normalize_module_config(state.module_config)["enabled"]))


_active: ContextVar[EffectiveRunConfig | None] = ContextVar(
    "searchos_effective_run_config", default=None,
)


def current_run_config() -> EffectiveRunConfig:
    return _active.get() or EffectiveRunConfig.from_settings()


@contextmanager
def activate_run_config(config: EffectiveRunConfig) -> Iterator[None]:
    token = _active.set(config)
    try:
        yield
    finally:
        _active.reset(token)
