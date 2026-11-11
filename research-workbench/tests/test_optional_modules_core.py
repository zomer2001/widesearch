"""Task-local innovation selection and the unchanged disabled execution path."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from searchos.config.run_config import (
    EffectiveRunConfig,
    activate_run_config,
    current_run_config,
    normalize_module_config,
)
from searchos.config.settings import settings
from searchos.harness.middleware import pipelines
from searchos.harness.middleware.extraction.core.models import ExtractionConfig
from searchos.harness.middleware.models import TaskContext
from searchos.harness.p1_runtime import begin_run, current_run_id
from searchos.harness.session import SearchSession, _write_findings_answer
from searchos.local.reader.references import build_ref_id
from searchos.socm import SearchState
from searchos.socm.evidence import EvidenceNode
from searchos.socm.workspace import WorkspaceManager


def make_session(monkeypatch, tmp_path, enabled):
    from searchos.config import models

    monkeypatch.setattr(models, "get_model_for", lambda _: object())
    monkeypatch.setattr(settings, "enable_skills", False)
    return SearchSession(
        workspace_root=str(tmp_path),
        module_config={"schema_version": 1, "enabled": enabled, "implementation_version": "1"},
    )


@pytest.mark.asyncio
async def test_concurrent_sessions_freeze_selection_and_restore_context(monkeypatch, tmp_path):
    first = make_session(monkeypatch, tmp_path, ["evidence_revision"])
    second = make_session(monkeypatch, tmp_path, ["context_memory"])
    baseline = make_session(monkeypatch, tmp_path, [])
    monkeypatch.setattr(settings, "enable_p1_evidence_reasoning", True)
    monkeypatch.setattr(settings, "enable_p2_context_memory", True)
    observed = []

    async def capture(_query):
        before = current_run_config()
        await asyncio.sleep(0)
        assert current_run_config() is before
        observed.append((before.enable_p1_evidence_reasoning,
                         before.enable_p2_context_memory,
                         ExtractionConfig.from_settings().p1_evidence_reasoning))
        assert current_run_id.get() == ""
        raise RuntimeError("expected test interruption")

    for session in (first, second, baseline):
        session._run_impl = capture
    parent = EffectiveRunConfig.from_settings().with_modules({"enabled": []})
    with activate_run_config(parent):
        results = await asyncio.gather(*(item.run("test") for item in (first, second, baseline)),
                                       return_exceptions=True)
        assert current_run_config() is parent
    assert all(isinstance(item, RuntimeError) for item in results)
    assert sorted(observed) == [(False, False, False), (False, True, False), (True, False, True)]


def test_disabled_modules_create_no_tickets_compactor_or_annotations(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "enable_p1_evidence_reasoning", True)
    monkeypatch.setattr(settings, "enable_p2_context_memory", True)
    workspace = WorkspaceManager(tmp_path, "baseline")
    workspace.create()
    workspace.save_state(SearchState(intent="Question"))
    before = (workspace.path / "search_state.json").read_bytes()
    disabled = EffectiveRunConfig.from_settings().with_modules({"enabled": []})
    with activate_run_config(disabled):
        assert begin_run(workspace, "__answer__", "unused") == ""
        assert pipelines._p2_compactor(object(), workspace, TaskContext(agent_id="orchestrator")) is None
        assert not ExtractionConfig.from_settings().p1_evidence_reasoning
    assert (workspace.path / "search_state.json").read_bytes() == before


@pytest.mark.parametrize("enabled", [["evidence_revision"], ["context_memory"],
                                    ["evidence_revision", "context_memory"]])
def test_enhanced_local_session_enters_findings_and_preserves_manifest(monkeypatch, tmp_path, enabled):
    from searchos.harness.middleware.sensor.core import models as budgets

    session = make_session(monkeypatch, tmp_path, enabled)

    class Captured(Exception):
        pass

    def capture(**_):
        raise Captured

    monkeypatch.setattr(budgets, "BudgetState", capture)
    with pytest.raises(Captured):
        asyncio.run(session.run("Local question", session_id="local",
                                initial_state=SearchState(source_scope={"mode": "local", "documents": []})))
    state = WorkspaceManager(tmp_path, "local").load_state()
    assert state.research_mode == "findings"
    assert state.module_config == normalize_module_config({"enabled": enabled})
    assert state.p1.executions


def test_local_baseline_keeps_direct_agent_route(monkeypatch, tmp_path):
    session = make_session(monkeypatch, tmp_path, [])
    with pytest.raises(ValueError, match="direct Local Search Agent"):
        asyncio.run(session.run("Question", initial_state=SearchState(source_scope={"mode": "local"})))


def test_existing_enhanced_state_cannot_switch_modules(monkeypatch, tmp_path):
    session = make_session(monkeypatch, tmp_path, ["context_memory"])
    state = SearchState(module_config=normalize_module_config({"enabled": ["evidence_revision"]}))
    with pytest.raises(ValueError, match="fixed"):
        asyncio.run(session.run("Follow up", initial_state=state))


@pytest.mark.parametrize("bridge_budget,window", [(100000, 16384), (200000, 32768)])
def test_cli_memory_uses_conservative_window_without_enabling_claim_annotation(
    monkeypatch, tmp_path, bridge_budget, window,
):
    workspace = WorkspaceManager(tmp_path, "cli-window")
    workspace.create()
    workspace.save_state(SearchState())
    config = EffectiveRunConfig.from_settings().with_modules({"enabled": ["context_memory"]})
    model = SimpleNamespace(_llm_type="codex-cli-action-bridge", max_input_tokens=bridge_budget,
                            profile={"max_input_tokens": bridge_budget})
    with activate_run_config(config):
        component = pipelines._p2_compactor(model, workspace, TaskContext(agent_id="orchestrator"))
        assert component.context_window == window
        assert not ExtractionConfig.from_settings().p1_evidence_reasoning


def test_local_findings_resolve_only_current_selected_evidence(tmp_path):
    workspace = WorkspaceManager(tmp_path, "citations")
    workspace.create()
    revision = "a" * 64
    ref = build_ref_id(library_id="lib", document_id="doc", source_revision=revision,
                       reference_key="s1")
    wrong = build_ref_id(library_id="lib", document_id="doc", source_revision=revision,
                         reference_key="s2")
    state = SearchState(source_scope={"mode": "local", "documents": [
        {"library_id": "lib", "document_id": "doc", "source_revision": revision},
    ]})
    state.evidence_graph.nodes.append(EvidenceNode(id="fact", finding="A fact",
                                                   source=ref))
    workspace.save_state(state)
    answer = _write_findings_answer(workspace, state, "Question", {"messages": [AIMessage(
        content=f"A fact [source](evidence:fact); bad [citation]({wrong}).",
    )]})
    assert f"[source]({ref})" in answer
    assert wrong not in answer
    assert "invalid-local-reference" in answer
    import json

    assert json.loads((workspace.path / "output/result.json").read_text())["mode"] == "local_research"


def test_enhanced_local_report_grounding_and_seed_rejection():
    from searchos.tools.schema import _assert_cell_value
    from searchos.tools.tasks import _ground_hybrid_report

    revision = "b" * 64
    ref = build_ref_id(library_id="lib", document_id="doc", source_revision=revision,
                       reference_key="s1")
    state = SearchState(
        research_mode="findings",
        module_config=normalize_module_config({"enabled": ["evidence_revision"]}),
        source_scope={"mode": "local", "documents": [
            {"library_id": "lib", "document_id": "doc", "source_revision": revision},
        ]},
    )
    state.evidence_graph.nodes.append(EvidenceNode(id="fact", finding="Verbatim fact", source=ref))
    report = {"last_message": "Unsupported worker assertion",
              "evidence_bindings": [{"evidence_id": "fact"}]}
    _ground_hybrid_report(report, state)
    assert report["agent_report"]["verified"] is False
    assert report["grounded_findings"][0]["source"] == ref
    assert "Unsupported worker assertion" not in report["last_message"]
    assert not _assert_cell_value(
        state, table_id="facts", entity="Example", attribute="Leader", value="Invented",
        source="agent://orchestrator/seed", evidence_prefix="seed", alignment_note="",
    )
    assert len(state.evidence_graph.nodes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_agent", [False, True])
async def test_enhanced_local_findings_can_target_existing_schema(
    monkeypatch, tmp_path, explicit_agent,
):
    import json

    from searchos.agents.runtime import set_orchestrator_context
    from searchos.tools import tasks
    from searchos.tools.search_state import _source_scope_var

    workspace = WorkspaceManager(tmp_path, "local-schema")
    workspace.create()
    state = SearchState(
        research_mode="findings",
        module_config=normalize_module_config({"enabled": ["evidence_revision"]}),
        source_scope={"mode": "local", "documents": [{"document_id": "fixture"}]},
    )
    state.coverage_map.initialize(["Example"], ["Name", "Leader"], primary_key=["Name"],
                                  table_id="leadership")
    workspace.save_state(state)
    set_orchestrator_context(workspace=workspace, model=object())

    async def tick():
        return {}

    monkeypatch.setattr(tasks, "_scheduler", lambda: SimpleNamespace(tick=tick))
    token = _source_scope_var.set(state.source_scope)
    try:
        result = json.loads(await tasks.enqueue_tasks.ainvoke({"items_json": json.dumps([{
            **({"agent_type": "local_search_agent"} if explicit_agent else {}),
            "task": "Read the selected appointment notice",
            "result_mode": "findings", "target_table": "leadership",
        }])}))
    finally:
        _source_scope_var.reset(token)
    assert len(result["queued"]) == 1
    assert not result.get("errors")
    assert workspace.load_state().frontier.questions[0].result_mode == "findings"
    assert workspace.load_state().frontier.questions[0].agent_type == "local_search_agent"


@pytest.mark.parametrize("manifest", [{"enabled": ["unknown"]}, {"enabled": "context_memory"},
                                      {"schema_version": 2}, {"implementation_version": "99"}])
def test_unknown_module_contract_rejected(manifest):
    with pytest.raises(ValueError):
        normalize_module_config(manifest)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,explicit,expected", [
    ("local", "", "local_search_agent"),
    ("hybrid", "", "search_agent"),
    ("web", "", "search_agent"),
    ("local", "search_agent", None),
])
async def test_real_scheduler_routes_omitted_agent_by_source_scope(
    monkeypatch, tmp_path, mode, explicit, expected,
):
    from searchos.agents import runtime
    from searchos.agents.orchestrator.scheduler import Scheduler
    from searchos.local.library import LocalLibraryService
    from searchos.socm import FrontierTask
    from searchos.tools.search_state import _source_scope_var

    monkeypatch.setattr(settings, "enable_skills", False)
    monkeypatch.setattr(settings, "enable_scheduler", True)
    monkeypatch.setattr(settings, "max_parallel_agents", 1)
    library_service = LocalLibraryService(tmp_path / "library")
    library = library_service.create_library("Fixture")
    source = tmp_path / "notice.txt"
    source.write_text("Synthetic appointment notice for scheduler routing.")
    document = await library_service.index_uploaded_file(library.library_id, source, "notice.txt")
    record, _ = library_service._verified_blocks(library.library_id, [document["document_id"]])[0]
    scope = {"mode": mode, "index_root": library.index_root,
             "documents": [record.model_dump(mode="json")]}
    workspace = WorkspaceManager(tmp_path / "runs", "routing")
    workspace.create()
    state = SearchState(research_mode="findings", source_scope=scope)
    state.frontier.add(FrontierTask(id="seeded", question="Read the notice", kind="search",
                                   agent_type=explicit, result_mode="findings"))
    workspace.save_state(state)
    runtime.set_orchestrator_context(workspace=workspace, model=object())
    calls = []

    class Graph:
        async def astream(self, *_args, **_kwargs):
            yield {"messages": [AIMessage(content="The routing fixture has completed.")]}

    def local_pipeline(**kwargs):
        calls.append(("local_search_agent", kwargs))
        return Graph()

    def web_pipeline(**kwargs):
        calls.append(("search_agent", kwargs))
        return Graph()

    monkeypatch.setattr(pipelines, "local_search_pipeline", local_pipeline)
    monkeypatch.setattr(pipelines, "web_search_pipeline", web_pipeline)
    token = _source_scope_var.set(scope)
    try:
        if expected is None:
            with pytest.raises(ValueError, match="unavailable.*search_agent"):
                await Scheduler().drain_ready_tasks()
            assert not calls
        else:
            result = await Scheduler().drain_ready_tasks()
            assert result["spawned"] == 1
            assert result["agents"][0]["agent_type"] == expected
            await asyncio.gather(*runtime._ctx.task_pool.values())
            assert [call[0] for call in calls] == [expected]
            assert calls[0][1]["extraction_policy"] == "schema_facts"
            assert calls[0][1]["final_summary"] is False
    finally:
        _source_scope_var.reset(token)


@pytest.mark.parametrize("targeted", [False, True])
def test_session_generated_context_never_changes_p2_user_goal(tmp_path, targeted):
    from langchain_core.messages import HumanMessage, convert_to_messages

    from searchos.harness.middleware.context.compaction_context_middleware import (
        CompactionContextMiddleware,
    )
    from searchos.harness.session import (
        _harness_message,
        _premature_end_nudge,
        _session_input_messages,
    )

    workspace = WorkspaceManager(tmp_path, "goal-ownership")
    workspace.create()
    workspace.save_state(SearchState(intent="Compare the selected notices."))
    component = CompactionContextMiddleware(
        SimpleNamespace(profile={"max_input_tokens": 32768}), workspace=workspace,
        p2_enabled=True, task_id="__answer__",
    )
    messages = convert_to_messages(_session_input_messages(
        "Compare the 2025 appointment.", context_preamble="Earlier assistant: guessed an answer.",
        targeted_repair_mode=targeted, targeted_repair_cells=["leadership/Example.Leader"],
    ))
    component._prepare_packet(messages)
    before = workspace.load_state().p2.goal
    assert before.user_messages == ["Compare the 2025 appointment."]
    assert any(message.additional_kwargs.get("sf_synthetic") for message in messages)

    for kind, text in [
        ("premature_end_resume", _premature_end_nudge("2 workers remain")),
        ("writer_finalize", "Budget exhausted; finalize from current evidence."),
    ]:
        messages.extend(convert_to_messages([_harness_message(text, kind)]))
    component._prepare_packet(messages)
    after = workspace.load_state().p2.goal
    assert after == before
    # Actual user follow-up still has its own authority and changes the version.
    messages.append(HumanMessage(content="Correction: also compare 2024."))
    component._prepare_packet(messages)
    changed = workspace.load_state().p2.goal
    assert changed.version == before.version + 1
    assert changed.user_messages == ["Compare the 2025 appointment.", "Correction: also compare 2024."]
