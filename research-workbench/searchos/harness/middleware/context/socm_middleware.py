"""每次模型请求注入当前 SOCM 投影，不依赖临时消息被持久化。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any, Literal

from langchain.agents.middleware.types import AgentMiddleware

from searchos.harness.middleware.context.utils.projection import TaskProjection
from searchos.harness.middleware.models import TaskContext
from searchos.harness.middleware.sensor.core.models import ControlState
from searchos.harness.middleware.utils.messages import inject_context
from searchos.socm.views.render import render_compact_summary, render_orchestrator_view
from searchos.socm.views.snapshot import SearchStateSnapshot


class SOCMContextMiddleware(AgentMiddleware):
    name = "SOCMContextMiddleware"

    def __init__(
        self,
        workspace: Any,
        *,
        task: TaskContext,
        view: Literal["task", "overview", "writing"] = "task",
        control: ControlState | None = None,
        guidance: str | None = None,
        heartbeat_turns: int = 0,
        agent_graphs: Mapping[str, Any] | None = None,
    ) -> None:
        if view not in {"task", "overview", "writing"}:
            raise ValueError(f"Unknown SOCM view: {view}")
        self.workspace = workspace
        self.task = task
        self.view = view
        self.control = control
        if view == "overview" and guidance is None:
            load_state = getattr(workspace, "load_state", None)
            scope = getattr(load_state(), "source_scope", None) if callable(load_state) else None
            if (scope or {}).get("mode") == "local":
                guidance = (
                    "推进已有 pending/running 任务，避免重复派发。只针对尚未覆盖的具体问题"
                    "分配本地阅读；用 read_evidence 核对原文与引用后回答。"
                    "空表和目录未匹配不证明资料没有答案。"
                )
        self.guidance = (
            guidance
            if guidance is not None
            else {
                "task": (
                    "Continue your assigned task; adjust searches to its remaining targets "
                    "and avoid unrelated work."
                ),
                "overview": (
                    "Adjust dispatch using current gaps and in-flight tasks; "
                    "avoid duplicate assignments. Before finishing, resolve derivable gaps with "
                    "python and edit_entities. Read input sources with read_evidence(cell=...) "
                    "and include their original source and source_excerpt in each edit object."
                ),
                "writing": "Ground writing in current evidence and report unresolved limitations.",
            }[view]
        )
        # Compatibility parameter: snapshots must remain visible on every request.
        self.heartbeat_turns = heartbeat_turns
        self._projection = TaskProjection(
            task.agent_id,
            task_text=task.task_text,
            target_table=task.target_table,
            agent_graphs=agent_graphs,
        )
        self._last_text = ""

    def bind_control(self, controls: tuple[ControlState, ...]) -> None:
        if self.control is not None or not controls:
            return
        if len(controls) != 1:
            raise ValueError("SOCMContextMiddleware requires explicit control when multiple exist")
        self.control = controls[0]

    def render(self) -> str:
        if self.workspace is None:
            return ""
        state = self.workspace.load_state()
        original_state = state
        from searchos.harness.p1_runtime import current_run_id, current_scope
        if current_run_id.get():
            scope = current_scope(state)
            excluded = set(state.p1.retired_uses.get(scope, []))
            affected = {key for key, cell in state.coverage_map.cells.items()
                        if excluded.intersection(cell.supporting_evidence_ids)}
            if affected:
                # A scoped retirement changes only this request's projection.
                # The shared answer view and the source assertions stay intact.
                from searchos.socm.coverage import refresh_cells
                state = state.model_copy(deep=True)
                state.p1.retired_uses["answer"] = sorted(
                    excluded | set(state.p1.retired_uses.get("answer", [])))
                refresh_cells(state, affected)
        if self.view == "task":
            text = self._projection(state)
        else:
            snapshot = SearchStateSnapshot.from_state(state)
            text = (
                render_orchestrator_view(snapshot)
                if self.view == "overview"
                else render_compact_summary(snapshot)
            )
        self._record_projected_inputs(state, original_state)
        from searchos.collection_policy.adapter import brief
        from searchos.tools.search_state import get_current_task_id
        collection_brief = brief(state, get_current_task_id())
        if collection_brief:
            text += "\n\n" + collection_brief
        # 每轮读取运行环境日期；无表时也注入，覆盖首次任务派发。
        temporal = (
            "[Runtime time]\n"
            f"Current date (runtime local date): {date.today().isoformat()}\n"
            "Resolve relative dates against this date unless the user explicitly specifies "
            "another reference date. Dates in source data or generated task briefs do not "
            "change the current date."
        )
        return "\n\n".join(block for block in (temporal, text) if block)

    def _record_projected_inputs(self, state: Any, original_state: Any) -> None:
        """Track candidates of the cells projected into this request.

        Display hints omit the identity of their chosen source, so dependencies
        are deliberately conservative within projected cells, never the whole
        evidence store. A count-only task listing does not claim to read its
        report or all task-bound facts.
        """
        from searchos.harness.p1_runtime import current_run_id, current_scope, record_snapshot

        if not current_run_id.get():
            return
        cmap = state.coverage_map
        if not cmap.tables:
            record_snapshot(self.workspace, original_state)
            return
        keys: set[str] = set()
        if self.view == "task":
            assigned, entities, task_table = self._projection._resolve_agent_target_cells(state)
            if assigned:
                keys = assigned & cmap.cells.keys()
            else:
                tid = (task_table if task_table in cmap.tables else
                       self.task.target_table if self.task.target_table in cmap.tables else
                       cmap.primary_table_id)
                table = cmap.tables[tid]
                discovery = not entities and table.is_column_only
                keys = {cmap.cell_key(tid, entity, attribute)
                        for entity in table.entities if discovery or entity in entities
                        for attribute in table.attributes if attribute not in table.primary_key}
        else:
            # Overview/compact renderers show data-cell state in these rows.
            for tid, table in cmap.tables.items():
                for entity in table.entities:
                    row_keys = [
                        cmap.cell_key(tid, entity, attribute)
                        for attribute in table.attributes if attribute not in table.primary_key
                    ]
                    if self.view == "writing" or any(
                        key in cmap.cells and (cmap.cells[key].has_conflict
                                               or cmap.cells[key].status.value != "filled")
                        for key in row_keys
                    ):
                        keys.update(row_keys)
        from searchos.socm.coverage import answer_evidence, project_cell_evidence
        nodes = {node.id: node for node in answer_evidence(state, use_scope=current_scope(state))}
        evidence_ids = set()
        for key in keys:
            cell = cmap.cells.get(key)
            binding = cmap.cell_binding(key)
            if cell is None or binding is None:
                continue
            evidence_ids.update(
                eid for eid in cell.supporting_evidence_ids
                if eid in nodes and project_cell_evidence(cmap, nodes[eid], *binding) is not None
            )
        record_snapshot(self.workspace, original_state, evidence_ids=sorted(evidence_ids))

    async def awrap_model_call(self, request: Any, handler: Callable) -> Any:
        text = self.render()
        if self.control is not None:
            self.control.context_snapshot = text
        stopped = self.control is not None and self.control.stopped
        guidance = (
            "Search is stopped. Use these records only to summarize partial findings and "
            "unresolved limitations; do not call tools or resume searching."
            if stopped
            else self.guidance
        )
        content = f"[AUTOMATED HARNESS — state snapshot]\n{guidance}\n\n{text}" if text else ""
        if content:
            request = inject_context(request, content, "socm_update")
            self._last_text = content
        return await handler(request)
