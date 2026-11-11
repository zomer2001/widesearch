"""Small JSON-serializable contracts; no browser, model client or database dependencies."""

from pydantic import BaseModel, Field


class Goal(BaseModel):
    text: str = ""
    terms: list[str] = Field(default_factory=list)
    # None means unassessed, NOT complete. Execution completion is not coverage.
    gap: float | None = None
    basis: str = "unassessed"


class Candidate(BaseModel):
    url: str
    title: str = ""
    text: str = ""
    links: list[str] = Field(default_factory=list)
    read: bool = False
    content_hash: str = ""


class Feedback(BaseModel):
    task_id: str
    source: str
    new: int = 0
    corroboration: int = 0
    repeated: int = 0
    evidence_ids: list[str] = Field(default_factory=list)


class CollectionState(BaseModel):
    version: int = 1
    goals: dict[str, Goal] = Field(default_factory=dict)
    candidates: dict[str, Candidate] = Field(default_factory=dict)
    # Content-based batch identities survive retries and restarts.
    batches: dict[str, Feedback] = Field(default_factory=dict)
    facts: dict[str, list[str]] = Field(default_factory=dict)
    communities: list[dict] = Field(default_factory=list)
    decisions: list[dict] = Field(default_factory=list)
    last_error: str = ""
