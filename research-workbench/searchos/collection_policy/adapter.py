"""Thin SearchOS integration. Disabled modules never reorder or write state."""

import logging
import re
import time
from urllib.parse import urljoin

from . import engine
from .algorithms import canonical_url
from .models import Candidate, Goal

logger = logging.getLogger(__name__)
MODULE_ID = "adaptive_collection"


def enabled(state) -> bool:
    return MODULE_ID in getattr(state, "module_config", {}).get("enabled", [])


def sync_goals(state) -> None:
    for task in state.frontier.questions:
        if task.kind == "write":
            continue
        goal = state.collection_policy.goals.setdefault(task.id, Goal())
        goal.text = task.task_prompt or task.question
        goal.terms = list(task.intent_terms)
        cells = [
            state.coverage_map.cells.get(
                key
                if "/" in key
                else f"{task.table_id or state.coverage_map.primary_table_id}/{key}"
            )
            for key in task.target_cells
        ]
        if cells:
            goal.gap = sum(
                c is None or c.status.value != "filled" or c.has_conflict for c in cells
            ) / len(cells)
            goal.basis = "table"


def _context():
    from searchos.tools.search_state import get_current_task_id, get_workspace

    return get_workspace(), get_current_task_id()


def _failed(workspace, operation: str, exc: Exception) -> None:
    logger.warning("Adaptive collection %s failed; original flow retained: %s", operation, exc)
    if workspace is not None:

        def update(state):
            state.collection_policy.last_error = f"{operation}: {type(exc).__name__}"
            return state

        workspace.atomic_update_state(update)


def rank_search_results(query: str, results: list) -> list:
    workspace, task_id = _context()
    if workspace is None or not enabled(workspace.load_state()) or not results:
        return results
    ordered = []

    def update(state):
        sync_goals(state)
        ordered.extend(
            engine.rank_pages(
                state.collection_policy,
                [
                    Candidate(
                        url=canonical_url(r.url), title=r.title, text=r.snippet or r.content[:6000]
                    )
                    for r in results
                    if r.url
                ],
                query=query,
                task_id=task_id,
            )
        )
        return state

    try:
        workspace.atomic_update_state(update)
        positions = {url: i for i, url in enumerate(ordered)}
        return sorted(results, key=lambda r: positions.get(canonical_url(r.url), len(positions)))
    except Exception as exc:
        _failed(workspace, "page_rank", exc)
        return results


def rank_ready_tasks(workspace, tasks: list) -> list:
    if workspace is None or not enabled(workspace.load_state()) or not tasks:
        return tasks
    ordered = []

    def update(state):
        sync_goals(state)
        ordered.extend(
            engine.rank_tasks(
                state.collection_policy,
                [
                    {
                        "id": t.id,
                        "text": " ".join([t.task_prompt or t.question, *t.intent_terms]),
                        "priority": t.priority,
                        "created_at": t.created_at,
                    }
                    for t in tasks
                ],
                intent=state.intent,
                now=time.time(),
            )
        )
        return state

    try:
        workspace.atomic_update_state(update)
        positions = {task_id: i for i, task_id in enumerate(ordered)}
        return sorted(tasks, key=lambda t: positions.get(t.id, len(positions)))
    except Exception as exc:
        _failed(workspace, "task_rank", exc)
        return tasks


def record_page(workspace, url: str, page) -> None:
    """Capture actual hyperlinks, without marking an unread/fetched page as evidence."""
    if not callable(getattr(workspace, "load_state", None)) or not enabled(workspace.load_state()):
        return
    from searchos.tools.simple_browser.state import is_url_allowed

    url = canonical_url(url)
    links = [
        (label, canonical_url(urljoin(url, href)))
        for label, href in re.findall(r"\[([^\]\n]+)\]\(([^\s)]+)\)", page.text or "")
    ]
    links += [("", canonical_url(link)) for link in page.urls.values()]
    links = [
        (label, link)
        for label, link in links
        if link.startswith(("https://", "http://")) and is_url_allowed(link)
    ][:100]

    def update(state):
        policy = state.collection_policy
        candidate = policy.candidates.setdefault(url, Candidate(url=url))
        candidate.title = page.title or candidate.title
        candidate.text = (page.text or "")[:6000]
        candidate.links = list(dict.fromkeys(link for _, link in links))
        for label, link in links:
            policy.candidates.setdefault(link, Candidate(url=link, title=label))
        return state

    try:
        workspace.atomic_update_state(update)
    except Exception as exc:
        _failed(workspace, "page_links", exc)


def observe_in_state(
    state,
    item: dict,
    evidence_ids: list[str],
    *,
    assessments: dict | None = None,
    goal_status: str = "unknown",
) -> None:
    """Called in the SAME atomic write as evidence commit; retries cannot double-credit."""
    if not enabled(state):
        return
    sync_goals(state)
    source = item.get("source_url", "")
    if not source.startswith(("http://", "https://", "local://", "skill://")):
        return
    task_id = item.get("task_id", "")
    facts = []
    assessments = assessments or {}
    from searchos.socm.coverage import evidence_source_allowed

    for evidence_id in dict.fromkeys(evidence_ids):
        node = state.evidence_graph.get_node(evidence_id)
        if (
            node is None
            or node.source != source
            or node.status.value != "active"
            or not node.source_excerpt
            or not evidence_source_allowed(state, node)
        ):
            continue
        # Context or unsupported material must not create a positive reward.
        binding = [
            b
            for b in state.evidence_graph.task_bindings
            if b.task_id == task_id and b.evidence_id == evidence_id
        ]
        if task_id and not any(b.relation in {"supports", "contradicts"} for b in binding):
            continue
        from searchos.util.span_match import find_span

        content = item.get("content", "")
        span, _ = find_span(
            content, node.source_excerpt, browser_lines=content.startswith("[Now viewing]")
        )
        if span is None:
            continue
        claim = (
            " | ".join([node.entity, node.attribute, node.value]) if node.value else node.finding
        )
        facts.append(
            {"id": node.id, "claim": claim, "novelty": assessments.get(evidence_id, "new")}
        )
    engine.observe(
        state.collection_policy,
        task_id=task_id,
        source=source,
        content=item.get("content", ""),
        facts=facts,
        goal_status=goal_status,
        scope=item.get("collection_scope", ""),
    )
    # Refresh table gaps after the new evidence is committed.
    sync_goals(state)


def assessment_context(state, task_id: str) -> str:
    if not enabled(state):
        return ""
    ids = {b.evidence_id for b in state.evidence_graph.task_bindings if b.task_id == task_id}
    known = [
        n.finding for n in state.evidence_graph.nodes if n.id in ids and n.status.value == "active"
    ]
    return (
        "\n自适应采集：与下列已有事实比较，每项额外返回 novelty（new/corroboration/repeat）；"
        "不同来源支持相同内容属于 corroboration。"
        "每项还返回 goal_status（unknown/partial/sufficient），"
        "只有已有事实加本次核验通过的事实足以回答任务全部要求时使用 sufficient；"
        "开放枚举无法证明完整时使用 partial。此状态是有依据的估计，不表示全网搜全。\n已有事实：\n"
        + "\n".join(known[-30:])[:6000]
    )


def refresh_after_read(workspace, task_id: str) -> None:
    """Re-rank observed links after a settled batch, outside the evidence transaction."""
    if not enabled(workspace.load_state()):
        return

    def update(state):
        sync_goals(state)
        policy = state.collection_policy
        goal = policy.goals.get(task_id)
        unread = [c for c in policy.candidates.values() if not c.read][-200:]
        if unread:
            engine.rank_pages(
                policy, unread, query=goal.text if goal else state.intent, task_id=task_id
            )
        return state

    try:
        workspace.atomic_update_state(update)
    except Exception as exc:
        _failed(workspace, "reading_rank", exc)


def brief(state, task_id: str = "") -> str:
    if not enabled(state):
        return ""
    sync_goals(state)
    text = engine.brief(state.collection_policy)
    suggestions = {}
    policy = state.collection_policy
    for decision in policy.decisions:
        candidate = policy.candidates.get(decision["id"])
        if (
            decision["kind"] == "page"
            and decision.get("task_id") == task_id
            and candidate is not None
            and not candidate.read
        ):
            suggestions[candidate.url] = (decision["score"], candidate)
    best = sorted(suggestions.values(), key=lambda item: -item[0])[:3]
    if best:
        text += "\n可继续阅读的候选：\n" + "\n".join(
            f"- {candidate.title[:80]} {candidate.url}" for _, candidate in best
        )
    return text
