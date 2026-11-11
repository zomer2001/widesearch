"""Writer 的结构化提纲、章节编辑工具及配套 SOCM 读取能力。"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import tool

from searchos.socm.coverage import answer_evidence
from searchos.tools.search_state import _current_agent_var, _ws

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent-prefix guards
# ---------------------------------------------------------------------------

def _require_agent_prefix(op: str, *allowed: str) -> None:
    agent = _current_agent_var.get()
    if not (agent and any(agent.startswith(a) for a in allowed)):
        raise RuntimeError(
            f"{op} called from agent {agent!r}; this tool is restricted "
            f"to {allowed}. Not in your toolset."
        )


def _require_writer(op: str) -> None:
    _require_agent_prefix(op, "writer_agent")


def _mirror_outline_to_output(ws: Any, rendered: str) -> None:
    """Write the live outline render to ``output/draft.md`` after every
    writer mutation. Lets users ``tail -f`` the file to watch the
    structured draft assemble. Best-effort — failures never crash a tool."""
    try:
        ws.write_output("draft.md", rendered)
    except Exception:  # noqa: BLE001
        logger.debug("draft mirror write failed", exc_info=True)


def _ensure_ev_ids(state: Any, ids: list[str]) -> tuple[list[str], list[str]]:
    from searchos.harness.p1_runtime import current_scope
    known = {n.id for n in answer_evidence(state, use_scope=current_scope(state))}
    good = [i for i in ids if i in known]
    bad = [i for i in ids if i not in known]
    return good, bad


def _publish_section(state: Any, section: Any) -> None:
    """Attach runtime inputs in the same transaction as the section body."""
    from searchos.harness.p1_runtime import current_run_id

    run_id = current_run_id.get()
    if not run_id:
        return
    from searchos.socm.revisions import publish_artifact

    artifact = publish_artifact(
        state, run_id=run_id, artifact_id=f"section:{section.id}", kind="section",
        content=section.content, support_sets=[list(section.cited_evidence_ids)],
    )
    section.p1_status = "active"
    section.p1_epoch = artifact.epoch


def _section_read_view(state: Any, section: Any) -> dict[str, Any]:
    from searchos.tools.socm_read import _p1_artifact_view

    row = section.model_dump()
    tracking = _p1_artifact_view(state, f"section:{section.id}")
    if (tracking.get("dependency_status") == "active"
            and state.p1.artifacts[f"section:{section.id}"].content != section.content):
        tracking["dependency_status"] = "stale"
        tracking["dependency_note"] = (
            "Section content differs from its versioned publication; "
            "rebuild it from current evidence."
        )
    row.update(tracking)
    if tracking.get("dependency_status") in {"stale", "untracked"}:
        row["content"] = ""
        row["notes"] = []
    return row


# ---------------------------------------------------------------------------
# Writer: outline + section tools (plan §4.2)
# ---------------------------------------------------------------------------

@tool
async def read_outline(section_id: str = "", toc_only: bool = True) -> str:
    """Read the outline. Default is the table-of-contents view (no content) — pass ``section_id`` to get one section's full body. ``toc_only=False`` without ``section_id`` dumps every section's content; avoid on large drafts.

    Args:
        section_id (str): return this section's full ``{id, title, content, cited_evidence_ids, notes, order}``.
        toc_only (bool): TOC view ``{sections:[{id, title, order, chars, n_citations, n_notes}], count, total_chars}``.
    """
    ws = _ws()
    state = ws.load_state()
    from searchos.tools.socm_read import _record_artifact_reads
    if section_id:
        sec = state.outline.find(section_id)
        if sec is None:
            return json.dumps({"error": f"section {section_id!r} not found"})
        row = _section_read_view(state, sec)
        if row.get("content"):
            _record_artifact_reads(ws, state, [f"section:{sec.id}"])
        return json.dumps(row, ensure_ascii=False)
    sections = sorted(state.outline.sections, key=lambda s: (s.order, s.id))
    if toc_only:
        from searchos.tools.socm_read import _p1_artifact_view
        return json.dumps(
            {
                "sections": [
                    {
                        "id": s.id,
                        "title": s.title,
                        "order": s.order,
                        "chars": len(s.content),
                        "n_citations": len(s.cited_evidence_ids),
                        "n_notes": len(s.notes),
                        **_p1_artifact_view(state, f"section:{s.id}"),
                    }
                    for s in sections
                ],
                "count": len(sections),
                "total_chars": sum(len(s.content) for s in sections),
            },
            ensure_ascii=False,
        )
    rows = [_section_read_view(state, section) for section in sections]
    _record_artifact_reads(
        ws, state, [f"section:{row['id']}" for row in rows if row.get("content")],
    )
    return json.dumps(
        {"sections": rows, "count": len(sections)},
        ensure_ascii=False,
    )


@tool
async def update_outline(ops_json: str) -> str:
    """Apply a batch of outline edits.

    Args:
        ops_json (str): JSON array of ops, each one of:
            ``{"type": "add", "id": "...", "title": "...", "order": 1.0}``
            ``{"type": "remove", "id": "..."}``
            ``{"type": "reorder", "id": "...", "order": 2.5}``
    """
    _require_writer("update_outline")
    try:
        ops = json.loads(ops_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"invalid JSON: {e}"})
    if not isinstance(ops, list):
        return json.dumps({"error": "ops_json must be a JSON array"})

    from searchos.socm import OutlineSection

    errors: list[str] = []
    applied: list[dict[str, Any]] = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or "type" not in op:
            errors.append(f"op[{i}]: expected object with 'type'")
            continue
        t = str(op.get("type", "")).lower()
        if t not in ("add", "remove", "reorder"):
            errors.append(f"op[{i}]: type must be add|remove|reorder")
            continue
        if "id" not in op or not str(op.get("id", "")).strip():
            errors.append(f"op[{i}]: id required")
    if errors:
        return json.dumps({"ok": False, "errors": errors})

    ws = _ws()

    def _apply(s: Any) -> Any:
        from searchos.harness.p1_runtime import assert_current_state
        assert_current_state(s)
        for op in ops:
            t = op["type"].lower()
            sid = str(op["id"]).strip()
            if t == "add":
                sec = OutlineSection(
                    id=sid,
                    title=str(op.get("title", "")),
                    order=float(op.get("order", 0.0)),
                )
                s.outline.upsert(sec)
                applied.append({"type": "add", "id": sid})
            elif t == "remove":
                ok = s.outline.remove(sid)
                applied.append({"type": "remove", "id": sid, **({"miss": True} if not ok else {})})
            elif t == "reorder":
                target = s.outline.find(sid)
                if target is not None:
                    target.order = float(op.get("order", target.order))
                    applied.append({"type": "reorder", "id": sid, "order": target.order})
                else:
                    applied.append({"type": "reorder", "id": sid, "miss": True})
        _mirror_outline_to_output(ws, s.outline.rendered())
        return s

    ws.atomic_update_state(_apply)
    return json.dumps({"ok": True, "applied": applied}, ensure_ascii=False)


@tool
async def write_section(
    section_id: str,
    content: str,
    cited_evidence_ids: str,
) -> str:
    """Write (or overwrite) a section body. ``cited_evidence_ids`` must reference active evidence within the current answer scope.

    Args:
        section_id (str): section id.
        content (str): section body.
        cited_evidence_ids (str): comma-separated evidence ids.
    """
    _require_writer("write_section")
    if not section_id.strip():
        return json.dumps({"error": "section_id required"})
    if not content.strip():
        return json.dumps({"error": "content required"})
    ids = [x.strip() for x in cited_evidence_ids.split(",") if x.strip()]
    if not ids:
        return json.dumps({"error": "cited_evidence_ids required — no refs means no write (§4.2)"})

    ws = _ws()
    state = ws.load_state()
    good, bad = _ensure_ev_ids(state, ids)
    if bad:
        return json.dumps({"error": f"unknown or ineligible evidence ids: {bad}"})

    from searchos.harness.p1_runtime import record_snapshot
    record_snapshot(ws, state, evidence_ids=good)

    from searchos.socm import OutlineSection

    commit_errors: list[str] = []

    def _apply(s: Any) -> Any:
        from searchos.harness.p1_runtime import assert_current_state
        assert_current_state(s)
        _, invalid = _ensure_ev_ids(s, good)
        if invalid:
            commit_errors.extend(invalid)
            return s
        sec = s.outline.find(section_id)
        if sec is None:
            sec = OutlineSection(id=section_id, content=content, cited_evidence_ids=good)
            sec.order = float(len(s.outline.sections) + 1)
            s.outline.sections.append(sec)
        else:
            sec.content = content
            sec.cited_evidence_ids = good
        _publish_section(s, sec)
        _mirror_outline_to_output(ws, s.outline.rendered())
        return s

    ws.atomic_update_state(_apply)
    if commit_errors:
        return json.dumps({"error": f"unknown or ineligible evidence ids: {commit_errors}"})
    return json.dumps(
        {"ok": True, "id": section_id, "cited": good, "chars": len(content)},
        ensure_ascii=False,
    )


@tool
async def edit_section(
    section_id: str,
    old_string: str,
    new_string: str,
    cited_evidence_ids: str = "",
) -> str:
    """Replace ``old_string`` with ``new_string`` inside a section (must match exactly once).

    Args:
        section_id (str): section id.
        old_string (str): text to replace; must be unique within the section.
        new_string (str): replacement text.
        cited_evidence_ids (str): comma-separated active, in-scope ids — if provided, REPLACES the citation list; required when the section has no prior citations.
    """
    _require_writer("edit_section")
    ws = _ws()
    state = ws.load_state()
    sec = state.outline.find(section_id)
    if sec is None:
        return json.dumps({"error": f"section {section_id!r} not found"})
    from searchos.harness.p1_runtime import current_run_id, record_snapshot
    if current_run_id.get():
        artifact = state.p1.artifacts.get(f"section:{section_id}")
        if artifact is None or artifact.status != "active" or artifact.content != sec.content:
            return json.dumps({"error": (
                "Section is stale or has no runtime provenance; "
                "rebuild with write_section from current evidence."
            )})
    occurrences = sec.content.count(old_string)
    if occurrences == 0:
        return json.dumps({"error": "old_string not found"})
    if occurrences > 1:
        return json.dumps({"error": f"old_string matches {occurrences}x — add context to disambiguate"})

    ids: list[str] = []
    if cited_evidence_ids.strip():
        ids = [x.strip() for x in cited_evidence_ids.split(",") if x.strip()]
        good, bad = _ensure_ev_ids(state, ids)
        if bad:
            return json.dumps({"error": f"unknown or ineligible evidence ids: {bad}"})
        ids = good
    elif not sec.cited_evidence_ids:
        return json.dumps({"error": "section has no prior citations; provide cited_evidence_ids (§4.2)"})
    else:
        ids, bad = _ensure_ev_ids(state, sec.cited_evidence_ids)
        if bad:
            return json.dumps({"error": f"unknown or ineligible evidence ids: {bad}"})

    record_snapshot(ws, state, evidence_ids=ids, artifact_ids=[f"section:{section_id}"]
                    if current_run_id.get() else [])

    commit_errors: list[str] = []

    def _apply(s: Any) -> Any:
        from searchos.harness.p1_runtime import assert_current_state
        assert_current_state(s)
        _, invalid = _ensure_ev_ids(s, ids)
        if invalid:
            commit_errors.extend(invalid)
            return s
        target = s.outline.find(section_id)
        if target is None:
            return s
        if target.content != sec.content:
            commit_errors.append("section changed while editing; read the current section again")
            return s
        target.content = target.content.replace(old_string, new_string, 1)
        if ids:
            target.cited_evidence_ids = ids
        _publish_section(s, target)
        _mirror_outline_to_output(ws, s.outline.rendered())
        return s

    ws.atomic_update_state(_apply)
    if commit_errors:
        return json.dumps({"error": f"unknown or ineligible evidence ids: {commit_errors}"})
    return json.dumps({"ok": True, "id": section_id, "chars": len(sec.content)}, ensure_ascii=False)


@tool
async def annotate_section(section_id: str, note: str) -> str:
    """Add a TODO / 存疑 note to a section (renders as ``> TODO: ...`` in the draft).

    Args:
        section_id (str): section id.
        note (str): note text (truncated to 240 chars).
    """
    _require_writer("annotate_section")
    if not note.strip():
        return json.dumps({"error": "note required"})
    ws = _ws()

    def _apply(s: Any) -> Any:
        from searchos.harness.p1_runtime import assert_current_state
        assert_current_state(s)
        sec = s.outline.find(section_id)
        if sec is None:
            return s
        sec.notes.append(note.strip()[:240])
        _mirror_outline_to_output(ws, s.outline.rendered())
        return s

    ws.atomic_update_state(_apply)
    state = ws.load_state()
    sec = state.outline.find(section_id)
    if sec is None:
        return json.dumps({"error": f"section {section_id!r} not found"})
    return json.dumps({"ok": True, "id": section_id, "notes": sec.notes}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def get_writer_tools() -> list:
    """Plan §4.2 writer toolset — structured outline + reads."""
    from searchos.tools.skill_catalog import list_skills, load_skill
    from searchos.tools.socm_read import get_socm_read_tools
    return [
        # Outline + section tools
        read_outline,
        update_outline,
        write_section,
        edit_section,
        annotate_section,
        # SOCM reads + skill discovery
        *get_socm_read_tools(),
        list_skills,
        load_skill,
    ]
