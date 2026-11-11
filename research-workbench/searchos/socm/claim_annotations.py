"""Version-bound semantic annotations; original evidence remains authoritative."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from searchos.socm.temporal import TemporalScope
from searchos.util.base_model import CamelModel


class EvidenceClaimAnnotation(CamelModel):
    """A checked interpretation of one immutable source-backed evidence version.

    ``scope_key`` excludes time and identifies the factual scope (e.g. branch,
    role, units). Unknown time is retained as None. ``verified`` is the result
    of source-support review, not a declaration that a source is infallible.
    """

    evidence_id: str
    evidence_fingerprint: str
    entity: str
    attribute: str
    value: str
    scope_key: str
    valid_time: TemporalScope | None = None
    verified: bool = False
    reason: str = ""


def evidence_fingerprint(node: Any) -> str:
    """Bind annotations to the exact persisted input, not just its evidence ID."""
    payload = {
        key: getattr(node, key, None)
        for key in (
            "id", "finding", "value", "entity", "attribute", "table_id", "source",
            "source_excerpt", "observation_id", "span", "text_hash", "status",
            "alignment", "alignment_note", "local_source_metadata",
        )
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()
