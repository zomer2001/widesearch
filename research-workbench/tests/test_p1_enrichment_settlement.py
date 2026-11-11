"""Optional P1 annotation must not reopen an already committed observation."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from searchos.harness.middleware.extraction import (
    DeliveryMode,
    EvidenceObservation,
    ExtractionConfig,
    ExtractionCoordinator,
)
from searchos.harness.middleware.models import TaskContext
from searchos.socm import SearchState
from searchos.socm.workspace import WorkspaceManager


class SlowAnnotationJudge:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.annotation_calls = 0
        self.extraction_calls = 0
        self.active_annotations = 0
        self.cancelled_annotations = 0

    async def ainvoke(self, prompt):
        if prompt.startswith("P1_CLAIM_"):
            self.annotation_calls += 1
            self.active_annotations += 1
            self.started.set()
            try:
                await self.release.wait()
                return SimpleNamespace(content="[]")
            except asyncio.CancelledError:
                self.cancelled_annotations += 1
                raise
            finally:
                self.active_annotations -= 1
        self.extraction_calls += 1
        return SimpleNamespace(content=json.dumps([{
            "Company": "Acme", "CEO": "Alice", "_source_page": 1,
            "_source_excerpt": "Acme's CEO is Alice.",
        }]))


def setup(tmp_path, judge, *, background=False, enabled=True):
    workspace = WorkspaceManager(tmp_path, "p1-settlement")
    workspace.create()
    state = SearchState()
    state.coverage_map.initialize(
        ["Acme"], ["Company", "CEO"], primary_key=["Company"], table_id="companies",
    )
    workspace.save_state(state)
    coordinator = ExtractionCoordinator(
        judge, workspace=workspace, batch_n=1 if background else 5,
        config=ExtractionConfig(dual_mode=False, cross_table_backfill=False,
                                p1_evidence_reasoning=enabled),
        task=TaskContext(agent_id="reader"),
    )
    return workspace, coordinator


def observation():
    return EvidenceObservation(
        content="Company leadership statement. Acme's CEO is Alice. This is a company-wide role.",
        source_url="https://example.org/company",
        target_table="companies",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_annotation_timeout_settles_original_observation_and_preserves_progress(
    tmp_path, background
):
    judge = SlowAnnotationJudge()
    workspace, coordinator = setup(tmp_path, judge, background=background)
    assert (await coordinator.submit(observation())).accepted
    if background:
        await asyncio.wait_for(judge.started.wait(), 1)
        # Base facts must already be attributed before optional work awaits.
        assert coordinator.committed_node_ids

    summary = await coordinator.finalize(timeout=0.08)

    assert summary.buffered == summary.in_flight == 0
    assert summary.committed_node_ids
    assert len(workspace.load_state().evidence_graph.nodes) == 1
    assert coordinator._retry_items == {}
    assert all(record["status"] == "done" for record in coordinator._journal.load())
    assert coordinator._deferred_annotation_ids == set(summary.committed_node_ids)
    assert coordinator._annotation_tasks == set()
    assert judge.active_annotations == 0
    assert judge.extraction_calls == 1
    if background:
        # One interrupted annotation was already running; only metadata is
        # resumed during the finalization remainder, never the source read.
        assert judge.cancelled_annotations == 2
        assert judge.annotation_calls == 2
    else:
        assert judge.cancelled_annotations == judge.annotation_calls == 1

    judge.release.set()
    later = await coordinator.finalize(timeout=1)
    assert later.buffered == later.in_flight == 0
    assert coordinator._deferred_annotation_ids == set()
    assert judge.extraction_calls == 1
    assert len(workspace.load_state().evidence_graph.nodes) == 1


@pytest.mark.asyncio
async def test_external_cancel_during_deferred_annotation_still_propagates(tmp_path):
    judge = SlowAnnotationJudge()
    workspace, coordinator = setup(tmp_path, judge)
    await coordinator.submit(observation())
    finalizing = asyncio.create_task(coordinator.finalize(timeout=2))
    await asyncio.wait_for(judge.started.wait(), 1)

    finalizing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finalizing

    assert judge.active_annotations == 0
    assert len(workspace.load_state().evidence_graph.nodes) == 1
    assert coordinator.committed_node_ids
    assert coordinator._retry_items == {}
    assert coordinator._deferred_annotation_ids
    assert all(record["status"] == "done" for record in coordinator._journal.load())


@pytest.mark.asyncio
async def test_external_cancel_during_normal_annotation_is_not_finalize_deferral(tmp_path):
    judge = SlowAnnotationJudge()
    workspace, coordinator = setup(tmp_path, judge)
    submitted = asyncio.create_task(coordinator.submit(observation(), delivery=DeliveryMode.SYNC))
    await asyncio.wait_for(judge.started.wait(), 1)

    submitted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submitted

    assert judge.active_annotations == 0
    assert coordinator._annotation_tasks == set()
    assert len(workspace.load_state().evidence_graph.nodes) == 1
    assert coordinator.committed_node_ids
    # External cancellation retains the source observation for explicit
    # recovery, unlike cancellation of just optional metadata by finalize.
    assert coordinator._retry_items


@pytest.mark.asyncio
async def test_switch_off_does_not_start_or_defer_annotation(tmp_path):
    judge = SlowAnnotationJudge()
    workspace, coordinator = setup(tmp_path, judge, enabled=False)
    await coordinator.submit(observation())

    summary = await coordinator.finalize(timeout=1)

    assert summary.buffered == summary.in_flight == 0
    assert summary.committed_node_ids
    assert judge.annotation_calls == 0
    assert coordinator._deferred_annotation_ids == set()
    assert workspace.load_state().evidence_graph.claim_annotations == {}


@pytest.mark.asyncio
async def test_annotation_failure_keeps_source_settled_for_later_metadata_retry(
    tmp_path, monkeypatch
):
    judge = SlowAnnotationJudge()
    workspace, coordinator = setup(tmp_path, judge)
    calls = 0

    async def unavailable(_evidence_ids):
        nonlocal calls
        calls += 1
        raise RuntimeError("Optional annotation backend unavailable")

    monkeypatch.setattr(coordinator, "_run_claim_enrichment", unavailable)
    receipt = await coordinator.submit(observation(), delivery=DeliveryMode.SYNC)

    assert all(report.succeeded for report in receipt.extraction_reports)
    assert receipt.committed_nodes
    assert calls == 1
    assert coordinator._retry_items == {}
    assert coordinator._deferred_annotation_ids
    assert len(workspace.load_state().evidence_graph.nodes) == 1
    assert all(record["status"] == "done" for record in coordinator._journal.load())


@pytest.mark.asyncio
async def test_no_annotation_call_starts_when_finalization_budget_is_exhausted(
    tmp_path, monkeypatch
):
    judge = SlowAnnotationJudge()
    _, coordinator = setup(tmp_path, judge)
    coordinator._deferred_annotation_ids.add("already-committed")

    async def unexpected(_ids):
        pytest.fail("No remaining finalization budget should mean no annotation call")

    monkeypatch.setattr(coordinator, "_run_claim_enrichment", unexpected)

    await coordinator._finish_deferred_annotations(asyncio.get_running_loop().time() - 1)

    assert coordinator._deferred_annotation_ids == {"already-committed"}


@pytest.mark.asyncio
async def test_deferred_annotation_requeues_affected_comparison_without_new_fact_extraction(
    tmp_path, monkeypatch
):
    judge = SlowAnnotationJudge()
    judge.release.set()
    _, coordinator = setup(tmp_path, judge)
    await coordinator.submit(observation(), delivery=DeliveryMode.SYNC)
    coordinator._deferred_annotation_ids.update(coordinator.committed_node_ids)
    assert coordinator._deferred_comparison_keys == set()
    events = []

    async def completed(_ids):
        events.append("annotation")
        # A partially completed previous attempt may have already stored the
        # annotation. Even accepted=0 must refresh the pair's changed version.
        return {"accepted": 0, "failed_batches": 0}

    async def review(keys):
        events.append(("comparison", keys))
        return {}

    monkeypatch.setattr(coordinator, "_run_claim_enrichment", completed)
    monkeypatch.setattr(coordinator._conflicts, "review", review)

    await coordinator.finalize(timeout=1)

    assert events == ["annotation", ("comparison", {"companies/Acme.CEO"})]
    assert coordinator._deferred_annotation_ids == set()
    assert coordinator._deferred_comparison_keys == set()
    assert judge.extraction_calls == 1


def test_annotation_timeout_setting_preserves_default_and_is_carried_to_extraction(monkeypatch):
    from searchos.config.settings import Settings, settings

    assert Settings.model_fields["p1_annotation_timeout_s"].default == 45.0
    assert ExtractionConfig().p1_annotation_timeout_s == 45.0
    monkeypatch.setattr(settings, "p1_annotation_timeout_s", 240.0)
    assert ExtractionConfig.from_settings().p1_annotation_timeout_s == 240.0


@pytest.mark.parametrize("timeout", [0, -1, 301, float("inf"), float("nan")])
def test_annotation_timeout_has_finite_positive_upper_bound(timeout):
    from pydantic import ValidationError

    from searchos.config.settings import Settings

    with pytest.raises(ValueError, match="p1_annotation_timeout_s"):
        ExtractionConfig(p1_annotation_timeout_s=timeout)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, p1_annotation_timeout_s=timeout)


@pytest.mark.asyncio
async def test_annotation_coordinator_uses_configured_timeout(tmp_path, monkeypatch):
    from dataclasses import replace

    judge = SlowAnnotationJudge()
    _, coordinator = setup(tmp_path, judge)
    coordinator._config = replace(coordinator._config, p1_annotation_timeout_s=240.0)
    observed = []

    async def wait_for(awaitable, timeout):
        observed.append(timeout)
        return await awaitable

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    report = await coordinator._run_claim_enrichment([])

    assert observed == [240.0]
    assert report["attempted"] == report["accepted"] == 0
