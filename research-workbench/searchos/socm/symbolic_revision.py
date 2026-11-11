"""Experimental P1 claim rules and confirmed-revision replay.

This is an explicit, finite rule engine over an ontology-aligned projection of
SearchOS evidence. It does not run OWL DL/SWRL, decide source credibility, or
modify the live scheduler. Read sets are supplied execution dependencies, not
causal explanations inferred from an LLM's prose.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import RLock
from typing import Literal
from urllib.parse import quote

from pydantic import Field

from searchos.socm.evidence import EvidenceNode, EvidenceStatus
from searchos.socm.temporal import TemporalRelation, TemporalScope, compare_temporal_scopes
from searchos.util.base_model import CamelModel


class Claim(CamelModel):
    """A source assertion, distinct from an accepted fact about the world."""

    evidence_id: str
    entity: str
    attribute: str
    value: str
    scope_key: str
    source: str
    source_excerpt: str
    observation_id: str = ""
    temporal: TemporalScope | None = None
    single_valued: bool = False
    status: EvidenceStatus = EvidenceStatus.ACTIVE

    @classmethod
    def from_evidence(
        cls, evidence: EvidenceNode, *, scope_key: str,
        temporal: TemporalScope | None = None, single_valued: bool = False,
    ) -> Claim:
        """Project existing evidence without changing its identity or storage."""
        return cls(
            evidence_id=evidence.id, entity=evidence.entity,
            attribute=evidence.attribute, value=evidence.value or evidence.finding,
            scope_key=scope_key, source=evidence.source,
            source_excerpt=evidence.source_excerpt, observation_id=evidence.observation_id,
            temporal=temporal, single_valued=single_valued, status=evidence.status,
        )


class ClaimDecision(CamelModel):
    outcome: Literal[
        "not_comparable", "same_value", "temporal_evolution", "unknown",
        "not_exclusive", "conflict_candidate",
    ]
    rule: str
    evidence_ids: list[str]
    reason: str
    temporal_relation: TemporalRelation = TemporalRelation.UNKNOWN


def _normal(value: str) -> str:
    return " ".join(value.split()).casefold()


def compare_claims(left: Claim, right: Claim) -> ClaimDecision:
    """Classify explicit scopes; never select a winner or mutate either claim.

    Entity alignment, measurement units and scope normalization must be supplied
    upstream. A temporal quote must occur in the stored excerpt; this anchor
    check alone is not semantic validation of the extracted dates.
    """
    ids = [left.evidence_id, right.evidence_id]

    def decision(outcome, rule, reason, relation=TemporalRelation.UNKNOWN):
        return ClaimDecision(outcome=outcome, rule=rule, reason=reason,
                             evidence_ids=ids, temporal_relation=relation)

    keys = ("entity", "attribute", "scope_key")
    if any(not getattr(left, k).strip() or not getattr(right, k).strip()
           or _normal(getattr(left, k)) != _normal(getattr(right, k)) for k in keys):
        return decision("not_comparable", "P1-SCOPE", "Entities, attributes or scopes differ.")
    if left.status != EvidenceStatus.ACTIVE or right.status != EvidenceStatus.ACTIVE:
        return decision("not_comparable", "P1-ACTIVE", "Use the current evidence view.")
    if not left.value.strip() or not right.value.strip():
        return decision("unknown", "P1-VALUE", "One assertion has no explicit value.")
    if _normal(left.value) == _normal(right.value):
        return decision("same_value", "P1-EQUAL", "Values agree within the supplied scope.")
    for claim in (left, right):
        interval = claim.temporal
        if (interval is None or not interval.basis_quote.strip()
                or _normal(interval.basis_quote) not in _normal(claim.source_excerpt)):
            return decision("unknown", "P1-TIME-ANCHOR", "Time has no anchored source excerpt.")
    relation = compare_temporal_scopes(left.temporal, right.temporal)
    if relation in (TemporalRelation.BEFORE, TemporalRelation.AFTER):
        return decision("temporal_evolution", "P1-DISJOINT",
                        "Different effective periods can coexist; neither source is retracted.",
                        relation)
    if relation == TemporalRelation.UNKNOWN:
        return decision("unknown", "P1-TIME-UNKNOWN", "Unknown time is not disjoint time.")
    if not (left.single_valued and right.single_valued):
        return decision("not_exclusive", "P1-MULTIVALUE",
                        "Different values are not exclusive without a domain constraint.", relation)
    return decision("conflict_candidate", "P1-OVERLAP-EXCLUSIVE",
                    "Overlapping periods and exclusive values require source verification.", relation)


class DerivedArtifact(CamelModel):
    id: str
    kind: Literal["summary", "task", "report", "conclusion"] = "summary"
    scope_key: str
    content: str = ""
    read_evidence_ids: list[str] = Field(default_factory=list)
    read_artifact_ids: list[str] = Field(default_factory=list)
    read_artifact_epochs: dict[str, int] = Field(default_factory=dict)
    # OR between sets, AND within each set. Alternative support is not a vote.
    support_sets: list[list[str]] = Field(default_factory=list)
    status: Literal["active", "stale"] = "active"
    execution_epoch: int = 0


class ConfirmedRevision(CamelModel):
    id: str
    old_evidence_id: str
    new_evidence_id: str
    scope_key: str
    reason: str
    confirmed: bool = False


class RepairImpact(CamelModel):
    artifact_id: str
    execution_epoch: int
    surviving_support_sets: list[list[str]]


class RepairPlan(CamelModel):
    revision_id: str
    impacts: list[RepairImpact]
    unaffected_ids: list[str]
    task_ids: list[str]
    already_applied: bool = False


class SymbolicRepairReplay:
    """Isolated in-memory replay, not a live SearchOS transaction coordinator.

    Evidence is copied and preserved. A revision retires only its use in the
    specified scope. Rebuild/publish exercises a version barrier; production
    model/tool callbacks still need to be connected separately.
    """

    def __init__(self, claims: list[Claim], artifacts: list[DerivedArtifact]):
        self.claims = {c.evidence_id: c.model_copy(deep=True) for c in claims}
        self.artifacts = {a.id: a.model_copy(deep=True) for a in artifacts}
        if len(self.claims) != len(claims) or len(self.artifacts) != len(artifacts):
            raise ValueError("Claim and artifact identifiers must each be unique")
        self.retired_uses: set[tuple[str, str]] = set()
        self.revisions: dict[str, tuple[str, ConfirmedRevision, RepairPlan]] = {}
        self._lock = RLock()
        for artifact in self.artifacts.values():
            self._validate_references(artifact)
        self._validate_acyclic()
        for artifact in self.artifacts.values():
            if artifact.status == "active" and any(
                self.artifacts[aid].status != "active"
                or self.artifacts[aid].execution_epoch != artifact.read_artifact_epochs[aid]
                for aid in artifact.read_artifact_ids
            ):
                raise ValueError("Active artifacts must consume current, active parent versions")

    def _validate_references(self, artifact: DerivedArtifact) -> None:
        evidence = set(artifact.read_evidence_ids)
        evidence.update(eid for group in artifact.support_sets for eid in group)
        if not evidence <= self.claims.keys():
            raise ValueError("Unknown evidence in dependency record")
        if not set(artifact.read_artifact_ids) <= self.artifacts.keys():
            raise ValueError("Unknown artifact in dependency record")
        if artifact.id in artifact.read_artifact_ids:
            raise ValueError("An artifact cannot consume itself")
        if set(artifact.read_artifact_epochs) != set(artifact.read_artifact_ids):
            raise ValueError("Every consumed artifact needs its observed execution epoch")
        if artifact.execution_epoch < 0 or any(v < 0 for v in artifact.read_artifact_epochs.values()):
            raise ValueError("Execution epochs must be non-negative")
        if any(not group for group in artifact.support_sets):
            raise ValueError("Empty support sets are not evidence")

    def _validate_acyclic(self, replacement: DerivedArtifact | None = None) -> None:
        """This prototype supports a DAG, not jointly rebuilt cyclic components."""
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(artifact_id: str) -> None:
            if artifact_id in visiting:
                raise ValueError("Artifact dependencies must be acyclic")
            if artifact_id in visited:
                return
            visiting.add(artifact_id)
            artifact = (replacement if replacement and replacement.id == artifact_id
                        else self.artifacts[artifact_id])
            for parent in artifact.read_artifact_ids:
                visit(parent)
            visiting.remove(artifact_id)
            visited.add(artifact_id)

        for artifact_id in self.artifacts:
            visit(artifact_id)

    def evidence_is_usable(self, evidence_id: str, scope_key: str) -> bool:
        claim = self.claims.get(evidence_id)
        return bool(claim and claim.status == EvidenceStatus.ACTIVE
                    and (evidence_id, scope_key) not in self.retired_uses)

    def apply(self, revision: ConfirmedRevision) -> RepairPlan:
        with self._lock:
            payload = revision.model_dump(mode="json")
            fingerprint = hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            old_event = self.revisions.get(revision.id)
            if old_event is not None:
                if fingerprint != old_event[0]:
                    raise ValueError("Revision ID reused with different contents")
                return old_event[2].model_copy(deep=True, update={"already_applied": True})
            if not revision.confirmed or not all(
                value.strip() for value in (revision.id, revision.scope_key, revision.reason)
            ):
                raise ValueError("A scoped, explicitly confirmed revision with a reason is required")
            old, new = revision.old_evidence_id, revision.new_evidence_id
            if old == new or not all(self.evidence_is_usable(e, revision.scope_key)
                                    for e in (old, new)):
                raise ValueError("Revision requires two distinct, currently usable evidence IDs")
            # Claim scope describes the proposition; revision scope describes
            # its particular downstream use. Do not conflate these two scopes.
            if any(not getattr(self.claims[old], key).strip()
                   or _normal(getattr(self.claims[old], key))
                   != _normal(getattr(self.claims[new], key))
                   for key in ("entity", "attribute", "scope_key")):
                raise ValueError("Replacement claims must share entity, attribute and fact scope")

            affected = {
                a.id for a in self.artifacts.values() if a.scope_key == revision.scope_key
                and (old in a.read_evidence_ids or any(old in group for group in a.support_sets))
            }
            while True:
                consumers = {a.id for a in self.artifacts.values()
                             if affected.intersection(a.read_artifact_ids)}
                if consumers <= affected:
                    break
                affected.update(consumers)

            self.retired_uses.add((old, revision.scope_key))
            impacts = []
            for artifact_id in sorted(affected):
                artifact = self.artifacts[artifact_id]
                artifact.status = "stale"  # Recheck, not a declaration that the conclusion is false.
                artifact.execution_epoch += 1
                supports = [group[:] for group in artifact.support_sets
                            if all(self.evidence_is_usable(eid, artifact.scope_key)
                                   for eid in group)]
                impacts.append(RepairImpact(artifact_id=artifact_id,
                                            execution_epoch=artifact.execution_epoch,
                                            surviving_support_sets=supports))
            plan = RepairPlan(
                revision_id=revision.id, impacts=impacts,
                unaffected_ids=sorted(self.artifacts.keys() - affected),
                task_ids=sorted(aid for aid in affected if self.artifacts[aid].kind == "task"),
            )
            self.revisions[revision.id] = (fingerprint, revision.model_copy(deep=True),
                                           plan.model_copy(deep=True))
            return plan

    def publish(self, artifact: DerivedArtifact, *, expected_epoch: int) -> bool:
        """Publish rebuilt output only against its current version and usable inputs."""
        with self._lock:
            current = self.artifacts.get(artifact.id)
            if current is None or current.execution_epoch != expected_epoch:
                return False
            if current.scope_key != artifact.scope_key or current.kind != artifact.kind:
                raise ValueError("A rebuild cannot change the artifact identity or scope")
            self._validate_references(artifact)
            self._validate_acyclic(replacement=artifact)
            if any(not self.evidence_is_usable(eid, artifact.scope_key)
                   for eid in artifact.read_evidence_ids):
                return False
            if any(self.artifacts[aid].status != "active"
                   or self.artifacts[aid].execution_epoch != artifact.read_artifact_epochs[aid]
                   for aid in artifact.read_artifact_ids):
                return False
            if artifact.support_sets and not any(
                all(self.evidence_is_usable(eid, artifact.scope_key) for eid in group)
                for group in artifact.support_sets
            ):
                return False
            published = artifact.model_copy(
                deep=True, update={"execution_epoch": expected_epoch, "status": "active"}
            )
            if current.status == "active":
                # An epoch identifies immutable published content and inputs.
                # Repeated delivery is safe only when it is identical.
                return published == current
            self.artifacts[artifact.id] = published
            return True

    def active_context(self) -> dict[str, str]:
        with self._lock:
            return {a.id: a.content for a in self.artifacts.values() if a.status == "active"}

    def to_rdf(self):
        """Export auditable claims, inputs and revisions using the P1 vocabulary.

        RDFLib is an optional research dependency. No remote ontology is loaded.
        RDF is a projection of replay state, not a second authoritative store.
        """
        from rdflib import Graph, Literal as RDFLiteral, Namespace, RDF, XSD

        so = Namespace("urn:searchos:p1:")
        prov = Namespace("http://www.w3.org/ns/prov#")
        time = Namespace("http://www.w3.org/2006/time#")
        graph = Graph().parse(Path(__file__).parent / "ontology" / "p1.ttl", format="turtle")

        def ref(kind, identity):
            return so[f"{kind}/{quote(identity, safe='')}"]

        for claim in self.claims.values():
            node = ref("claim", claim.evidence_id)
            graph.add((node, RDF.type, so.Claim))
            for name, value in (("evidenceId", claim.evidence_id), ("entity", claim.entity),
                                ("attribute", claim.attribute), ("value", claim.value),
                                ("scopeKey", claim.scope_key),
                                ("sourceExcerpt", claim.source_excerpt)):
                graph.add((node, so[name], RDFLiteral(value)))
            graph.add((node, so.status, RDFLiteral(claim.status.value)))
            # A missing original observation ID gets a separate excerpt observation,
            # never a fabricated ID for the original whole reading window.
            observation_id = claim.observation_id or "excerpt-" + hashlib.sha256(
                json.dumps([claim.source, claim.source_excerpt]).encode()
            ).hexdigest()[:24]
            observation = ref("observation", observation_id)
            graph.add((observation, RDF.type, so.SourceObservation))
            graph.add((observation, so.sourceReference, RDFLiteral(claim.source)))
            graph.add((node, so.source, observation))
            if claim.temporal:
                interval = ref("interval", claim.evidence_id)
                graph.add((node, so.validDuring, interval))
                graph.add((interval, RDF.type, time.ProperInterval))
                for name, value in (("startDate", claim.temporal.start),
                                    ("endDate", claim.temporal.end)):
                    if value is not None:
                        graph.add((interval, so[name], RDFLiteral(value, datatype=XSD.date)))
        for artifact in self.artifacts.values():
            node = ref("artifact", f"{artifact.id}:{artifact.execution_epoch}")
            execution = ref("execution", f"{artifact.id}:{artifact.execution_epoch}")
            graph.add((node, RDF.type, so.DerivedArtifact))
            graph.add((execution, RDF.type, so.Execution))
            graph.add((node, prov.wasGeneratedBy, execution))
            graph.add((node, so.scopeKey, RDFLiteral(artifact.scope_key)))
            graph.add((node, so.status, RDFLiteral(artifact.status)))
            graph.add((execution, so.executionEpoch,
                       RDFLiteral(artifact.execution_epoch, datatype=XSD.nonNegativeInteger)))
            for eid in artifact.read_evidence_ids:
                graph.add((execution, so.usedEvidence, ref("claim", eid)))
            for aid in artifact.read_artifact_ids:
                epoch = artifact.read_artifact_epochs[aid]
                graph.add((execution, so.usedArtifact, ref("artifact", f"{aid}:{epoch}")))
            for index, group in enumerate(artifact.support_sets):
                support = ref("support", f"{artifact.id}:{artifact.execution_epoch}:{index}")
                graph.add((support, RDF.type, so.SupportSet))
                graph.add((node, so.supportAlternative, support))
                for eid in group:
                    graph.add((support, so.memberEvidence, ref("claim", eid)))
        for eid, scope in sorted(self.retired_uses):
            binding = ref("use", json.dumps([eid, scope]))
            graph.add((binding, RDF.type, so.EvidenceUse))
            graph.add((binding, so.usedClaim, ref("claim", eid)))
            graph.add((binding, so.scopeKey, RDFLiteral(scope)))
            graph.add((binding, so.status, RDFLiteral("retired")))
        for _, revision, plan in self.revisions.values():
            node = ref("revision", revision.id)
            graph.add((node, RDF.type, so.RevisionEvent))
            graph.add((node, so.scopeKey, RDFLiteral(revision.scope_key)))
            graph.add((node, so.revisionReason, RDFLiteral(revision.reason)))
            graph.add((node, so.oldEvidence, ref("claim", revision.old_evidence_id)))
            graph.add((node, so.newEvidence, ref("claim", revision.new_evidence_id)))
            graph.add((node, prov.used, ref("claim", revision.old_evidence_id)))
            graph.add((node, prov.used, ref("claim", revision.new_evidence_id)))
            for impact in plan.impacts:
                affected = ref("artifact", f"{impact.artifact_id}:{impact.execution_epoch}")
                graph.add((affected, prov.wasInfluencedBy, node))
        return graph
