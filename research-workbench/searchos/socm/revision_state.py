"""Persisted P1 dependencies, execution tickets and confirmed revision events."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from searchos.util.base_model import CamelModel


class Artifact(CamelModel):
    id: str
    kind: Literal["task", "section", "report"]
    scope_key: str = "answer"
    epoch: int = 0
    version: int = 0
    status: Literal["active", "stale"] = "active"
    content: str = ""
    read_evidence: dict[str, str] = Field(default_factory=dict)
    read_artifacts: dict[str, int] = Field(default_factory=dict)
    # OR between groups, AND within each group; these are not source votes.
    support_sets: list[list[str]] = Field(default_factory=list)
    producer_task_id: str = ""


class Execution(CamelModel):
    id: str
    task_id: str
    assigned_agent_id: str = ""
    scope_key: str = "answer"
    task_epoch: int
    source_scope_fingerprint: str
    read_evidence: dict[str, str] = Field(default_factory=dict)
    read_artifacts: dict[str, int] = Field(default_factory=dict)
    output_epochs: dict[str, int] = Field(default_factory=dict)
    # Epochs reject revision races; versions also reject another producer's
    # normal write within the same epoch. Own sequential writes advance this.
    output_versions: dict[str, int] = Field(default_factory=dict)
    invalidated: bool = False
    closed: bool = False


class ConfirmedEvidenceRevision(CamelModel):
    id: str
    old_evidence_id: str
    new_evidence_id: str
    scope_key: str = "answer"
    reason: str
    confirmed: bool = False
    expected_old_version: str
    expected_new_version: str


class RevisionImpact(CamelModel):
    revision_id: str
    affected_artifact_ids: list[str] = Field(default_factory=list)
    affected_task_ids: list[str] = Field(default_factory=list)
    surviving_support_sets: dict[str, list[list[str]]] = Field(default_factory=dict)
    unchanged_ids: list[str] = Field(default_factory=list)
    invalidated_execution_ids: list[str] = Field(default_factory=list)
    already_applied: bool = False


class AppliedRevision(CamelModel):
    payload_fingerprint: str
    event: ConfirmedEvidenceRevision
    impact: RevisionImpact


class RevisionMemory(CamelModel):
    enabled: bool = False
    artifacts: dict[str, Artifact] = Field(default_factory=dict)
    # Published contents remain inspectable after replacement, together with
    # the exact dependency versions used to generate them.
    artifact_history: dict[str, list[Artifact]] = Field(default_factory=dict)
    executions: dict[str, Execution] = Field(default_factory=dict)
    retired_uses: dict[str, list[str]] = Field(default_factory=dict)
    events: dict[str, AppliedRevision] = Field(default_factory=dict)
