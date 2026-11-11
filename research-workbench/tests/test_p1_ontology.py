"""Offline RDF contracts for sourced claims, temporal scope, and repair provenance."""

from datetime import date
from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.compare import isomorphic
from rdflib.namespace import OWL, RDF, RDFS, XSD

P1 = Namespace("urn:searchos:p1:")
PROV = Namespace("http://www.w3.org/ns/prov#")
TIME = Namespace("http://www.w3.org/2006/time#")
EX = Namespace("urn:searchos:p1:example:")
VOCABULARY = Path(__file__).resolve().parents[1] / "searchos/socm/ontology/p1.ttl"

EXAMPLE = """
@prefix p1: <urn:searchos:p1:> .
@prefix ex: <urn:searchos:p1:example:> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix time: <http://www.w3.org/2006/time#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ex:sourceA a p1:SourceObservation .
ex:sourceB a p1:SourceObservation .
ex:day a time:ProperInterval ;
    p1:startDate "2026-10-23"^^xsd:date ;
    p1:endDate "2026-10-23"^^xsd:date .
ex:claimA a p1:Claim ; p1:evidenceId "ev-a" ; p1:entity "Acme" ;
    p1:attribute "CEO" ; p1:value "Alice" ; p1:scopeKey "company" ;
    p1:source ex:sourceA ; p1:validDuring ex:day ; p1:status "disputed" .
ex:claimB a p1:Claim ; p1:evidenceId "ev-b" ; p1:entity "Acme" ;
    p1:attribute "CEO" ; p1:value "Bob" ; p1:scopeKey "company" ;
    p1:source ex:sourceB ; p1:validDuring ex:day ; p1:status "disputed" .
ex:previousSummary a p1:DerivedArtifact ;
    p1:supportedBy ex:claimA ; p1:status "stale" .
ex:execution a p1:Execution ; p1:usedEvidence ex:claimB ;
    p1:usedArtifact ex:previousSummary ; p1:executionEpoch "2"^^xsd:nonNegativeInteger .
ex:revision a p1:RevisionEvent ; p1:status "prepared" ;
    p1:revisionReason "Competing sources require verification before replacement." .
"""


@pytest.fixture
def vocabulary():
    return Graph().parse(VOCABULARY, format="turtle")


def test_claims_are_sourced_assertions_not_functional_entity_truth(vocabulary):
    """Competing assertions remain distinct and keep their individual provenance."""
    assert (P1.Claim, RDFS.subClassOf, PROV.Entity) in vocabulary
    assert (P1.entity, RDFS.domain, P1.Claim) in vocabulary
    assert (P1.entity, RDFS.range, XSD.string) in vocabulary
    assert (P1.value, RDFS.domain, P1.Claim) in vocabulary
    assert not list(vocabulary.subjects(RDF.type, OWL.FunctionalProperty))
    assert not list(vocabulary.subjects(RDF.type, OWL.InverseFunctionalProperty))
    assert not list(vocabulary.triples((None, OWL.sameAs, None)))
    assert not list(vocabulary.triples((None, OWL.hasKey, None)))

    graph = Graph().parse(data=EXAMPLE, format="turtle")
    assert set(graph.subjects(RDF.type, P1.Claim)) == {EX.claimA, EX.claimB}
    assert set(graph.objects(None, P1.entity)) == {Literal("Acme")}
    assert set(graph.objects(None, P1.value)) == {Literal("Alice"), Literal("Bob")}
    assert graph.value(EX.claimA, P1.source) == EX.sourceA
    assert graph.value(EX.claimB, P1.source) == EX.sourceB


def test_provenance_distinguishes_consumption_from_evidence_support(vocabulary):
    """Execution inputs and artifact support point to the right kinds of nodes."""
    for property_, domain, range_, parent in (
        (P1.source, P1.Claim, P1.SourceObservation, PROV.wasDerivedFrom),
        (P1.usedEvidence, P1.Execution, P1.Claim, PROV.used),
        (P1.usedArtifact, P1.Execution, P1.DerivedArtifact, PROV.used),
        (P1.supportedBy, P1.DerivedArtifact, P1.Claim, PROV.wasDerivedFrom),
    ):
        assert (property_, RDF.type, OWL.ObjectProperty) in vocabulary
        assert (property_, RDFS.domain, domain) in vocabulary
        assert (property_, RDFS.range, range_) in vocabulary
        assert (property_, RDFS.subPropertyOf, parent) in vocabulary
    for class_ in (P1.SourceObservation, P1.DerivedArtifact):
        assert (class_, RDFS.subClassOf, PROV.Entity) in vocabulary
    for class_ in (P1.Execution, P1.RevisionEvent):
        assert (class_, RDFS.subClassOf, PROV.Activity) in vocabulary
    assert (P1.executionEpoch, RDFS.domain, P1.Execution) in vocabulary
    assert (P1.executionEpoch, RDFS.range, XSD.nonNegativeInteger) in vocabulary
    assert (P1.revisionReason, RDFS.domain, P1.RevisionEvent) in vocabulary
    assert (P1.revisionReason, RDFS.range, XSD.string) in vocabulary


def test_closed_calendar_dates_are_not_time_instants(vocabulary):
    assert (P1.validDuring, RDFS.domain, P1.Claim) in vocabulary
    assert (P1.validDuring, RDFS.range, TIME.ProperInterval) in vocabulary
    for property_ in (P1.startDate, P1.endDate):
        assert (property_, RDFS.domain, TIME.ProperInterval) in vocabulary
        assert (property_, RDFS.range, XSD.date) in vocabulary
        assert not list(vocabulary.triples((property_, OWL.equivalentProperty, None)))

    graph = Graph().parse(data=EXAMPLE, format="turtle")
    start = graph.value(EX.day, P1.startDate)
    end = graph.value(EX.day, P1.endDate)
    assert start.datatype == end.datatype == XSD.date
    assert start.toPython() == end.toPython() == date(2026, 10, 23)
    assert (EX.day, RDF.type, TIME.ProperInterval) in graph
    assert (EX.day, RDF.type, TIME.Instant) not in graph
    assert not list(graph.triples((None, TIME.hasBeginning, start)))


def test_example_and_vocabulary_roundtrip_preserves_repair_dependencies(vocabulary):
    """A portable RDF export retains both competing claims and stale dependencies."""
    vocabulary.parse(data=EXAMPLE, format="turtle")
    restored = Graph().parse(data=vocabulary.serialize(format="turtle"), format="turtle")
    assert isomorphic(vocabulary, restored)
    assert restored.value(EX.execution, P1.usedArtifact) == EX.previousSummary
    assert restored.value(EX.previousSummary, P1.supportedBy) == EX.claimA
    assert restored.value(EX.execution, P1.usedEvidence) == EX.claimB
    assert restored.value(EX.previousSummary, P1.status) == Literal("stale")
    assert restored.value(EX.execution, P1.executionEpoch).toPython() == 2
