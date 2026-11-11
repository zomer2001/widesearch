"""Committed facts survive optional, independently verified P1 metadata enrichment."""

import asyncio
import hashlib
import json
from datetime import date

import pytest

from searchos.harness.middleware.extraction.core.claim_enrichment import (
    MAX_BATCH_CHARS,
    MAX_BATCH_ITEMS,
    MAX_EXCERPT_CHARS,
    enrich_claims,
)
from searchos.harness.middleware.extraction.core.store import InMemoryEvidenceStore
from searchos.socm import SearchState
from searchos.socm.claim_annotations import evidence_fingerprint
from searchos.socm.evidence import EvidenceNode, EvidenceStatus, SourceObservation

QUOTE = "Effective from 2024-01-01 through 2024-12-31."
EXCERPT = "Acme's CEO is Alice. " + QUOTE


def make_store(count=1, *, excerpt=EXCERPT):
    state = SearchState()
    for index in range(count):
        source = f"https://example.org/source-{index}"
        observation = SourceObservation.capture(source, "Heading\n" + excerpt + "\nFooter")
        node = EvidenceNode(
            id=f"ev-{index}", finding="Acme's CEO is Alice.", source=source,
            source_excerpt=excerpt, observation_id=observation.id,
            span=(len("Heading\n"), len("Heading\n") + len(excerpt)),
            text_hash="sha1:" + hashlib.sha1(excerpt.encode()).hexdigest(),
        )
        state.evidence_graph.nodes.append(node)
        state.evidence_graph.observations.append(observation)
    return InMemoryEvidenceStore(state)


def candidate(evidence_id="ev-0", *, valid_time="default", **updates):
    if valid_time == "default":
        valid_time = {"start": "2024-01-01", "end": "2024-12-31", "basis_quote": QUOTE}
    row = dict(evidence_id=evidence_id, entity="Acme", attribute="CEO", value="Alice",
               scope_key="company-wide executive role", valid_time=valid_time)
    row.update(updates)
    return row


def verdict(evidence_id="ev-0", **updates):
    row = dict(evidence_id=evidence_id, metadata_supported=True, time_supported=True,
               scope_is_non_temporal=True, reason="The excerpt supports the claim and dates.")
    row.update(updates)
    return row


class ScriptedInvoke:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        assert self.responses, "Unexpected additional model call"
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response()
        return response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)


def run(store, invoke, ids=None):
    ids = ids if ids is not None else [node.id for node in store.state.evidence_graph.nodes]
    return asyncio.run(enrich_claims(ids, store=store, invoke=invoke))


def test_enrichment_uses_two_calls_and_only_adds_version_bound_annotation():
    store = make_store()
    original_nodes = [node.model_dump() for node in store.state.evidence_graph.nodes]
    original_observations = [item.model_dump() for item in store.state.evidence_graph.observations]
    invoke = ScriptedInvoke([candidate()], [verdict()])

    stats = run(store, invoke)

    annotation = store.state.evidence_graph.claim_annotations["ev-0"]
    assert stats["accepted"] == 1
    assert stats["unknown"] == 0
    assert annotation.verified is True
    assert annotation.valid_time.start == date(2024, 1, 1)
    assert annotation.evidence_fingerprint == evidence_fingerprint(
        store.state.evidence_graph.nodes[0])
    assert len(invoke.prompts) == 2
    assert invoke.prompts[0].startswith("P1_CLAIM_EXTRACT")
    assert invoke.prompts[1].startswith("P1_CLAIM_VERIFY")
    assert [node.model_dump() for node in store.state.evidence_graph.nodes] == original_nodes
    assert [item.model_dump() for item in store.state.evidence_graph.observations] == (
        original_observations)


def test_explicit_chinese_calendar_dates_can_anchor_iso_endpoints():
    quote = "有效期为2024年1月1日至2024年12月31日。"
    store = make_store(excerpt="Acme's CEO is Alice. " + quote)
    row = candidate(valid_time={"start": "2024-01-01", "end": "2024-12-31",
                                "basis_quote": quote})

    stats = run(store, ScriptedInvoke([row], [verdict()]))

    assert stats["accepted"] == 1
    assert store.state.evidence_graph.claim_annotations["ev-0"].valid_time.end == date(2024, 12, 31)


def test_unknown_time_keeps_verified_non_temporal_metadata_and_original_fact():
    store = make_store(excerpt="Acme's CEO is Alice; the effective dates are not specified.")

    stats = run(store, ScriptedInvoke(
        [candidate(valid_time=None)], [verdict(time_supported=False)]))

    assert stats["accepted"] == stats["unknown"] == 1
    assert store.state.evidence_graph.claim_annotations["ev-0"].valid_time is None
    assert store.state.evidence_graph.nodes[0].status == EvidenceStatus.ACTIVE


def test_publication_date_rejected_by_independent_review_becomes_unknown_effective_time():
    quote = "Published on 2024-01-01."
    store = make_store(excerpt="Acme's CEO is Alice. " + quote)
    row = candidate(valid_time={"start": "2024-01-01", "end": None, "basis_quote": quote})

    stats = run(store, ScriptedInvoke([row], [verdict(
        time_supported=False, reason="This is the publication date, not the effective period.")]))

    assert stats["accepted"] == stats["unknown"] == 1
    assert store.state.evidence_graph.claim_annotations["ev-0"].valid_time is None


@pytest.mark.parametrize("verdict_change", [
    {"metadata_supported": False}, {"scope_is_non_temporal": False},
    {"metadata_supported": "true"}, {"reason": ""},
])
def test_independent_review_must_explicitly_support_metadata_and_non_temporal_scope(verdict_change):
    store = make_store()

    stats = run(store, ScriptedInvoke([candidate()], [verdict(**verdict_change)]))

    assert stats["accepted"] == 0
    assert store.state.evidence_graph.claim_annotations == {}
    assert store.state.evidence_graph.nodes[0].status == EvidenceStatus.ACTIVE


@pytest.mark.parametrize("update", [
    {"entity": ""}, {"entity": 123}, {"scope_key": "current"}, {"scope_key": "2024 company"},
    {"valid_time": {"start": "2024-02-30", "end": None, "basis_quote": QUOTE}},
    {"valid_time": {"start": "2024", "end": None, "basis_quote": QUOTE}},
    {"valid_time": {"start": "2024-12-31", "end": "2024-01-01", "basis_quote": QUOTE}},
    {"valid_time": {"start": "2025-01-01", "end": None, "basis_quote": QUOTE}},
    {"valid_time": {"start": "2024-01-01", "end": None, "basis_quote": "Not in the excerpt"}},
    {"valid_time": {"start": None, "end": None, "basis_quote": "Not in the excerpt"}},
    {"valid_time": {"start": "2024-01-01", "end": None}},
])
def test_invalid_or_unanchored_candidates_are_dropped_before_independent_review(update):
    store = make_store()
    invoke = ScriptedInvoke([candidate(**update)])

    stats = run(store, invoke)

    assert stats["accepted"] == 0
    assert len(invoke.prompts) == 1
    assert store.state.evidence_graph.claim_annotations == {}


def test_missing_required_field_does_not_silently_default_to_supported_metadata():
    store = make_store()
    row = candidate()
    del row["valid_time"]

    assert run(store, ScriptedInvoke([row]))["accepted"] == 0


def test_duplicate_evidence_ids_reject_all_candidates_for_that_id():
    store = make_store()
    invoke = ScriptedInvoke([candidate(), candidate(value="Bob")])

    assert run(store, invoke)["accepted"] == 0
    assert len(invoke.prompts) == 1


def test_unknown_evidence_id_is_never_created_even_if_model_invents_one():
    store = make_store()
    invoke = ScriptedInvoke([candidate("invented-id")])

    assert run(store, invoke)["accepted"] == 0
    assert [node.id for node in store.state.evidence_graph.nodes] == ["ev-0"]


def test_duplicate_json_keys_are_rejected_instead_of_last_value_winning():
    store = make_store()
    raw = json.dumps([candidate()]).replace('"entity": "Acme"',
                                           '"entity": "Other", "entity": "Acme"')

    stats = run(store, ScriptedInvoke(raw))

    assert stats["accepted"] == 0
    assert stats["failed_batches"] == 1


def test_duplicate_verification_verdicts_cannot_resolve_to_true_by_order():
    store = make_store()

    stats = run(store, ScriptedInvoke(
        [candidate()], [verdict(), verdict(metadata_supported=False)]))

    assert stats["accepted"] == 0
    assert store.state.evidence_graph.claim_annotations == {}


@pytest.mark.parametrize("fault", ["missing_observation", "observation_hash", "evidence_hash",
                                   "span", "source", "inactive"])
def test_untrusted_or_stale_source_grounding_is_skipped_without_model_call(fault):
    store = make_store()
    graph = store.state.evidence_graph
    if fault == "missing_observation":
        graph.observations.clear()
    elif fault == "observation_hash":
        graph.observations[0].text_hash = "sha256:wrong"
    elif fault == "evidence_hash":
        graph.nodes[0].text_hash = "sha1:wrong"
    elif fault == "span":
        graph.nodes[0].span = (0, len(EXCERPT))
    elif fault == "source":
        graph.observations[0].source = "https://other.example.org"
    else:
        graph.nodes[0].status = EvidenceStatus.SUPERSEDED
    invoke = ScriptedInvoke()

    stats = run(store, invoke)

    assert stats["attempted"] == stats["accepted"] == 0
    assert invoke.prompts == []


@pytest.mark.parametrize("change", ["fact", "status", "observation"])
def test_atomic_commit_rechecks_fact_and_original_observation_version(change):
    store = make_store()

    def mutate_before_verification_returns():
        def mutate(state):
            if change == "fact":
                state.evidence_graph.nodes[0].finding = "Changed while metadata was being extracted"
            elif change == "status":
                state.evidence_graph.nodes[0].status = EvidenceStatus.REJECTED
            else:
                # Keep the cited span, source and hashes valid, but change the
                # observation behind its original ID. The old run must not win.
                observation = state.evidence_graph.observations[0]
                observation.text += "\nA new source version"
                observation.text_hash = "sha256:" + hashlib.sha256(
                    observation.text.encode()).hexdigest()
            return state
        store.atomic_update_state(mutate)
        return [verdict()]

    stats = run(store, ScriptedInvoke([candidate()], mutate_before_verification_returns))

    assert stats["accepted"] == 0
    assert stats["stale"] == 1
    assert store.state.evidence_graph.claim_annotations == {}


@pytest.mark.parametrize("stage", ["extraction", "verification"])
def test_invoke_failure_keeps_base_evidence_and_does_not_write_verified_metadata(stage):
    store = make_store()
    original = store.state.model_dump()
    responses = [RuntimeError("Model unavailable")]
    if stage == "verification":
        responses.insert(0, [candidate()])

    stats = run(store, ScriptedInvoke(*responses))

    assert stats["failed_batches"] == 1
    assert stats["accepted"] == 0
    assert store.state.model_dump() == original


def test_exhausted_invoke_returning_empty_string_does_not_count_as_success():
    store = make_store()

    stats = run(store, ScriptedInvoke([candidate()], ""))

    assert stats["accepted"] == 0
    assert stats["failed_batches"] == 1


@pytest.mark.parametrize("stage", ["extraction", "verification"])
def test_async_cancellation_propagates_without_retracting_committed_facts(stage):
    store = make_store()
    responses = [asyncio.CancelledError()]
    if stage == "verification":
        responses.insert(0, [candidate()])

    with pytest.raises(asyncio.CancelledError):
        run(store, ScriptedInvoke(*responses))

    assert store.state.evidence_graph.nodes[0].status == EvidenceStatus.ACTIVE
    assert store.state.evidence_graph.claim_annotations == {}


def test_already_verified_same_evidence_version_does_not_consume_more_model_calls():
    store = make_store()
    run(store, ScriptedInvoke([candidate()], [verdict()]))
    no_calls = ScriptedInvoke()

    stats = run(store, no_calls, ids=["ev-0", "ev-0"])

    assert stats["requested"] == 1
    assert stats["attempted"] == stats["accepted"] == 0
    assert no_calls.prompts == []


def test_batching_bounds_item_count_and_never_expands_to_full_observation():
    store = make_store(MAX_BATCH_ITEMS + 1)
    prompts = []

    async def invoke(prompt):
        prompts.append(prompt)
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        assert len(payload["evidence"]) <= MAX_BATCH_ITEMS
        assert all(item["source_excerpt"] == EXCERPT for item in payload["evidence"])
        assert "Heading" not in prompt and "Footer" not in prompt
        if prompt.startswith("P1_CLAIM_EXTRACT"):
            return json.dumps([candidate(item["evidence_id"]) for item in payload["evidence"]])
        assert len(json.dumps(payload, ensure_ascii=False)) <= MAX_BATCH_CHARS
        return json.dumps([verdict(item["evidence_id"]) for item in payload["candidates"]])

    stats = run(store, invoke)

    assert stats["accepted"] == MAX_BATCH_ITEMS + 1
    assert sum(prompt.startswith("P1_CLAIM_EXTRACT") for prompt in prompts) == 2
    assert all("不可信" in prompt and "不执行" in prompt for prompt in prompts)


def test_overlong_excerpt_is_skipped_without_truncating_conditions_or_changing_fact():
    store = make_store(excerpt=EXCERPT + "x" * MAX_EXCERPT_CHARS)
    original = store.state.evidence_graph.nodes[0].source_excerpt
    invoke = ScriptedInvoke()

    stats = run(store, invoke)

    assert stats["attempted"] == 0
    assert store.state.evidence_graph.nodes[0].source_excerpt == original


def test_large_candidate_cannot_expand_independent_verification_beyond_character_budget():
    store = make_store()
    invoke = ScriptedInvoke([candidate(value="x" * MAX_BATCH_CHARS)])

    stats = run(store, invoke)

    assert stats["accepted"] == 0
    assert len(invoke.prompts) == 1


def test_character_budget_splits_batches_below_item_limit_for_both_independent_calls():
    store = make_store(4)
    for node in store.state.evidence_graph.nodes:
        node.finding += " contextual qualifiers" * 100
    extraction_batches = []

    async def invoke(prompt):
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        assert len(json.dumps(payload, ensure_ascii=False)) <= MAX_BATCH_CHARS
        if prompt.startswith("P1_CLAIM_EXTRACT"):
            extraction_batches.append(len(payload["evidence"]))
            return json.dumps([candidate(item["evidence_id"]) for item in payload["evidence"]])
        return json.dumps([verdict(item["evidence_id"]) for item in payload["candidates"]])

    stats = run(store, invoke)

    assert len(extraction_batches) > 1
    assert max(extraction_batches) < MAX_BATCH_ITEMS
    assert stats["accepted"] == 4


@pytest.mark.parametrize("p1_enabled", [False, True])
def test_annotation_commit_invalidates_old_comparison_only_under_saved_p1_policy(p1_enabled):
    from searchos.socm.conflicts import cell_pairs, reconcile_conflicts
    from searchos.socm.evidence import EvidenceComparison
    from tests.test_p1_conflict_preflight import KEY, make_state

    state = make_state()
    state.evidence_graph.claim_annotations.pop("e0")
    state.coverage_map.p1_enabled = p1_enabled
    pair, = cell_pairs(state, KEY)
    state.coverage_map.evidence_comparisons[KEY] = {pair.fingerprint: EvidenceComparison(
        left_id=pair.left.id, right_id=pair.right.id, verdict="contradictory", verified=True,
        reason="Earlier semantic verdict before temporal annotation.",
    )}
    reconcile_conflicts(state, {KEY})
    assert state.coverage_map.cells[KEY].conflict_status == "confirmed"
    store = InMemoryEvidenceStore(state)
    row = candidate("e0", scope_key="head office CEO", valid_time={
        "start": "2024-01-01", "end": "2024-12-31", "basis_quote": "2024-01-01 to 2024-12-31",
    })

    stats = run(store, ScriptedInvoke([row], [verdict("e0")]), ids=["e0"])

    assert stats["accepted"] == 1
    assert store.state.coverage_map.p1_enabled is p1_enabled
    assert store.state.coverage_map.cells[KEY].conflict_status == (
        "pending" if p1_enabled else "confirmed")
    assert pair.fingerprint in store.state.coverage_map.evidence_comparisons[KEY]


@pytest.mark.parametrize("responses, expected", [
    (([],), {"extraction_missing": 1}),
    (([candidate(), candidate()],), {"extraction_missing": 1}),
    (([candidate(entity="")],), {"candidate_invalid": 1}),
    (([candidate()], []), {"candidates": 1, "verification_attempted": 1,
                           "verification_missing": 1}),
    (([candidate()], [verdict(reason="")]), {"verification_invalid": 1}),
    (([candidate()], [verdict(metadata_supported=False)]), {"verification_rejected": 1}),
    (([candidate(value="x" * MAX_BATCH_CHARS)],), {"verification_budget_skipped": 1}),
    ((RuntimeError("private model failure detail"),), {"extraction_failed_batches": 1}),
    (([candidate()], RuntimeError("private model failure detail")),
     {"verification_failed_batches": 1}),
])
def test_count_only_diagnostics_distinguish_empty_invalid_rejected_and_failed_stages(
    responses, expected,
):
    store = make_store()
    original = store.state.model_dump()

    stats = run(store, ScriptedInvoke(*responses))

    assert stats["accepted"] == 0
    assert stats["skipped"] == 1
    assert all(type(value) is int and value >= 0 for value in stats.values())
    for name, count in expected.items():
        assert stats[name] == count
    assert store.state.model_dump() == original


def test_complete_finding_can_enrich_empty_structured_fields_without_changing_base_fact():
    store = make_store()
    node = store.state.evidence_graph.nodes[0]
    assert not node.entity and not node.attribute and not node.value
    original = node.model_dump()
    invoke = ScriptedInvoke([candidate()], [verdict(time_supported=False)])

    stats = run(store, invoke)

    assert stats["candidates"] == stats["verification_attempted"] == stats["accepted"] == 1
    assert stats["time_rejected"] == stats["unknown"] == 1
    assert "并不否定完整的 finding" in invoke.prompts[0]
    assert store.state.evidence_graph.nodes[0].model_dump() == original
    assert store.state.evidence_graph.claim_annotations["ev-0"].verified
