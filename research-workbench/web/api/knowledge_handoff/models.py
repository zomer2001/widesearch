"""The shared contract between Workbuddy, the CLI, and the web importer."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.research_mapping import DocumentExtraction

from . import SCHEMA_VERSION


class SummarySubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=40000)
    kind: Literal["extractive", "abstractive"] = "extractive"
    quotes: list[str] = Field(min_length=1, max_length=200)


class ResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["knowledge-handoff-v2"] = SCHEMA_VERSION
    document_id: str = Field(min_length=1, max_length=100)
    source_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    outputs: list[DocumentExtraction] = Field(default_factory=list, max_length=500)
    summary: SummarySubmission | None = None
    status: Literal["completed", "partial", "no_relevant_content"] = "completed"
    notes: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def consistent_status(self):
        has_facts = any(
            output.entities or output.relations or output.records for output in self.outputs
        )
        if self.status == "no_relevant_content" and has_facts:
            raise ValueError("no_relevant_content 不能同时包含抽取事实")
        if self.status == "completed" and not has_facts and self.summary is None:
            raise ValueError(
                "空结果不能标为 completed；未发现相关内容用 no_relevant_content，未完成用 partial"
            )
        if not self.outputs and self.summary is None and self.status != "no_relevant_content":
            raise ValueError("结果必须包含抽取内容或独立摘要")
        return self
