"""Deterministic P2 goal/experience operations; callers own SOCM transactions.

No extra model call, training or reward model is introduced. Route attribution
is at the completed attempt level, because the current tool telemetry does not
prove which individual query caused an extraction.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from searchos.socm.conflicts import _source_grounding
from searchos.socm.context_memory import ContextPacket, ExperienceCard, ExperienceFeedback
from searchos.socm.coverage import CellStatus
from searchos.socm.frontier import FrontierTaskStatus
from searchos.socm.revisions import (
    StaleExecutionError,
    artifact_usable,
    evidence_usable,
    evidence_version,
    source_scope_fingerprint,
)

if TYPE_CHECKING:
    from searchos.socm.state import SearchState


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def experience_fingerprint(card: ExperienceCard) -> str:
    """Identity of the full observed card, including feedback and review status."""
    return _digest(card.model_dump(mode="json"))


def _texts(values: Iterable[str]) -> list[str]:
    return list(
        dict.fromkeys(value for value in values if isinstance(value, str) and value.strip())
    )


def _message_texts(values: Iterable[str]) -> list[str]:
    return [value for value in values if isinstance(value, str) and value.strip()]


def _merge_messages(previous: list[str], incoming: Iterable[str]) -> list[str]:
    """Merge complete ordered human histories without erasing repeated updates.

    Callers provide the full retained human sequence, not a single incremental
    text. A shorter prefix can be an older concurrent snapshot. Overlap merge
    also accepts a retained suffix after a legacy checkpoint transition.
    """
    new = _message_texts(incoming)
    if not new or previous[: len(new)] == new:
        return previous[:]
    if not previous or new[: len(previous)] == previous:
        return new
    for count in range(min(len(previous), len(new)), 0, -1):
        if previous[-count:] == new[:count]:
            return [*previous, *new[count:]]
    return [*previous, *new]


def sync_goals(
    state: SearchState, *, user_messages: Iterable[str] = (), hard_constraints: Iterable[str] = ()
) -> None:
    """Save complete ordered trusted human history without rewriting its text."""
    goal = state.p2.goal
    messages = _merge_messages(goal.user_messages, user_messages)
    constraints = _texts([*goal.hard_constraints, *hard_constraints])
    if (goal.original_goal, goal.user_messages, goal.hard_constraints) != (
        state.intent,
        messages,
        constraints,
    ):
        goal.original_goal = state.intent
        goal.user_messages = messages
        goal.hard_constraints = constraints
        goal.version += 1


def _task(state: SearchState, task_id: str):
    return next((item for item in state.frontier.questions if item.id == task_id), None)


def _nonnegative(value: float | int | None):
    return value if value is not None and math.isfinite(value) and value >= 0 else None


def record_task_experience(
    state: SearchState,
    task_id: str,
    agent_id: str,
    evidence_ids: Iterable[str],
    *,
    execution_id: str,
    baseline_evidence_ids: Iterable[str] | None = None,
    baseline_filled_cells: Iterable[str] | None = None,
    duration_s: float | None = None,
    query_count: int | None = None,
    token_count: int | None = None,
) -> ExperienceCard | None:
    """Record a completed attempt using its own committed evidence IDs.

    The caller must obtain IDs and baseline from the extraction coordinator,
    never the LLM report. Rows/quotes are rechecked here. Duplicate evidence is
    auditable but earns no extra credit. Missing baseline/cost remains unknown.
    """
    if not state.p2.enabled:
        return None
    task = _task(state, task_id)
    if task is None or task.kind not in {"search", "explore"}:
        return None
    if task.status != FrontierTaskStatus.COMPLETED:
        return None
    if not execution_id or task.assigned_agent_id != agent_id:
        raise ValueError("Experience requires a completed attempt's execution and owner")
    ids = sorted(set(evidence_ids))
    baseline = None if baseline_evidence_ids is None else sorted(set(baseline_evidence_ids))
    filled_baseline = None if baseline_filled_cells is None else sorted(set(baseline_filled_cells))
    payload = _digest(
        [
            task_id,
            agent_id,
            ids,
            baseline,
            filled_baseline,
            _nonnegative(duration_s),
            _nonnegative(query_count),
            _nonnegative(token_count),
        ]
    )
    card_id = "experience:" + execution_id
    if card_id in state.p2.experiences:
        previous = state.p2.experiences[card_id]
        if previous.payload_fingerprint != payload:
            raise ValueError("Execution ID reused with different acquisition feedback")
        return previous
    scope = task.p1_scope_key
    grounded = []
    for eid in ids:
        node = state.evidence_graph.get_node(eid)
        if (
            node is not None
            and not node.source.startswith("agent://")
            and evidence_usable(state, eid, scope)
            and _source_grounding(state, node)["valid"]
        ):
            grounded.append(eid)
    prior_credit = set(state.p2.credited_evidence.get(scope, []))
    new_ids = None if baseline is None else sorted(set(grounded) - set(baseline))
    # When no before snapshot exists, do not upgrade first observation into
    # novelty. A card may be retained for audit, but is not a positive example.
    credited = sorted(set(new_ids or ()) - prior_credit)
    supported = []
    for key, cell in state.coverage_map.cells.items():
        supporting = {
            eid for eid in cell.supporting_evidence_ids if evidence_usable(state, eid, scope)
        }
        if (
            cell.status == CellStatus.FILLED
            and supporting
            and supporting <= set(credited)
            and not cell.has_conflict
            and cell.conflict_status == "clear"
        ):
            supported.append(key)
    filled = None if filled_baseline is None else sorted(set(supported) - set(filled_baseline))
    routes = [dict(item) for item in task.route_history if item.get("worker") == agent_id]
    # Attempts are associations, not query-level causal credit. A route marked
    # failure stays in the audit card and is explicitly labelled in rendering.
    queries = _texts(task.attempt_queries)
    source_nodes = [state.evidence_graph.get_node(eid) for eid in grounded]
    artifact_id = "task:" + task_id
    parents = {}
    if state.p1.enabled:
        if not artifact_usable(state, artifact_id):
            raise StaleExecutionError(
                "Task experience requires its current published task artifact"
            )
        parents[artifact_id] = state.p1.artifacts[artifact_id].version
    card = ExperienceCard(
        id=card_id,
        execution_id=execution_id,
        payload_fingerprint=payload,
        task_id=task_id,
        agent_id=agent_id,
        question=task.question,
        scope_key=scope,
        source_scope_fingerprint=source_scope_fingerprint(state),
        queries=queries,
        routes=routes,
        sources=_texts(node.source for node in source_nodes if node),
        target_cells=list(task.target_cells),
        evidence_versions={eid: evidence_version(state, eid) for eid in grounded},
        artifact_versions=parents,
        feedback=ExperienceFeedback(
            grounded_evidence_ids=grounded,
            new_evidence_ids=new_ids,
            credited_evidence_ids=credited,
            newly_filled_cells=filled,
            unique_supported_cells=sorted(supported),
            repeated_evidence_count=len(set(grounded) & (prior_credit | set(baseline or ()))),
            duration_s=_nonnegative(duration_s),
            query_count=_nonnegative(query_count),
            token_count=_nonnegative(token_count),
        ),
        status=(
            "active"
            if credited and any(route.get("outcome") == "success" for route in routes)
            else "insufficient_feedback"
        ),
    )
    state.p2.experiences[card.id] = card
    state.p2.credited_evidence[scope] = sorted(prior_credit | set(credited))
    return card


def _invalid_reasons(state: SearchState, card: ExperienceCard) -> list[str]:
    reasons = []
    if source_scope_fingerprint(state) != card.source_scope_fingerprint:
        reasons.append("source_scope_changed")
    for eid, version in card.evidence_versions.items():
        if not evidence_usable(state, eid, card.scope_key):
            reasons.append("evidence_unavailable:" + eid)
        elif evidence_version(state, eid) != version:
            reasons.append("evidence_changed:" + eid)
    for aid, version in card.artifact_versions.items():
        if not artifact_usable(state, aid) or state.p1.artifacts[aid].version != version:
            reasons.append("artifact_changed:" + aid)
    return reasons


def refresh_experiences(state: SearchState) -> list[str]:
    """Mark stale support for revalidation; never call its method ineffective."""
    changed = []
    for card in state.p2.experiences.values():
        reasons = _invalid_reasons(state, card)
        if reasons and (
            card.status != "revalidation_required" or card.invalidation_reasons != reasons
        ):
            card.status = "revalidation_required"
            card.invalidation_reasons = reasons
            changed.append(card.id)
    return changed


_STOP_WORDS = frozenset(
    (
        "the a an and or to of for from in on at is are be find search get current what which "
        "when how please information data source sources report research official website "
        "compare collect evaluate verify locate obtain inspect"
    ).split()
)
_GENERIC_CHINESE = frozenset({"获取", "查询", "检索", "官方", "来源", "资料", "比较", "核验"})


def _terms(text: str) -> set[str]:
    words = {
        part
        for part in re.findall(r"[a-z0-9]+", text.lower())
        if len(part) > 1 and part not in _STOP_WORDS
    }
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(segment[index : index + 2] for index in range(len(segment) - 1))
    return words - _GENERIC_CHINESE


def _goal_view(state: SearchState, task_id: str, user_messages: Iterable[str]) -> dict:
    task = _task(state, task_id)
    messages = _merge_messages(state.p2.goal.user_messages, user_messages)
    targets = list(task.target_cells) if task else []
    gaps = [
        key
        for key, cell in sorted(state.coverage_map.cells.items())
        if cell.status != CellStatus.FILLED or cell.has_conflict or cell.conflict_status != "clear"
    ]
    if targets:
        gaps = [key for key in gaps if key in targets or key.split("/", 1)[-1] in targets]
    return {
        "original_goal": state.intent,
        "user_messages": messages,
        "hard_constraints": list(state.p2.goal.hard_constraints),
        "required_attributes": list(state.required_attributes),
        "current_task": task.task_prompt or task.question if task else state.intent,
        "target_cells": targets,
        "gaps": gaps,
        "source_scope": source_scope_fingerprint(state),
    }


def _goal_fingerprint(view: dict) -> str:
    # Progress changes the live gap projection, not the user's objective.
    # Summary lineage uses the stable goal fingerprint; publication checks
    # additionally compare the complete state fingerprint below.
    return _digest({key: value for key, value in view.items() if key != "gaps"})


def _render_card(card: ExperienceCard) -> str:
    feedback = card.feedback
    # Display bounded exact telemetry values; omitted routes remain in durable
    # storage. They are never replaced by an invented general strategy.
    lines = [
        f"Experience {card.id} (attempt-level association; not a causal guarantee)",
        "Task: " + card.question,
        "Queries observed: " + json.dumps(card.queries[:3], ensure_ascii=False),
        "Routes observed: " + json.dumps(card.routes[:2], ensure_ascii=False, sort_keys=True),
        "Sources: " + json.dumps(card.sources[:3], ensure_ascii=False),
        "Grounded new evidence credited once: " + json.dumps(feedback.credited_evidence_ids),
        "Newly filled cells with exclusive current support: "
        + json.dumps(feedback.newly_filled_cells),
        f"Repeated evidence: {feedback.repeated_evidence_count}; "
        f"duration_s: {feedback.duration_s}; "
        f"query_count: {feedback.query_count}; token_count: {feedback.token_count}",
    ]
    return "\n".join(lines)


def build_context_packet(
    state: SearchState,
    task_id: str = "__answer__",
    *,
    user_messages: Iterable[str] = (),
    max_experiences: int = 3,
    max_chars: int = 2400,
    scope_key: str | None = None,
) -> ContextPacket:
    """Create a read-only protected goal block and a bounded experience view.

    max_chars caps the experience subsection, never silently truncates user
    goals/constraints. Relevance is deterministic lexical/target overlap, not
    an assertion of semantic transfer. Current task outranks the broad goal.
    """
    user_messages = _message_texts(user_messages)
    task = _task(state, task_id)
    scope = scope_key or (task.p1_scope_key if task else "answer")
    view = _goal_view(state, task_id, user_messages)
    goal_lines = [
        "[P2 GOAL — authoritative user text; keep separate from history summary]",
        "Original objective: " + view["original_goal"],
        "User updates, in order (later updates may revise earlier scope): "
        + json.dumps(view["user_messages"], ensure_ascii=False),
        "Explicit hard constraints: " + json.dumps(view["hard_constraints"], ensure_ascii=False),
        "Required attributes: " + json.dumps(view["required_attributes"], ensure_ascii=False),
        "Current task: " + view["current_task"],
        "Current gaps: " + json.dumps(view["gaps"][:24], ensure_ascii=False),
    ]
    if len(view["gaps"]) > 24:
        goal_lines.append(
            f"{len(view['gaps']) - 24} additional gaps remain in SOCM; "
            "query them before completion."
        )
    terms = _terms(
        (task.question if task else view["current_task"])
        + " "
        + " ".join(view["gaps"])
        + " "
        + " ".join(view["target_cells"])
    )
    candidates = []
    for card in state.p2.experiences.values():
        if card.status != "active" or card.scope_key != scope or _invalid_reasons(state, card):
            continue
        matching = terms & _terms(
            card.question + " " + " ".join(card.target_cells) + " " + " ".join(card.queries)
        )
        if not matching:
            continue
        novelty = len(card.feedback.credited_evidence_ids)
        filled = len(card.feedback.newly_filled_cells or ())
        score = len(matching) * 10 + min(novelty, 4) + min(filled, 3) * 2
        candidates.append((score, card))
    selected = []
    rendered = []
    used = 0
    domains: set[str] = set()
    strategies: set[str] = set()
    header = (
        "[P2 COLLECTION EXPERIENCE — use as a lead only when it fits the current gap; "
        "verify fresh evidence, never copy a prior conclusion]\n"
    )
    while candidates and len(selected) < max(0, max_experiences):

        def rank(item):
            score, card = item
            family = {urlsplit(source).netloc or source.split("/", 1)[0] for source in card.sources}
            strategy = _digest([card.queries, [route.get("signature") for route in card.routes]])
            return score - 4 * bool(family & domains) - 8 * (strategy in strategies), card.id

        winner = max(candidates, key=rank)
        candidates.remove(winner)
        card = winner[1]
        text = _render_card(card)
        separator_cost = len("\n\n") if rendered else len(header)
        if used + len(text) + separator_cost > max(0, max_chars):
            continue
        selected.append(card)
        rendered.append(text)
        used += len(text) + separator_cost
        domains.update(
            urlsplit(source).netloc or source.split("/", 1)[0] for source in card.sources
        )
        strategies.add(_digest([card.queries, [route.get("signature") for route in card.routes]]))
    evidence = {
        eid: version for card in selected for eid, version in card.evidence_versions.items()
    }
    artifacts = {
        aid: version for card in selected for aid, version in card.artifact_versions.items()
    }
    content = "\n".join(goal_lines)
    if rendered:
        content += "\n\n" + header + "\n\n".join(rendered)
    return ContextPacket(
        content=content,
        task_id=task_id,
        scope_key=scope,
        goal_fingerprint=_goal_fingerprint(view),
        state_fingerprint=_digest(view),
        source_scope_fingerprint=source_scope_fingerprint(state),
        goal_version=state.p2.goal.version,
        user_messages=user_messages,
        experience_ids=[card.id for card in selected],
        read_evidence=evidence,
        read_artifacts=artifacts,
        experience_fingerprints={card.id: experience_fingerprint(card) for card in selected},
        experience_chars=used,
    )


def verify_packet(state: SearchState, packet: ContextPacket) -> None:
    view = _goal_view(state, packet.task_id, packet.user_messages)
    if _goal_fingerprint(view) != packet.goal_fingerprint:
        raise StaleExecutionError("P2 goal changed during summarization")
    if packet.state_fingerprint and _digest(view) != packet.state_fingerprint:
        raise StaleExecutionError("P2 current gaps changed during summarization")
    if source_scope_fingerprint(state) != packet.source_scope_fingerprint:
        raise StaleExecutionError("P2 allowed source scope changed")
    for cid in packet.experience_ids:
        card = state.p2.experiences.get(cid)
        if (
            card is None
            or card.status != "active"
            or card.scope_key != packet.scope_key
            or _invalid_reasons(state, card)
            or experience_fingerprint(card) != packet.experience_fingerprints.get(cid)
        ):
            raise StaleExecutionError("P2 experience requires revalidation: " + cid)
    for eid, version in packet.read_evidence.items():
        if (
            not evidence_usable(state, eid, packet.scope_key)
            or evidence_version(state, eid) != version
        ):
            raise StaleExecutionError("P2 experience evidence changed: " + eid)
    for aid, version in packet.read_artifacts.items():
        if not artifact_usable(state, aid) or state.p1.artifacts[aid].version != version:
            raise StaleExecutionError("P2 experience parent changed: " + aid)
