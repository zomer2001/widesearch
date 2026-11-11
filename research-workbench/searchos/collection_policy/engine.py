"""The four policy operations. All state is explicitly supplied by the caller."""

import hashlib
import re

from .algorithms import canonical_url, relevance, select, similarity, tokens
from .models import Candidate, CollectionState, Feedback, Goal


def fingerprint(text: str) -> str:
    # Keep numbers, units, punctuation and negation; only normalize whitespace/case.
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().casefold().encode()).hexdigest()


def observe(
    state: CollectionState,
    *,
    task_id: str,
    source: str,
    content: str,
    facts: list[dict],
    goal_status: str = "unknown",
    title: str = "",
    links: list[str] | None = None,
    scope: str = "",
) -> None:
    source = canonical_url(source)
    digest = fingerprint(content)
    batch_id = fingerprint(task_id + "\n" + source + "\n" + digest + "\n" + scope)
    if batch_id in state.batches:
        return
    feedback = Feedback(task_id=task_id, source=source)
    duplicate_body = any(c.content_hash == digest for c in state.candidates.values())
    for fact in facts:
        # The adapter only supplies accepted, source-anchored evidence.
        claim = fact["claim"]
        key = fingerprint(claim)
        seen = state.facts.get(key, [])
        novelty = fact.get("novelty", "new")
        if source in seen or novelty == "repeat":
            feedback.repeated += 1
        elif seen or novelty == "corroboration" or duplicate_body:
            feedback.corroboration += 1
        else:
            feedback.new += 1
        if source not in seen:
            state.facts[key] = [*seen, source]
        feedback.evidence_ids.append(fact["id"])
    state.batches[batch_id] = feedback
    candidate = state.candidates.get(source, Candidate(url=source))
    candidate.title = title or candidate.title
    candidate.text = content[:6000]
    candidate.read = True
    candidate.content_hash = digest
    candidate.links = list(dict.fromkeys([*candidate.links, *(links or [])]))[:100]
    state.candidates[source] = candidate
    goal = state.goals.get(task_id)
    # Model assessment is advisory; table coverage is recomputed by the adapter.
    if goal and facts and goal.basis != "table":
        if goal_status == "sufficient":
            goal.gap, goal.basis = 0.0, "reader_assessment"
        elif goal_status == "partial":
            goal.gap, goal.basis = 0.5, "reader_assessment"


def recent_gain(state: CollectionState, task_id: str) -> float:
    records = [item for item in state.batches.values() if item.task_id == task_id][-5:]
    return sum(item.new > 0 for item in records) / len(records) if records else 0.5


def goal_weight(state: CollectionState, task_id: str) -> float:
    goal = state.goals.get(task_id, Goal())
    gap = goal.gap if goal.gap is not None else 1.0
    # Soft down-ranking leaves assessed goals available for corroboration/recovery.
    return (0.2 + 0.8 * gap) * (1 + recent_gain(state, task_id))


def rank_pages(
    state: CollectionState, candidates: list[Candidate], *, query: str, task_id: str = ""
) -> list[str]:
    for candidate in candidates:
        old = state.candidates.get(candidate.url)
        if old is None or not old.read:
            state.candidates[candidate.url] = candidate
    wanted = {item.url for item in candidates}
    # Include every returned candidate, then recent context; no candidate is dropped.
    current = [state.candidates[url] for url in dict.fromkeys(c.url for c in candidates)]
    context = [c for url, c in state.candidates.items() if url not in wanted]
    graph_items = (
        current + context[-max(0, 300 - len(current)) :] if len(current) < 300 else current
    )
    goal = state.goals.get(task_id)
    intent = " ".join([query, goal.text, *goal.terms]) if goal else query
    weights = dict.fromkeys(wanted, goal_weight(state, task_id))
    ordered, decisions, groups = select(graph_items, intent, weights, wanted)
    for decision in decisions:
        decision["task_id"] = task_id
    state.communities = groups
    state.decisions = (state.decisions + decisions[:20])[-60:]
    return ordered


def rank_tasks(state: CollectionState, tasks: list[dict], *, intent: str, now: float) -> list[str]:
    scores = relevance(intent, [t["text"] for t in tasks])
    ranked = []
    for task, rel in zip(tasks, scores):
        task_id = task["id"]
        own_records = any(b.task_id == task_id for b in state.batches.values())
        gain = recent_gain(state, task_id)
        if not own_records:
            # Transfer route experience softly to a related follow-up, never hard-block it.
            words = set(tokens(task["text"]))
            neighbors = [
                (similarity(words, set(tokens(g.text))), key)
                for key, g in state.goals.items()
                if key != task_id and any(b.task_id == key for b in state.batches.values())
            ]
            if neighbors:
                strength, neighbor = max(neighbors)
                gain = (1 - strength) * gain + strength * recent_gain(state, neighbor)
        goal = state.goals.get(task_id, Goal())
        gap = goal.gap if goal.gap is not None else 1.0
        age = max(0.0, now - task["created_at"])
        score = (task["priority"] + rel) / 2 * (0.2 + 0.8 * gap) * (1 + gain) + 0.1 * age / (
            60 + age
        )
        ranked.append((score, task_id))
    ranked.sort(key=lambda item: -item[0])
    state.decisions = (
        state.decisions
        + [{"id": task_id, "kind": "task", "score": round(score, 4)} for score, task_id in ranked]
    )[-60:]
    return [task_id for _, task_id in ranked]


def brief(state: CollectionState) -> str:
    lines = [
        "[自适应采集：本轮目标与资料反馈]",
        "依据缺口和近期增量安排后续任务；有增量的线索继续深入，未解决但低收益的方向换查询或来源。",
        "任务执行完毕不等于目标已覆盖；资料分组是词汇聚类，不代表事实相同。",
        "enqueue_tasks 可用 intent_terms 提供当前任务的关键术语及中英文别名。",
    ]
    for task_id, goal in list(state.goals.items())[-20:]:
        gap = "未评估" if goal.gap is None else f"{goal.gap:.0%}"
        records = [b for b in state.batches.values() if b.task_id == task_id][-5:]
        lines.append(
            f"- {task_id}: {goal.text[:140]} | 缺口 {gap} ({goal.basis}) | "
            f"最近 {len(records)} 批：新增 {sum(b.new for b in records)}、"
            f"佐证 {sum(b.corroboration for b in records)}、重复 {sum(b.repeated for b in records)}"
        )
    for group in state.communities[:8]:
        lines.append(
            f"- 资料组 {' / '.join(group['terms'])}: {group['size']} 篇，已读 {group['read']}"
        )
    return "\n".join(lines)
