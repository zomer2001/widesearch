#!/usr/bin/env python3
"""Exercise the real P1 extraction pipeline with synthetic, fixed model outputs.

No model is loaded, no model service is called, and no source URL is fetched.
Only model responses are fixtures: evidence admission, source observations,
annotation validation, conflict review, and workspace persistence use SearchOS.
This demonstrates integration, not model accuracy or revision/recovery behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from searchos.harness.middleware.extraction import (  # noqa: E402
    DeliveryMode,
    EvidenceObservation,
    ExtractionConfig,
    ExtractionCoordinator,
    WorkspaceEvidenceStore,
)
from searchos.socm import SearchState  # noqa: E402
from searchos.socm.claim_annotations import evidence_fingerprint  # noqa: E402
from searchos.socm.conflicts import cell_pairs  # noqa: E402
from searchos.socm.coverage import project_cell_evidence  # noqa: E402
from searchos.socm.workspace import WorkspaceManager  # noqa: E402

ENTITY = "Example Research Institute (synthetic)"
TABLE = "leadership"
CELL_KEY = f"{TABLE}/{ENTITY}.Leader"
SCOPE = "institute-wide leadership"


@dataclass(frozen=True)
class Fixture:
    key: str
    leader: str
    excerpt: str
    start: str | None = None
    end: str | None = None

    @property
    def source(self) -> str:
        return f"https://example.invalid/p1-pipeline/{self.key}"

    @property
    def metadata(self) -> None:
        # Web fixtures have no local-document identity. Synthetic provenance
        # is explicit in their text and in the result report instead.
        return None


FIXTURES = (
    Fixture(
        "leader-2024",
        "Alice",
        f"[Synthetic fixture] {ENTITY} had Alice as its leader from 2024-01-01 to 2024-12-31.",
        "2024-01-01",
        "2024-12-31",
    ),
    Fixture(
        "leader-2025",
        "Bob",
        f"[Synthetic fixture] {ENTITY} had Bob as its leader from 2025-01-01 to 2025-12-31.",
        "2025-01-01",
        "2025-12-31",
    ),
    Fixture(
        "leader-undated",
        "Casey",
        f"[Synthetic fixture] {ENTITY} lists Casey as its leader; "
        "this excerpt does not state the effective dates.",
    ),
)


class FixedFixtureModel:
    """In-process fake at the model boundary; deliberately accepts known prompts only."""

    def __init__(self, store: WorkspaceEvidenceStore) -> None:
        self.store = store
        self.calls: list[str] = []
        self.semantic_pairs: list[list[str]] = []
        self.before_annotations: dict[str, dict] = {}
        self.by_source = {fixture.source: fixture for fixture in FIXTURES}

    async def ainvoke(self, prompt: str) -> SimpleNamespace:
        if prompt.startswith("P1_CLAIM_EXTRACT"):
            self.calls.append("claim_extract")
            payload = json.loads(prompt.rsplit("\n", 1)[1])
            state = self.store.load_state()
            rows = []
            for item in payload["evidence"]:
                fixture = self.by_source[item["source"]]
                node = state.evidence_graph.get_node(item["evidence_id"])
                assert node is not None
                self.before_annotations.setdefault(node.id, node.model_dump(mode="json"))
                rows.append(
                    {
                        "evidence_id": item["evidence_id"],
                        # schema_facts keeps these fields on the cell binding,
                        # not the original node. The fixture reads the source.
                        "entity": ENTITY,
                        "attribute": "Leader",
                        "value": fixture.leader,
                        "scope_key": SCOPE,
                        "valid_time": {
                            "start": fixture.start,
                            "end": fixture.end,
                            "basis_quote": fixture.excerpt,
                        }
                        if fixture.start
                        else None,
                    }
                )
        elif prompt.startswith("P1_CLAIM_VERIFY"):
            self.calls.append("claim_verify")
            payload = json.loads(prompt.rsplit("\n", 1)[1])
            rows = [
                {
                    "evidence_id": item["evidence_id"],
                    "metadata_supported": True,
                    "time_supported": item["valid_time"] is not None,
                    "scope_is_non_temporal": True,
                    "reason": "Fixed synthetic verification: metadata is supported by the fixture.",
                }
                for item in payload["candidates"]
            ]
        elif "\nINPUT_JSON:\n" in prompt:
            self.calls.append("semantic_comparison")
            payload = json.loads(prompt.split("\nINPUT_JSON:\n", 1)[1])
            rows = []
            for item in payload:
                self.semantic_pairs.append(sorted([item["a"]["source"], item["b"]["source"]]))
                rows.append(
                    {
                        "id": item["id"],
                        "verdict": "uncertain",
                        "reason": "Fixed synthetic fallback output; no winner is selected.",
                        "supported_a": True,
                        "supported_b": True,
                        "same_scope": True,
                        "scope": SCOPE,
                        "quote_a": item["a"]["excerpt"],
                        "quote_b": item["b"]["excerpt"],
                    }
                )
        elif prompt.startswith("You are a structured data extractor."):
            self.calls.append("row_extraction")
            pages = re.findall(r"^### Page (\d+)\nSource: ([^\n]+)", prompt, re.MULTILINE)
            if not pages:
                raise AssertionError("No fixture source in the actual extraction prompt")
            rows = []
            for number, source in pages:
                fixture = self.by_source[source]
                rows.append(
                    {
                        "Organization": ENTITY,
                        "Leader": fixture.leader,
                        "_source_page": int(number),
                        "_source_excerpt": fixture.excerpt,
                        "_alignment": "full",
                        "_confidence": "high",
                    }
                )
        else:
            raise AssertionError("Unexpected prompt: the demo must use only known fixture outputs")
        return SimpleNamespace(content=json.dumps(rows, ensure_ascii=False))


async def run_demo(
    output_root: Path,
    *,
    policy: str = "schema_facts",
    p1_enabled: bool = True,
) -> dict:
    """Create a fresh workspace under output_root; never reuse or delete prior runs."""
    if policy not in {"schema_facts", "task_rows"}:
        raise ValueError("policy must be schema_facts or task_rows")
    workspace = WorkspaceManager(output_root.resolve())
    run_path = workspace.create()
    state = SearchState(
        intent="Collect all synthetic institute leadership assertions and dates.",
        source_scope={"mode": "web", "documents": []},
    )
    state.coverage_map.initialize(
        entities=[ENTITY],
        attributes=["Organization", "Leader"],
        table_id=TABLE,
        primary_key=["Organization"],
    )
    workspace.save_state(state)
    store = WorkspaceEvidenceStore(workspace)
    model = FixedFixtureModel(store)
    coordinator = ExtractionCoordinator(
        model,
        store=store,
        policy=policy,
        config=ExtractionConfig(
            dual_mode=False,
            cross_table_backfill=False,
            p1_evidence_reasoning=p1_enabled,
        ),
    )
    receipt_ids = []
    for fixture in FIXTURES:
        receipt = await coordinator.submit(
            EvidenceObservation(
                content=fixture.excerpt,
                source_url=fixture.source,
                page_id=fixture.key,
                target_table=TABLE,
                local_source_metadata=fixture.metadata,
            ),
            delivery=DeliveryMode.SYNC,
        )
        if not receipt.accepted or len(receipt.committed_node_ids) != 1:
            raise AssertionError(f"Fixture was not committed once: {fixture.key}; {receipt}")
        receipt_ids.extend(receipt.committed_node_ids)
    await coordinator.finalize()

    # A second manager ensures all validation below reads the persisted workspace.
    reloaded = WorkspaceManager(run_path.parent, run_path.name).load_state()
    graph = reloaded.evidence_graph
    nodes = {node.source: node for node in graph.nodes}
    observations = {observation.id: observation for observation in graph.observations}
    current_pairs = cell_pairs(reloaded, CELL_KEY)
    comparisons = []
    for pair in current_pairs:
        decision = reloaded.coverage_map.evidence_comparisons.get(CELL_KEY, {}).get(
            pair.fingerprint
        )
        if decision is None:
            raise AssertionError("Real conflict review did not persist a current comparison")
        comparisons.append(
            {
                "sources": sorted([pair.left.source, pair.right.source]),
                **decision.model_dump(mode="json"),
            }
        )

    checks = {
        "three_facts_committed_by_real_coordinator": len(graph.nodes) == len(set(receipt_ids)) == 3,
        "sources_and_metadata_preserved": len(nodes) == 3
        and all(
            nodes[f.source].source_excerpt == f.excerpt
            and nodes[f.source].local_source_metadata == f.metadata
            for f in FIXTURES
        ),
        "all_values_remain_on_their_original_cell_projections": len(nodes) == 3
        and all(
            (
                projection := project_cell_evidence(
                    reloaded.coverage_map,
                    nodes[f.source],
                    TABLE,
                    ENTITY,
                    "Leader",
                )
            )
            is not None
            and projection.value == f.leader
            for f in FIXTURES
        ),
        "all_evidence_remains_active_and_supported": (
            all(node.status.value == "active" for node in graph.nodes)
            and set(reloaded.coverage_map.cells[CELL_KEY].supporting_evidence_ids)
            == set(receipt_ids)
        ),
        "all_three_pairs_persisted": len(comparisons) == 3,
        "json_roundtrip": (
            SearchState.model_validate_json(reloaded.model_dump_json()).model_dump(mode="json")
            == reloaded.model_dump(mode="json")
        ),
        "new_manager_reads_same_workspace": reloaded.model_dump(mode="json")
        == store.load_state().model_dump(mode="json"),
    }
    if p1_enabled:
        known_sources = sorted([f.source for f in FIXTURES[:2]])
        known = [decision for decision in comparisons if decision["sources"] == known_sources]
        fallback = [
            decision for decision in comparisons if FIXTURES[2].source in decision["sources"]
        ]
        checks.update(
            {
                "original_observations_retained": len(observations) == 3
                and all(
                    (observation := observations.get(nodes[f.source].observation_id)) is not None
                    and observation.source == f.source
                    and observation.text == f.excerpt
                    and observation.local_source_metadata == f.metadata
                    and observation.text_hash
                    == "sha256:" + hashlib.sha256(f.excerpt.encode()).hexdigest()
                    and nodes[f.source].text_hash
                    in {
                        algorithm + ":" + hashlib.new(algorithm, f.excerpt.encode()).hexdigest()
                        for algorithm in ("sha1", "sha256")
                    }
                    and nodes[f.source].span is not None
                    and observation.text[slice(*nodes[f.source].span)] == f.excerpt
                    for f in FIXTURES
                ),
                "fresh_annotations_with_anchored_dates": len(graph.claim_annotations) == 3
                and all(
                    (annotation := graph.claim_annotations.get(nodes[f.source].id)) is not None
                    and annotation.verified
                    and annotation.evidence_fingerprint == evidence_fingerprint(nodes[f.source])
                    and annotation.scope_key == SCOPE
                    and (
                        annotation.valid_time is None
                        if f.start is None
                        else (
                            annotation.valid_time is not None
                            and annotation.valid_time.start.isoformat() == f.start
                            and annotation.valid_time.end.isoformat() == f.end
                            and annotation.valid_time.basis_quote == f.excerpt
                        )
                    )
                    for f in FIXTURES
                ),
                "disjoint_years_compatible_without_semantic_call": len(known) == 1
                and (
                    known[0]["verdict"] == "compatible"
                    and known[0]["verified"]
                    and known[0]["method"] == "p1_rule"
                    and known[0]["rule"] == "P1-DISJOINT"
                    and known[0]["temporal_relation"] in {"before", "after"}
                    and known_sources not in model.semantic_pairs
                ),
                "unknown_dates_use_fixed_semantic_fallback": len(fallback) == 2
                and all(
                    item["method"] == "llm"
                    and item["verdict"] == "uncertain"
                    and item["sources"] in model.semantic_pairs
                    for item in fallback
                ),
                "annotation_does_not_rewrite_base_evidence": len(model.before_annotations) == 3
                and all(
                    node.model_dump(mode="json") == model.before_annotations[node.id]
                    for node in graph.nodes
                ),
            }
        )
    else:
        checks.update(
            {
                "disabled_adds_no_annotation_calls": not graph.claim_annotations
                and not any(call in {"claim_extract", "claim_verify"} for call in model.calls),
                "disabled_preserves_semantic_review": all(
                    item["method"] == "llm" and item["sources"] in model.semantic_pairs
                    for item in comparisons
                ),
            }
        )

    result = {
        "demo": "synthetic fixtures and fixed model outputs; real SearchOS extraction pipeline",
        "scope": (
            "Integration and persistence only; no model accuracy evaluation or revision recovery."
        ),
        "policy": policy,
        "p1_enabled": p1_enabled,
        "workspace": str(run_path),
        "state_file": str(run_path / "search_state.json"),
        "model_calls": (
            "No model service calls; the counts below are in-process fixture invocations."
        ),
        "fixture_call_counts": dict(Counter(model.calls)),
        "counts": {
            "facts": len(graph.nodes),
            "observations": len(graph.observations),
            "annotations": len(graph.claim_annotations),
            "comparisons": len(comparisons),
        },
        "checks": checks,
        "passed": all(checks.values()),
        "comparisons": comparisons,
        "annotations": {
            key: value.model_dump(mode="json") for key, value in graph.claim_annotations.items()
        },
    }
    result_path = run_path / "result.json"
    result["result_file"] = str(result_path)
    result_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO / "artifacts" / "p1-pipeline-demo")
    parser.add_argument("--policy", choices=("schema_facts", "task_rows"), default="schema_facts")
    parser.add_argument(
        "--disable-p1", action="store_true", help="Run the baseline with no claim enrichment"
    )
    args = parser.parse_args()
    result = asyncio.run(run_demo(args.output, policy=args.policy, p1_enabled=not args.disable_p1))
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "demo",
                    "passed",
                    "policy",
                    "p1_enabled",
                    "counts",
                    "fixture_call_counts",
                    "result_file",
                )
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
