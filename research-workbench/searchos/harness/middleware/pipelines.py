"""默认组合：创建业务对象、列出 middleware、调用同一个图入口。"""

from __future__ import annotations

from searchos.config.run_config import current_run_config
from searchos.config.settings import settings
from searchos.harness.middleware.context.compaction_context_middleware import (
    CompactionContextMiddleware,
)
from searchos.harness.middleware.context.dynamic_trim_middleware import DynamicTrimMiddleware
from searchos.harness.middleware.context.search_episode_middleware import SearchEpisodeMiddleware
from searchos.harness.middleware.context.socm_middleware import SOCMContextMiddleware
from searchos.harness.middleware.context.tool_history_middleware import ToolHistoryMiddleware
from searchos.harness.middleware.context.tool_results_middleware import (
    ToolResultLimitMiddleware,
)
from searchos.harness.middleware.extraction import (
    ExtractionConfig,
    ExtractionCoordinator,
    FinalSummaryEvidenceMiddleware,
    LocalEvidenceMiddleware,
    WebEvidenceMiddleware,
)
from searchos.harness.middleware.models import SourceContext, TaskContext
from searchos.harness.middleware.sensor.budget_middleware import BudgetMiddleware
from searchos.harness.middleware.sensor.core.models import BudgetState, ControlState
from searchos.harness.middleware.sensor.detection_middleware import (
    CoverageStallMiddleware,
    DispatchRoundMiddleware,
    EmptyResultMiddleware,
    QueryRepetitionMiddleware,
    ReadingReminderMiddleware,
    StateStallMiddleware,
)
from searchos.harness.middleware.sensor.progress_middleware import (
    ProgressGuardMiddleware,
    ReadingProgressMiddleware,
    ToolProgressMiddleware,
)
from searchos.harness.middleware.sensor.route_middleware import RetryRouteMiddleware

ORCHESTRATOR_DRAIN_TOOLS = frozenset(
    {
        "check_agents",
        "add_entities",
        "edit_entities",
        "remove_entities",
        "inspect_table",
        "python",
        "stop_task",
    }
)
WRITER_DRAIN_TOOLS = frozenset(
    {
        "read_outline",
        "update_outline",
        "write_section",
        "edit_section",
        "annotate_section",
        "read_coverage",
        "read_evidence",
        "resolve_cell_provenance",
        "list_frontier",
        "read_task_report",
    }
)
SEARCH_RESULT_LIMITS = dict.fromkeys(
    {
        "web_search",
        "tavily_search",
        "serper_search",
        "browser_navigate",
        "search",
        "local_catalog",
    },
    6000,
) | {"explore_web": 48000}


def _graph(model, tools, system_prompt, middleware, **graph_options):
    from searchos.agents.runtime import create_search_agent_graph

    return create_search_agent_graph(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware,
        **graph_options,
    )


def _worker_defaults(
    workspace,
    task,
    budget,
    control,
    *,
    history,
    view="task",
    drain_tools=frozenset(),
    compress_history=True,
    agent_graphs=None,
    model=None,
):
    middleware = [
        BudgetMiddleware(
            budget, control=control, workspace=workspace, task=task, drain_tools=drain_tools
        ),
        RetryRouteMiddleware(workspace, agent_id=task.agent_id),
        QueryRepetitionMiddleware(control, workspace=workspace, task=task),
        ReadingReminderMiddleware(control),
        EmptyResultMiddleware(control, workspace=workspace, task=task),
        ToolResultLimitMiddleware(SEARCH_RESULT_LIMITS),
    ]
    p2 = _p2_compactor(model, workspace, task)
    if p2 is None and not isinstance(history, DynamicTrimMiddleware):
        middleware.insert(0, history)
    if settings.enable_socm_projection:
        middleware.append(
            SOCMContextMiddleware(
                workspace,
                task=task,
                view=view,
                control=control,
                agent_graphs=agent_graphs,
                heartbeat_turns=settings.socm_heartbeat_turns,
            )
        )
    if p2 is None and compress_history and settings.compress_old_tool_results:
        middleware.append(ToolHistoryMiddleware())
    if p2 is not None:
        middleware.append(p2)
        return middleware
    middleware.append(
        history
        if isinstance(history, DynamicTrimMiddleware)
        else DynamicTrimMiddleware(max_tokens_fraction=settings.dynamic_trim_fraction)
    )
    return middleware


def _p2_compactor(model, workspace, task):
    """One persistent compactor; do not pre-fold its source history."""
    if not current_run_config().enable_p2_context_memory:
        return None
    # Only the orchestrator sees real conversation turns. A worker without a
    # frontier binding still receives a generated assignment, never a user edit.
    context_task_id = task.task_id or (
        "__answer__" if task.agent_id == "orchestrator"
        else "__worker__:" + (task.agent_id or "unassigned")
    )
    context_window = current_run_config().p2_context_window or None
    if getattr(model, "_llm_type", "") == "codex-cli-action-bridge":
        # The CLI bridge bounds its JSON envelope by UTF-8 bytes, while the
        # compactor estimates tokens. Keep a conservative window with room for
        # tool schemas and CLI instructions rather than using the nominal 100k.
        # Workbench supplies a 200k-byte envelope budget. Smaller standalone
        # bridge instances keep the smaller cap, and the bridge still checks
        # the complete serialized request before invoking any model.
        cli_cap = 32768 if getattr(model, "max_input_tokens", 0) >= 200000 else 16384
        context_window = min(context_window or cli_cap, cli_cap)
    compactor = CompactionContextMiddleware(
        model, workspace=workspace, task_id=context_task_id, p2_enabled=True,
        context_window=context_window,
        summary_fraction=current_run_config().p2_summary_fraction,
        max_experiences=current_run_config().p2_max_experiences,
        experience_max_chars=current_run_config().p2_experience_max_chars,
    )

    def enable(state):
        state.p2.enabled = True
        return state

    workspace.atomic_update_state(enable)
    return compactor


def _extraction(model, workspace, task, policy, trajectory_logger, *, batch_n=5):
    if model is None:
        return None
    return ExtractionCoordinator(
        model,
        workspace=workspace,
        task=task,
        policy=policy,
        config=ExtractionConfig.from_settings(),
        trajectory_logger=trajectory_logger,
        batch_n=batch_n,
    )


def _search_progress(workspace, task, control, extraction):
    guard = (
        ProgressGuardMiddleware(
            extraction, control, task=task, limit=settings.search_agent_no_progress_tool_limit
        )
        if extraction is not None
        else ToolProgressMiddleware(
            control,
            workspace=workspace,
            task=task,
            limit=settings.search_agent_no_progress_tool_limit,
        )
    )
    middleware = [guard, ReadingProgressMiddleware(set())]
    if workspace is not None:
        middleware.append(StateStallMiddleware(workspace, control=control, task=task))
    return middleware


def web_search_pipeline(
    model,
    tools,
    *,
    workspace,
    task: TaskContext,
    system_prompt="",
    budget=None,
    control=None,
    extraction_model=None,
    extraction_policy="task_rows",
    final_summary=True,
    source: SourceContext | None = None,
    agent_graphs=None,
    trajectory_logger=None,
    conversation_logger=None,
    **graph_options,
):
    """网页检索默认组合；修改来源行为可直接手写 middleware 列表。"""
    budget, control = budget or BudgetState(), control or ControlState()
    extraction = _extraction(
        extraction_model, workspace, task, extraction_policy, trajectory_logger
    )
    history = (
        SearchEpisodeMiddleware(
            workspace,
            evidence_source=extraction,
            history_max_tokens=settings.layered_context_layer2_max_tokens,
        )
        if settings.use_layered_context
        else DynamicTrimMiddleware(max_tokens_fraction=settings.dynamic_trim_fraction)
    )
    middleware = [
        *_search_progress(workspace, task, control, extraction),
        *_worker_defaults(
            workspace,
            task,
            budget,
            control,
            history=history,
            compress_history=True,
            agent_graphs=agent_graphs,
            model=model,
        ),
    ]
    if extraction is not None:
        middleware.append(
            WebEvidenceMiddleware(extraction, source=source, trajectory_logger=trajectory_logger)
        )
        if final_summary:
            middleware.append(FinalSummaryEvidenceMiddleware(extraction))
    return _graph(
        model,
        tools,
        system_prompt,
        middleware,
        control=control,
        workspace=workspace,
        trajectory_logger=trajectory_logger,
        conversation_logger=conversation_logger,
        agent_id=task.agent_id,
        **graph_options,
    )


def local_search_pipeline(
    model,
    tools,
    *,
    workspace,
    task: TaskContext,
    system_prompt="",
    budget=None,
    control=None,
    extraction_model=None,
    extraction_policy="task_rows",
    final_summary=True,
    source: SourceContext | None = None,
    agent_graphs=None,
    trajectory_logger=None,
    conversation_logger=None,
    **graph_options,
):
    """本地检索默认组合，保留原文窗口供下一轮推理使用。"""
    budget, control = budget or BudgetState(), control or ControlState()
    extraction = _extraction(
        extraction_model, workspace, task, extraction_policy, trajectory_logger,
        # Enhanced local research often reads just one short source per task.
        # Start its real extraction promptly, instead of waiting until teardown
        # for a five-observation buffer to fill. Baseline behavior is unchanged.
        batch_n=1 if current_run_config().enhancements_enabled else 5,
    )
    middleware = [
        *_search_progress(workspace, task, control, extraction),
        *_worker_defaults(
            workspace,
            task,
            budget,
            control,
            history=DynamicTrimMiddleware(max_tokens_fraction=settings.dynamic_trim_fraction),
            compress_history=False,
            agent_graphs=agent_graphs,
            model=model,
        ),
    ]
    if extraction is not None:
        middleware.append(LocalEvidenceMiddleware(extraction, source=source))
        if final_summary:
            middleware.append(FinalSummaryEvidenceMiddleware(extraction))
    return _graph(
        model,
        tools,
        system_prompt,
        middleware,
        control=control,
        workspace=workspace,
        trajectory_logger=trajectory_logger,
        conversation_logger=conversation_logger,
        agent_id=task.agent_id,
        **graph_options,
    )


def explore_pipeline(
    model,
    tools,
    *,
    workspace,
    task: TaskContext,
    system_prompt="",
    budget=None,
    control=None,
    agent_graphs=None,
    trajectory_logger=None,
    conversation_logger=None,
    **graph_options,
):
    budget, control = budget or BudgetState(), control or ControlState()
    middleware = _worker_defaults(
        workspace,
        task,
        budget,
        control,
        history=DynamicTrimMiddleware(max_tokens_fraction=settings.dynamic_trim_fraction),
        agent_graphs=agent_graphs,
        model=model,
    )
    from searchos.agents.explore.policy import ExplorePolicy

    policy = None
    if settings.enable_explore_batch:
        config = current_run_config()
        policy = ExplorePolicy(
            max_waves=config.explore_max_waves,
            control=control,
        )
    return _graph(
        model,
        tools,
        system_prompt,
        middleware,
        control=control,
        workspace=workspace,
        trajectory_logger=trajectory_logger,
        conversation_logger=conversation_logger,
        agent_id=task.agent_id,
        policy=policy,
        emit_tool_started=True,
        **graph_options,
    )


def writer_pipeline(
    model,
    tools,
    *,
    workspace,
    task: TaskContext,
    system_prompt="",
    budget=None,
    control=None,
    agent_graphs=None,
    trajectory_logger=None,
    conversation_logger=None,
    **graph_options,
):
    budget, control = budget or BudgetState(), control or ControlState()
    middleware = _worker_defaults(
        workspace,
        task,
        budget,
        control,
        history=DynamicTrimMiddleware(max_tokens_fraction=settings.dynamic_trim_fraction),
        view="writing",
        drain_tools=WRITER_DRAIN_TOOLS,
        agent_graphs=agent_graphs,
        model=model,
    )
    return _graph(
        model,
        tools,
        system_prompt,
        middleware,
        control=control,
        workspace=workspace,
        trajectory_logger=trajectory_logger,
        conversation_logger=conversation_logger,
        agent_id=task.agent_id,
        drain_tools=WRITER_DRAIN_TOOLS,
        finish_without_tools=False,
        **graph_options,
    )


def orchestrator_pipeline(
    model,
    tools,
    *,
    workspace,
    system_prompt="",
    budget=None,
    control=None,
    agent_graphs=None,
    task_pool=None,
    on_exhausted=None,
    trajectory_logger=None,
    conversation_logger=None,
    steer_queue=None,
    **graph_options,
):
    budget = budget or BudgetState(max_queries=0, max_iterations=settings.orch_max_iterations)
    control = control or ControlState()
    middleware = [
        BudgetMiddleware(
            budget,
            control=control,
            drain_tools=ORCHESTRATOR_DRAIN_TOOLS,
            on_exhausted=on_exhausted,
            warn_iterations=True,
            wrap_up="Drain running agents with check_agents, inspect the live table, then answer.",
        ),
        RetryRouteMiddleware(workspace, agent_id="orchestrator"),
        DispatchRoundMiddleware(control, max_rounds=settings.orch_max_dispatches),
        CoverageStallMiddleware(
            workspace,
            control=control,
            agent_graphs=agent_graphs if agent_graphs is not None else {},
            max_stalled_rounds=settings.orch_coverage_stall_rounds,
        ),
        ToolResultLimitMiddleware(SEARCH_RESULT_LIMITS),
    ]
    if steer_queue is not None:
        from searchos.harness.middleware.context.steering_middleware import SteeringMiddleware

        middleware.insert(0, SteeringMiddleware(steer_queue, trajectory_logger=trajectory_logger))
    if settings.enable_socm_projection:
        middleware.append(
            SOCMContextMiddleware(
                workspace,
                task=TaskContext(agent_id="orchestrator"),
                view="overview",
                control=control,
                heartbeat_turns=settings.socm_heartbeat_turns,
            )
        )
    from searchos.agents.orchestrator.policy import DispatchPolicy

    policy = None
    if settings.max_parallel_agents > 0:
        policy = DispatchPolicy(
            workspace,
            agents=task_pool if task_pool is not None else {},
            control=control,
            capacity=settings.max_parallel_agents,
            cooldown_s=settings.pool_nudge_cooldown_s,
            max_reminders=settings.pool_nudge_max_count,
        )
    p2 = _p2_compactor(model, workspace, TaskContext(agent_id="orchestrator"))
    if p2 is None and settings.compress_old_tool_results:
        middleware.append(ToolHistoryMiddleware())
    middleware.append(p2 if p2 is not None else
                      DynamicTrimMiddleware(max_tokens=settings.orch_trim_max_tokens))
    return _graph(
        model,
        tools,
        system_prompt,
        middleware,
        control=control,
        workspace=workspace,
        trajectory_logger=trajectory_logger,
        conversation_logger=conversation_logger,
        agent_id="orchestrator",
        policy=policy,
        drain_tools=ORCHESTRATOR_DRAIN_TOOLS,
        finish_without_tools=False,
        **graph_options,
    )
