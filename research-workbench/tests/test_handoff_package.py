"""Contract and package checks use only fictional sources and temporary databases."""

import hashlib
import json
import zipfile

import pytest
from api.knowledge_handoff import package
from api.knowledge_handoff.contracts import build_contract, install_contract, normalize_outputs
from api.knowledge_handoff.models import ResultEnvelope
from api.research_mapping import Source

from searchos.knowledge_store.database import ResearchDatabase


@pytest.fixture
def store(tmp_path):
    result = ResearchDatabase(tmp_path / "research.sqlite")
    result.initialize()
    return result


def fixture_output(fields):
    return [
        {
            "entities": [
                {
                    "entity_key": "p",
                    "kind": "product",
                    "name": "Fictional-X",
                    "identity_quote": "Fictional-X operates underwater.",
                    "fields": fields,
                }
            ]
        }
    ]


def test_contract_is_read_only_and_added_axes_install_idempotently(store):
    before = store.verify()["counts"]
    contract = build_contract(store)
    assert store.verify()["counts"] == before
    fields = {f["id"]: f for f in contract["fields"]}
    assert len(fields) == before["field_definitions"] + 6
    assert fields["technology_detection_pipeline"]["allowed_values"] == ["单阶段", "两阶段"]
    assert fields["technology_backbone_architectures"]["allowed_values"] is None
    assert fields["technology_route_branch"]["deprecated_for_new_extraction"]
    assert fields["product_operating_domains"]["not_to_confuse"]
    assert {f["definition_status"] for f in fields.values()} == {"reviewed", "type_template"}
    install_contract(store)
    after = store.verify()["counts"]
    assert after["field_definitions"] == len(fields)
    assert store.verify()["valid"]
    assert build_contract(store)["sha256"] == contract["sha256"]
    install_contract(store)
    assert store.verify()["counts"] == after


def test_normalization_preserves_unknowns_as_issues_and_does_not_fill_false(store):
    text = "Fictional-X operates underwater. It plans to support publish-subscribe."
    source = Source("https://example.invalid", "fiction", text, "x.md", "x")
    fields = [
        {
            "field_id": "product_operating_domains",
            "value": ["underwater"],
            "quote": "Fictional-X operates underwater.",
        },
        {
            "field_id": "product_supports_publish_subscribe",
            "value": True,
            "quote": "It plans to support publish-subscribe.",
        },
        {
            "field_id": "product_supports_night_sensing",
            "value": False,
            "quote": "Fictional-X operates underwater.",
        },
        {
            "field_id": "product_supports_data_storage",
            "value": "unknown",
            "quote": "Fictional-X operates underwater.",
        },
    ]
    result, issues = normalize_outputs(store, fixture_output(fields), source)
    accepted = result[0]["entities"][0]["fields"]
    assert len(accepted) == 1
    assert accepted[0]["value"] == ["水下"]
    assert accepted[0]["qualifiers"]["original_value"] == ["underwater"]
    assert len(issues) == 3
    assert all(i["raw"] for i in issues)
    assert fields[0]["value"] == ["underwater"]  # caller input not mutated


def test_metric_units_are_normalized_without_inventing_conversion(store):
    text = "Fictional-X operates underwater. It operates for 2 hours."
    source = Source("https://example.invalid", "fiction", text, "x.md", "x")
    fields = [
        {
            "field_id": "product_endurance",
            "value": {"value": 2, "unit": "hours", "raw": "2 hours"},
            "quote": "It operates for 2 hours.",
        }
    ]
    result, issues = normalize_outputs(store, fixture_output(fields), source)
    assert not issues
    v = result[0]["entities"][0]["fields"][0]["value"]
    assert v == {"value": 2, "unit": "h", "raw": "2 hours"}
    fields[0]["value"] = {"min": 4, "max": 2, "unit": "h"}
    result, issues = normalize_outputs(store, fixture_output(fields), source)
    assert len(issues) == 1
    assert not result[0]["entities"][0]["fields"]


def test_dates_and_closed_vocabulary_are_checked(store):
    text = "Fictional-X operates underwater. Release: 2026-02-30."
    source = Source("https://example.invalid", "fiction", text, "x.md", "x")
    fields = [
        {
            "field_id": "product_operating_domains",
            "value": ["imaginary domain"],
            "quote": "Fictional-X operates underwater.",
        },
        {
            "field_id": "product_release_date",
            "value": "2026-02-30",
            "quote": "Release: 2026-02-30.",
        },
    ]
    _, issues = normalize_outputs(store, fixture_output(fields), source)
    assert len(issues) == 2


def test_export_includes_only_selected_archived_sources_and_separates_summary(
    store, tmp_path, monkeypatch
):
    text = "Fictional-X operates underwater."
    revision = hashlib.sha256(text.encode()).hexdigest()
    data_root = tmp_path / "library"
    data_root.mkdir()
    (data_root / ".env").write_text("SECRET_MUST_NOT_BE_PACKED")

    def source(*args):
        assert args[2:] == ("doc1", revision)
        return Source(
            "https://example.invalid/fiction",
            "Fiction",
            text,
            "archived.md",
            "doc1",
            library_id="lib1",
            document_id="doc1",
            source_revision=revision,
        )

    monkeypatch.setattr(package, "library_source", source)
    output = tmp_path / "handoff"
    before = store.verify()["counts"]
    result = package.export_package(
        data_root=data_root,
        database=store.path,
        slug="anduril",
        documents=[{"document_id": "doc1", "source_revision": revision}],
        library_id="lib1",
        output_dir=output,
    )
    assert result["model_calls"] == 0
    assert store.verify()["counts"] == before
    manifest = json.loads((output / "manifest.json").read_text())
    entry = manifest["sources"][0]
    assert entry["source_revision"] == revision
    assert (output / entry["path"]).read_text() == text
    assert not list((output / "results").glob("*.json"))
    assert (output / "summaries").is_dir()
    ResultEnvelope.model_validate(
        json.loads((output / "examples/fictional_result.json").read_text())
    )
    with zipfile.ZipFile(result["zip_path"]) as archive:
        names = archive.namelist()
        assert entry["path"] in names
        assert "results/" in names and "summaries/" in names
        assert all(not n.endswith((".sqlite", ".env", ".py")) for n in names)
        assert b"SECRET_MUST_NOT_BE_PACKED" not in b"".join(archive.read(n) for n in names)
    with pytest.raises(FileExistsError):
        package.export_package(
            data_root=data_root,
            database=store.path,
            slug="anduril",
            documents=[{"document_id": "doc1", "source_revision": revision}],
            library_id="lib1",
            output_dir=output,
        )


def test_stale_source_is_excluded_and_empty_export_fails_cleanly(store, tmp_path, monkeypatch):
    def missing(*args):
        raise ValueError("原文版本已变化")

    monkeypatch.setattr(package, "library_source", missing)
    with pytest.raises(ValueError, match="均未通过"):
        package.export_package(
            data_root=tmp_path,
            database=store.path,
            slug="anduril",
            documents=[{"document_id": "doc1", "source_revision": "a" * 64}],
            library_id="lib1",
            output_dir=tmp_path / "failed",
        )
    assert not (tmp_path / "failed").exists()
    with pytest.raises(ValueError, match="实验目录"):
        package.export_package(
            data_root=tmp_path / "widesearch_experiment",
            database=store.path,
            slug="anduril",
            documents=[],
            library_id="lib1",
            output_dir=tmp_path / "experiment",
        )


def test_business_evidence_only_reuses_grounded_named_session_findings(tmp_path):
    root = tmp_path / "business_sessions"
    session = root / "session1"
    session.mkdir(parents=True)
    text = "Prefix. Fictional-X operates underwater."
    quote = "Fictional-X operates underwater."
    source = Source("https://example.invalid/fiction", "fiction", text, "x.md", "doc1")
    good = {
        "id": "e1",
        "status": "active",
        "source": source.url,
        "finding": "underwater",
        "source_excerpt": quote,
        "span": [0, 3],
    }
    nodes = [
        good,
        {**good, "id": "e2", "status": "rejected"},
        {**good, "id": "e3", "source": "agent://x/final_summary"},
        {**good, "id": "e4", "source_excerpt": "not in current original"},
        {**good, "id": "e5", "source": "https://another.invalid"},
    ]
    (session / "search_state.json").write_text(json.dumps({"evidence_graph": {"nodes": nodes}}))
    output = package._business_evidence(
        source, {"session_ids": ["session1", "../../outside"]}, workspace_root=root
    )
    assert len(output) == 1
    assert output[0]["span"] == [8, len(text)]
    assert output[0]["candidate_only"]
    assert (
        package._business_evidence(
            source, {"session_ids": ["session1"]}, workspace_root=tmp_path / "widesearch_experiment"
        )
        == []
    )


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ("entity_kind", "未知对象类型"),
        ("relation_predicate", "未知关系类型"),
        ("relation_types", "起点对象类型"),
        ("participation_role", "未知参与角色"),
        ("participation_types", "主体/对象类型"),
        ("record_kind", "未知记录类型"),
        ("planned_boolean", "计划或部分支持"),
        ("unsupported_false", "明确否定证据"),
    ],
)
def test_offline_cli_rejects_unknown_kinds_roles_and_invalid_boolean_facts(
    store, mutation, expected
):
    from api.knowledge_handoff.cli import validate_result

    text = "Fictional-X plans to support publish-subscribe. Example-Tech is a technology."
    source = Source("https://example.invalid", "fiction", text, "x.md", "doc1")
    output = {
        "entities": [
            {"entity_key": "p", "kind": "product", "name": "Fictional-X", "identity_quote": text},
            {
                "entity_key": "t",
                "kind": "technology",
                "name": "Example-Tech",
                "identity_quote": text,
            },
        ],
        "relations": [],
        "records": [],
    }
    if mutation == "entity_kind":
        output["entities"][0]["kind"] = "invented_kind"
    elif mutation.startswith("relation_"):
        output["relations"] = [
            {
                "source_key": "p",
                "target_key": "t",
                "quote": text,
                "predicate": "invented_predicate"
                if mutation == "relation_predicate"
                else "subtechnology_of",
            }
        ]
    elif mutation.startswith("participation_"):
        output["records"] = [
            {
                "subject_key": "p",
                "target_key": "t",
                "kind": "participation",
                "quote": text,
                "label": "invented_role" if mutation == "participation_role" else "researcher",
            }
        ]
    elif mutation == "record_kind":
        output["records"] = [{"subject_key": "p", "kind": "invented_record", "quote": text}]
    else:
        output["entities"][0]["fields"] = [
            {
                "field_id": "product_supports_publish_subscribe",
                "value": mutation == "planned_boolean",
                "quote": text if mutation == "planned_boolean" else "Example-Tech is a technology.",
            }
        ]
    envelope = {
        "document_id": "doc1",
        "source_revision": "a" * 64,
        "outputs": [output],
        "status": "completed",
    }
    with pytest.raises(ValueError, match=expected):
        validate_result(envelope, source, build_contract(store))


def test_cli_normalization_retains_original_value_on_valid_synonym(store):
    from api.knowledge_handoff.cli import validate_result

    text = "Fictional-X operates underwater."
    source = Source("https://example.invalid", "fiction", text, "x.md", "doc1")
    fields = [{"field_id": "product_operating_domains", "value": ["underwater"], "quote": text}]
    envelope = {
        "document_id": "doc1",
        "source_revision": "a" * 64,
        "outputs": fixture_output(fields),
        "status": "completed",
    }
    result = validate_result(envelope, source, build_contract(store))
    assert result.outputs[0].entities[0].fields[0].value == ["水下"]
    assert result.outputs[0].entities[0].fields[0].qualifiers["original_value"] == ["underwater"]
