"""P2 durable goal text and feedback-grounded collection experience.

Cards describe an observed acquisition outcome, not a proven causal strategy
or a claim that source-supported statements are necessarily true.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from searchos.util.base_model import CamelModel


class GoalSnapshot(CamelModel):
    version: int = 0
    original_goal: str = ""
    user_messages: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)


class ExperienceFeedback(CamelModel):
    grounded_evidence_ids: list[str] = Field(default_factory=list)
    # None means no dispatch baseline was captured; never infer "new" from a
    # task's final cumulative count or a model's claim of success.
    new_evidence_ids: list[str] | None = None
    credited_evidence_ids: list[str] = Field(default_factory=list)
    newly_filled_cells: list[str] | None = None
    unique_supported_cells: list[str] = Field(default_factory=list)
    repeated_evidence_count: int = 0
    duration_s: float | None = None
    query_count: int | None = None
    token_count: int | None = None
    interpretation: str = (
        "Observed source-grounded yield; correctness and causality are not established."
    )


class ExperienceCard(CamelModel):
    id: str
    execution_id: str
    payload_fingerprint: str
    task_id: str
    agent_id: str
    question: str
    scope_key: str = "answer"
    source_scope_fingerprint: str
    queries: list[str] = Field(default_factory=list)
    routes: list[dict] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    target_cells: list[str] = Field(default_factory=list)
    evidence_versions: dict[str, str] = Field(default_factory=dict)
    artifact_versions: dict[str, int] = Field(default_factory=dict)
    feedback: ExperienceFeedback = Field(default_factory=ExperienceFeedback)
    status: Literal["active", "insufficient_feedback", "revalidation_required"] = "active"
    invalidation_reasons: list[str] = Field(default_factory=list)


class ContextMemory(CamelModel):
    enabled: bool = False
    goal: GoalSnapshot = Field(default_factory=GoalSnapshot)
    experiences: dict[str, ExperienceCard] = Field(default_factory=dict)
    # Award acquisition credit once per evidence identity and use scope.
    credited_evidence: dict[str, list[str]] = Field(default_factory=dict)
    compaction_metrics: list[dict] = Field(default_factory=list)


class ContextPacket(CamelModel):
    content: str
    task_id: str = "__answer__"
    scope_key: str = "answer"
    goal_fingerprint: str
    state_fingerprint: str = ""
    source_scope_fingerprint: str
    goal_version: int = 0
    user_messages: list[str] = Field(default_factory=list)
    experience_ids: list[str] = Field(default_factory=list)
    read_evidence: dict[str, str] = Field(default_factory=dict)
    read_artifacts: dict[str, int] = Field(default_factory=dict)
    experience_fingerprints: dict[str, str] = Field(default_factory=dict)
    experience_chars: int = 0
