"""Independent, version-bound summaries. Never rewrite source documents."""

from __future__ import annotations

import json
import re
from pathlib import Path

from api.research_mapping import Source, key
from searchos.knowledge_store.database import now
from searchos.local.index.source_registry import _atomic_write

from .models import SummarySubmission


def validate_summary(source: Source, submission: SummarySubmission | dict) -> dict:
    value = SummarySubmission.model_validate(submission)
    text = value.text.strip()
    if not text:
        raise ValueError("摘要不能为空")
    quotes = list(dict.fromkeys(source.quote(q) for q in value.quotes))
    # Extractive summaries consist only of the supplied verbatim quotations.
    # Abstractive summaries remain labelled as derived, not verified source text.
    if value.kind == "extractive":
        lines = [
            re.sub(r"^(?:[-*•]|\d+[.)、])\s*", "", line).strip()
            for line in text.splitlines()
            if line.strip()
        ]

        def normalized(s):
            return " ".join(s.split())

        if not all(any(normalized(line) == normalized(q) for q in quotes) for line in lines):
            raise ValueError("摘录式摘要只能由所列原句组成；改写内容请标记为 abstractive")
    return {
        "text": text,
        "kind": value.kind,
        "evidence": [
            {"quote": q, "start": source.text.index(q), "end": source.text.index(q) + len(q)}
            for q in quotes
        ],
        "verification": "literal_quotes_checked",
        "semantic_review": "not_reviewed",
    }


def save_summary(artifact_root: Path, source: Source, submission, *, slug: str) -> dict:
    content = validate_summary(source, submission)
    identity = "summary_" + key(
        slug, source.library_id, source.source_id, source.source_revision, source.digest, content
    )
    root = Path(artifact_root) / "summaries"
    target = root / f"{identity}.json"
    if target.exists():
        return json.loads(target.read_text(encoding="utf-8"))
    record = {
        "id": identity,
        "slug": slug,
        "document_id": source.source_id,
        "library_id": source.library_id,
        "source_revision": source.source_revision,
        "text_sha256": source.digest,
        "title": source.title,
        "url": source.url,
        "created_at": now(),
        "producer": "external_handoff",
        "model_calls": 0,
        **content,
    }
    root.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return record


def list_summaries(
    artifact_root: Path,
    *,
    slug: str,
    document_id: str | None = None,
    source_revision: str | None = None,
    text_sha256: str | None = None,
) -> list[dict]:
    records = []
    for path in (Path(artifact_root) / "summaries").glob("summary_*.json"):
        if path.is_symlink():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("slug") != slug:
                continue
            if document_id and record.get("document_id") != document_id:
                continue
            record["current"] = (
                (
                    record.get("source_revision") == source_revision
                    and (text_sha256 is None or record.get("text_sha256") == text_sha256)
                )
                if source_revision
                else None
            )
            records.append(record)
        except (OSError, ValueError, TypeError):
            continue
    return sorted(records, key=lambda r: r.get("created_at", ""), reverse=True)
