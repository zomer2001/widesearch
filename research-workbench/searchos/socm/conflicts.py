"""单元格冲突的纯状态投影；只采纳当前内容对应的已复核判断。"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from searchos.socm.evidence import EvidenceNode
from searchos.socm.values import is_missing_value

COMPARISON_VERSION = "cell-semantics-v1"
P1_COMPARISON_VERSION = "cell-semantics-p1-v2"


def normalize_text(text: str) -> str:
    """只规范 Unicode 和空白，保留否定、大小写、数值与版本。"""
    return " ".join(unicodedata.normalize("NFC", text).split())


@dataclass(frozen=True)
class EvidencePair:
    cell_key: str
    fingerprint: str
    left: EvidenceNode
    right: EvidenceNode
    context: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.fingerprint,
            **self.context,
            "a": _claim(self.left),
            "b": _claim(self.right),
        }


def _claim(node: EvidenceNode) -> dict[str, Any]:
    return {
        "value": node.value or node.finding,
        "excerpt": node.source_excerpt,
        "source": node.source,
        "alignment": node.alignment,
        "alignment_note": node.alignment_note,
    }


def _source_grounding(state: Any, node: EvidenceNode | None) -> dict[str, Any]:
    """Check the saved reading window and snapshot its current identity/content.

    The digest includes the actual text, not just its declared hash, so even a
    corrupt observation invalidates an earlier cached comparison. No source
    text is copied into the comparison prompt or this compact audit snapshot.
    """
    observations = [
        item for item in state.evidence_graph.observations
        if node is not None and item.id == node.observation_id
    ]
    snapshot = {
        "valid": False,
        "observations": [
            {
                "id": item.id,
                "source": item.source,
                "text_hash": item.text_hash,
                "actual_hash": "sha256:" + hashlib.sha256(item.text.encode()).hexdigest(),
                "local_source_metadata": item.local_source_metadata,
            }
            for item in observations
        ],
    }
    if (node is None or not node.observation_id or len(observations) != 1
            or not node.source_excerpt or node.span is None):
        return snapshot
    observation = observations[0]
    if (observation.source != node.source
            or observation.local_source_metadata != node.local_source_metadata
            or observation.text_hash != snapshot["observations"][0]["actual_hash"]):
        return snapshot
    if len(node.span) != 2:
        return snapshot
    start, end = node.span
    if not (type(start) is int and type(end) is int
            and 0 <= start < end <= len(observation.text)):
        return snapshot
    if observation.text[start:end] != node.source_excerpt:
        return snapshot
    algorithm, _, digest = node.text_hash.partition(":")
    snapshot["valid"] = algorithm in {"sha1", "sha256"} and digest == hashlib.new(
        algorithm, node.source_excerpt.encode()
    ).hexdigest()
    return snapshot


def _annotation_context(state: Any, node: EvidenceNode) -> dict[str, Any]:
    """Snapshot annotation and ORIGINAL evidence, never a cell's projected copy."""
    from searchos.socm.claim_annotations import evidence_fingerprint

    original = state.evidence_graph.get_node(node.id)
    annotation = state.evidence_graph.claim_annotations.get(node.id)
    return {
        "annotation": annotation.model_dump(mode="json") if annotation is not None else None,
        "source_fingerprint": evidence_fingerprint(original) if original is not None else None,
        "grounding": _source_grounding(state, original),
    }


def _temporal_preflight(
    state: Any, left: EvidenceNode, right: EvidenceNode,
    source_contexts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Only verified, fresh, scope-preserving projections may enter P1 rules.

    Returning a rule outcome does not choose a winning value. All metadata in
    this audit block remains data when a pair falls back to the model reviewer.
    """
    from searchos.socm.symbolic_revision import Claim, compare_claims

    claims = []
    for projected in (left, right):
        original = state.evidence_graph.get_node(projected.id)
        annotation = state.evidence_graph.claim_annotations.get(projected.id)
        source_context = source_contexts[projected.id]
        if (
            original is None or annotation is None or not annotation.verified
            or annotation.evidence_id != original.id
            or annotation.evidence_fingerprint != source_context["source_fingerprint"]
        ):
            return {"enabled": True, "outcome": "not_applied", "reason": "缺少当前已核验注释"}
        if not source_context["grounding"]["valid"]:
            return {"enabled": True, "outcome": "not_applied", "reason": "原始来源窗口或定位已失效"}
        # A valid annotation about the stored fact does not automatically verify
        # a later binding that changes its entity, attribute, or claimed value.
        expected = (annotation.entity, annotation.attribute, annotation.value)
        actual = (projected.entity, projected.attribute, projected.value or projected.finding)
        if any(not normalize_text(a) or normalize_text(a) != normalize_text(b)
               for a, b in zip(expected, actual)) or not annotation.scope_key.strip():
            return {"enabled": True, "outcome": "not_applied", "reason": "单元格投影超出注释范围"}
        interval = annotation.valid_time
        if (
            interval is None or not normalize_text(interval.basis_quote)
            or normalize_text(interval.basis_quote) not in normalize_text(original.source_excerpt)
        ):
            return {"enabled": True, "outcome": "not_applied", "reason": "有效时间未锚定原文"}
        claims.append(Claim.from_evidence(
            projected, scope_key=annotation.scope_key, temporal=interval,
        ))
    if normalize_text(claims[0].scope_key) != normalize_text(claims[1].scope_key):
        return {"enabled": True, "outcome": "not_applied", "reason": "事实适用范围不同"}
    decision = compare_claims(*claims)
    return {
        "enabled": True,
        "outcome": decision.outcome,
        "rule": decision.rule,
        "reason": decision.reason,
        "temporal_relation": decision.temporal_relation.value,
        "scope": claims[0].scope_key,
        "a_time": claims[0].temporal.model_dump(mode="json"),
        "b_time": claims[1].temporal.model_dump(mode="json"),
    }


def cell_pairs(state: Any, key: str, *, p1_enabled: bool | None = None) -> list[EvidencePair]:
    """比较完整投影；只归并内容和上下文完全相同的重复证据。"""
    from searchos.socm.coverage import evidence_source_allowed, project_cell_evidence

    cmap = state.coverage_map
    if p1_enabled is None:
        p1_enabled = getattr(cmap, "p1_enabled", False)
    target = cmap.cell_binding(key)
    cell = cmap.cells.get(key)
    if target is None or cell is None:
        return []
    tid, entity, attribute = target
    schema = cmap.tables[tid]
    column = schema.column_desc.get(attribute)
    context = {
        "entity": entity,
        "attribute": attribute,
        "column": column.model_dump() if column is not None else {},
        "table": schema.table_label,
    }
    nodes = []
    seen = set()
    source_contexts = {}
    for fid in sorted(set(cell.supporting_evidence_ids)):
        node = state.evidence_graph.get_node(fid)
        if node is None or not evidence_source_allowed(state, node):
            continue
        node = project_cell_evidence(cmap, node, *target)
        if node is None or is_missing_value(node.value or node.finding):
            continue
        identity_data: Any = _claim(node)
        if p1_enabled:
            # Hash a potentially long reading window once per candidate, not
            # again for every pair that consumes the same evidence.
            source_contexts[node.id] = _annotation_context(state, node)
            identity_data = [identity_data, source_contexts[node.id]]
        identity = json.dumps(identity_data, sort_keys=True, ensure_ascii=False)
        if identity not in seen:
            nodes.append(node)
            seen.add(identity)
    pairs = []
    for left, right in combinations(nodes, 2):
        if normalize_text(left.value or left.finding) == normalize_text(
            right.value or right.finding
        ):
            continue
        pair_context = dict(context)
        annotations = None
        if p1_enabled:
            pair_context["p1"] = _temporal_preflight(state, left, right, source_contexts)
            annotations = [source_contexts[left.id], source_contexts[right.id]]
        content = [
            COMPARISON_VERSION,
            key,
            state.source_scope,
            pair_context,
            left.id,
            _claim(left),
            right.id,
            _claim(right),
        ]
        if p1_enabled:
            # Keep the disabled path byte-for-byte compatible with old caches;
            # the enabled path has its own version, mode and annotation snapshot.
            content = [P1_COMPARISON_VERSION, True, annotations, *content[1:]]
        fingerprint = hashlib.sha256(
            json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        pairs.append(EvidencePair(key, fingerprint, left, right, pair_context))
    return pairs


def reconcile_conflicts(state: Any, keys: set[str], *, p1_enabled: bool | None = None) -> None:
    """刷新当前标记，历史记录不参与计数；本函数不调用模型。"""
    from searchos.socm.coverage import evidence_source_allowed, project_cell_evidence

    cmap = state.coverage_map
    explicit = state.evidence_graph.get_conflicts()
    for key in keys:
        cell = cmap.cells.get(key)
        target = cmap.cell_binding(key)
        if cell is None or target is None:
            continue
        active_ids: set[str] = set()
        uncertain = pending = False
        assessments = cmap.evidence_comparisons.get(key, {})
        for pair in cell_pairs(state, key, p1_enabled=p1_enabled):
            result = assessments.get(pair.fingerprint)
            if result is None:
                pending = True
            elif result.verdict == "uncertain":
                uncertain = True
            elif result.verdict == "contradictory" and result.verified:
                active_ids.update((pair.left.id, pair.right.id))
            elif result.verdict == "contradictory":
                uncertain = True
        # 显式关系保留兼容，但失效、撤销或移出本单元格的证据不再算冲突。
        for left, right in explicit:
            if all(
                node.id in cell.supporting_evidence_ids
                and evidence_source_allowed(state, node)
                and project_cell_evidence(cmap, node, *target) is not None
                for node in (left, right)
            ):
                active_ids.update((left.id, right.id))
        cell.conflict_history_evidence_ids = sorted(
            set(cell.conflict_history_evidence_ids + cell.conflict_evidence_ids) | active_ids
        )
        cell.conflict_evidence_ids = sorted(active_ids)
        cell.has_conflict = bool(active_ids)
        cell.conflict_status = (
            "confirmed"
            if active_ids
            else "pending"
            if pending
            else "uncertain"
            if uncertain
            else "clear"
        )
