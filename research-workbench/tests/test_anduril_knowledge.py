"""Offline knowledge-builder regressions: no real research, model, or repository code."""

from __future__ import annotations

import json
import sqlite3

import pytest

from scripts.anduril_knowledge import (
    KINDS,
    APIError,
    KnowledgeError,
    RepositoryVerifier,
    build_knowledge,
    extract_structured,
    local_base,
)

SHA = "a" * 40


def payload(**overrides):
    result = {"schema_version": 1, **{kind: [] for kind in KINDS}}
    result.update(overrides)
    return result


def evidence(item_id="evidence:one", **overrides):
    return {
        "id": item_id,
        "source_url": "https://example.org/report",
        "title": "Source",
        "quote": "A documented release.",
        "location": "section 1",
        **overrides,
    }


def product(name="Ghost", **overrides):
    return {
        "id": "product:ghost",
        "name": name,
        "parent_id": None,
        "evidence_ids": ["evidence:one"],
        **overrides,
    }


def unit(**overrides):
    return {
        "id": "unit:demo",
        "name": "Independent reference",
        "route_ids": ["route:demo"],
        "repo_url": "https://github.com/example/demo",
        "ref": "v1",
        "commit": None,
        "license": "MIT",
        "code_paths": ["src/demo.py"],
        "inputs": ["public sample"],
        "outputs": ["metric"],
        "steps": ["Run the documented public sample"],
        "acceptance_criteria": ["finite output"],
        "limitations": ["External reference only"],
        "relationship": "independent_reference",
        "execution_status": "success",
        "evidence_ids": ["evidence:one"],
        **overrides,
    }


def put_batch(output, name, data, session=None, status="completed", report=None):
    directory = output / name
    directory.mkdir(parents=True, exist_ok=True)
    if report is None:
        report = (
            "# Batch report\n\nRead public sources.\n\n```json\n" + json.dumps(data) + "\n```\n"
        )
    (directory / "report.md").write_text(report, encoding="utf-8")
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"tasks": []}
    manifest["tasks"] = [task for task in manifest["tasks"] if task["id"] != name]
    manifest["tasks"].append(
        {"id": name, "session_id": session or f"session-{name}", "status": status}
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def read_rows(output):
    return [json.loads(line) for line in (output / "knowledge.jsonl").read_text().splitlines()]


def offline(output, **kwargs):
    return build_knowledge(output, upload=False, verify_repos=False, **kwargs)


class FakeGitHub:
    def __init__(self):
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        if "/commits/" in path:
            return {"sha": SHA}
        if "/git/trees/" in path:
            return {"truncated": False, "tree": [{"path": "src/demo.py", "type": "blob"}]}
        return {
            "full_name": "example/demo",
            "html_url": "https://github.com/example/demo",
            "license": {
                "spdx_id": "MIT",
                "name": "MIT License",
                "url": "https://api.github.com/licenses/mit",
            },
        }


class FakeLibrary:
    def __init__(self, local_only=True):
        self.local_only = local_only
        self.libraries = []
        self.documents = []
        self.uploads = []
        self.requests = []
        self.next_id = 0
        self.parse_failed = False
        self.reject_deleted = False

    def request(self, method, path, data=None):
        self.requests.append((method, path, data))
        if path == "/api/local/capabilities":
            return {"local_only_upload": self.local_only}
        if path == "/api/local/libraries":
            if method == "POST":
                self.libraries.append({"library_id": "lib_ab12", "name": data["name"]})
                return self.libraries[-1]
            return self.libraries
        if path.endswith("/documents"):
            return self.documents
        raise AssertionError(path)

    def upload(self, path, filename, content):
        assert path.endswith("/uploads?local_only=true")
        self.uploads.append((path, filename, content))
        if self.reject_deleted:
            raise APIError(409)
        self.next_id += 1
        result = {
            "document_id": f"doc_{self.next_id:02x}",
            "parse_status": "failed" if self.parse_failed else "ready",
            "index_job": None,
        }
        self.documents.append(result)
        return result


def test_final_json_block_damage_is_never_replaced_by_an_earlier_example(tmp_path):
    good = (
        "```json\n" + json.dumps(payload(products=[product()], evidence=[evidence()])) + "\n```\n"
    )
    for tail, marker in [
        ("```json\n{broken}\n```", "structured_json_invalid"),
        ("```json\n{", "structured_json_unclosed"),
    ]:
        data, issues = extract_structured(good + tail)
        assert data is None
        assert marker in issues
    put_batch(tmp_path, "broken", None, report="# text without JSON")
    result = offline(tmp_path)
    assert result["status"] == "partial"
    assert result["record_versions"] == 0
    check = json.loads((tmp_path / "manifest/validation.json").read_text())
    assert check["batches"][0]["issues"] == ["structured_json_missing"]
    with sqlite3.connect(tmp_path / "knowledge.sqlite") as db:
        assert db.execute("SELECT content FROM documents").fetchone()[0] == "# text without JSON"


def test_cross_batch_conflicts_and_revisions_preserve_all_versions_and_documents(tmp_path):
    put_batch(tmp_path, "inventory", payload(products=[product()], evidence=[evidence()]))
    first = offline(tmp_path)
    assert first["status"] == "partial"  # other required knowledge categories remain missing
    before = (tmp_path / "knowledge.jsonl").read_bytes()
    offline(tmp_path)
    assert (tmp_path / "knowledge.jsonl").read_bytes() == before
    put_batch(
        tmp_path, "systems", payload(products=[product("Different name")], evidence=[evidence()])
    )
    conflict = offline(tmp_path)
    assert conflict["status"] == "partial"
    assert conflict["record_versions"] == 4
    rows = [r for r in read_rows(tmp_path) if r["kind"] == "products"]
    assert {r["batch"] for r in rows} == {"inventory", "systems"}
    assert all("conflicting_record_versions" in r["issues"] for r in rows)
    put_batch(
        tmp_path, "systems", payload(products=[product("Revised name")], evidence=[evidence()])
    )
    revised = offline(tmp_path)
    assert revised["record_versions"] == 5
    with sqlite3.connect(tmp_path / "knowledge.sqlite") as db:
        names = db.execute("SELECT name,active,source_session FROM products").fetchall()
        assert ("Different name", 0, "session-systems") in names
        assert ("Revised name", 1, "session-systems") in names
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 3
        assert len(db.execute("SELECT * FROM evidence").fetchall()) == 2


def test_missing_evidence_unknown_maturity_and_unresolved_routes_remain_candidates(tmp_path):
    maturity = {
        "id": "maturity:unknown",
        "subject_id": "product:ghost",
        "assessment_scope": "product",
        "stage": "unknown",
        "assessment_type": "inferred",
        "source_stage": None,
        "trl": None,
        "basis": "No public test located",
        "evidence_ids": ["evidence:missing"],
    }
    put_batch(
        tmp_path,
        "one",
        payload(
            products=[product()],
            maturity_assessments=[maturity],
            reproduction_units=[unit()],
            evidence=[evidence(source_url="file:///etc/passwd")],
        ),
    )
    result = offline(tmp_path)
    assert result["status"] == "partial"
    rows = read_rows(tmp_path)
    assert all(r["validation_status"] == "candidate" for r in rows)
    by_kind = {row["kind"]: row for row in rows}
    assert "evidence_invalid:evidence:one" in by_kind["products"]["issues"]
    assert "evidence_not_found:evidence:missing" in by_kind["maturity_assessments"]["issues"]
    assert by_kind["maturity_assessments"]["payload"]["trl"] is None
    assert by_kind["maturity_assessments"]["payload"]["assessment_type"] == "inferred"
    assert by_kind["reproduction_units"]["payload"]["execution_status"] == "not_run"
    assert by_kind["reproduction_units"]["source_payload"]["execution_status"] == "success"
    assert "reference_not_found:route_ids:route:demo" in by_kind["reproduction_units"]["issues"]


def test_github_fixed_commit_path_license_verification_is_cached_but_never_execution(tmp_path):
    route = {"id": "route:demo", "name": "Reference mechanism", "evidence_ids": ["evidence:one"]}
    put_batch(
        tmp_path,
        "one",
        payload(
            products=[
                product(),
                product("Variant", id="product:variant", parent_id="product:ghost"),
            ],
            product_relations=[
                {
                    "id": "relation:variant",
                    "from_id": "product:variant",
                    "relation": "variant_of",
                    "to_id": "product:ghost",
                    "evidence_ids": ["evidence:one"],
                }
            ],
            technologies=[
                {
                    "id": "technology:demo",
                    "name": "Demo mechanism",
                    "evidence_ids": ["evidence:one"],
                }
            ],
            maturity_assessments=[
                {
                    "id": "maturity:demo",
                    "subject_id": "product:ghost",
                    "stage": "concept",
                    "source_stage": "concept",
                    "assessment_type": "source_reported",
                    "trl": None,
                    "evidence_ids": ["evidence:one"],
                }
            ],
            technology_routes=[route],
            reproduction_units=[unit()],
            evidence=[evidence()],
        ),
    )
    github = FakeGitHub()
    result = build_knowledge(tmp_path, upload=False, repo_client=github)
    assert result["status"] == "completed"
    assert len(github.paths) == 3
    row = next(row for row in read_rows(tmp_path) if row["kind"] == "reproduction_units")
    assert row["payload"]["commit"] == SHA
    assert row["payload"]["repository_verification"]["metadata_status"] == "metadata_verified"
    assert row["payload"]["execution_status"] == "not_run"
    build_knowledge(tmp_path, upload=False, repo_client=github)
    assert len(github.paths) == 3
    assert (tmp_path / "knowledge-summary.md").read_text().count("not_run") >= 2


def test_github_budget_and_non_github_and_missing_paths_are_truthful(tmp_path):
    github = FakeGitHub()
    verifier = RepositoryVerifier(tmp_path / "cache.json", client=github)
    result = verifier.verify(unit(code_paths=["missing.py"]))
    assert result["metadata_status"] == "metadata_partial"
    assert result["code_path_checks"] == [{"path": "missing.py", "exists": False}]
    assert result["execution_status"] == "not_run"
    second = verifier.verify(unit(ref="other-branch"))
    assert (
        "metadata_request_budget_exhausted" not in second["checks"]
    )  # tree is immutable and cached
    third = verifier.verify(unit(ref="third-branch"))
    assert "metadata_request_budget_exhausted" in third["checks"]
    assert len(github.paths) == 4
    result = verifier.verify(unit(repo_url="https://gitlab.com/example/demo"))
    assert result["metadata_status"] == "metadata_unchecked"
    assert len(github.paths) == 4


def test_archive_deduplicates_and_does_not_resurrect_deleted_artifact_or_library(tmp_path):
    put_batch(tmp_path, "one", payload(products=[product()], evidence=[evidence()]))
    client = FakeLibrary()
    first = build_knowledge(tmp_path, verify_repos=False, client=client)
    assert first["archive"]["uploaded"] == 3
    assert len(client.uploads) == 3
    second = build_knowledge(tmp_path, verify_repos=False, client=client)
    assert second["archive"]["deduplicated"] == 3
    assert len(client.uploads) == 3
    # The report was removed in the UI. A changed source report must not revive it.
    client.documents.pop(0)
    put_batch(tmp_path, "one", payload(products=[product("Changed")], evidence=[evidence()]))
    third = build_knowledge(tmp_path, verify_repos=False, client=client)
    assert third["archive"]["deleted_skipped"] == 1
    assert len(client.uploads) == 5  # only the changed summary and JSONL
    assert all("one-report.md" not in upload[1] for upload in client.uploads[3:])
    ledger = json.loads((tmp_path / "manifest/upload-ledger.json").read_text())
    assert ledger["artifacts"]["one/report.md"]["deleted"] is True
    client.libraries.clear()
    build_knowledge(tmp_path, verify_repos=False, client=client)
    assert len(client.uploads) == 5
    assert not client.libraries


def test_old_server_without_explicit_model_free_capability_is_never_uploaded(tmp_path):
    put_batch(tmp_path, "one", payload(products=[product()], evidence=[evidence()]))
    client = FakeLibrary(local_only=False)
    result = build_knowledge(tmp_path, verify_repos=False, client=client)
    assert result["archive"]["status"] == "partial"
    assert result["archive"]["failures"] == [{"reason": "model_free_upload_not_supported"}]
    assert client.uploads == []
    assert client.libraries == []
    assert (tmp_path / "knowledge.sqlite").is_file()


def test_saved_parse_failure_is_tracked_and_source_deleted_conflict_not_retried(tmp_path):
    put_batch(tmp_path, "one", payload(products=[product()], evidence=[evidence()]))
    client = FakeLibrary()
    client.parse_failed = True
    first = build_knowledge(tmp_path, verify_repos=False, client=client)
    assert first["archive"]["status"] == "partial"
    assert first["archive"]["uploaded"] == 3
    build_knowledge(tmp_path, verify_repos=False, client=client)
    assert len(client.uploads) == 3
    # A separate output directory has no ledger, but the server's deletion marker wins.
    other = tmp_path / "other"
    put_batch(other, "one", payload(products=[product()], evidence=[evidence()]))
    client.reject_deleted = True
    result = build_knowledge(other, verify_repos=False, client=client)
    assert result["archive"]["deleted_skipped"] == 3
    count = len(client.uploads)
    build_knowledge(other, verify_repos=False, client=client)
    assert len(client.uploads) == count


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:3000",
        "http://example.com",
        "http://user:pw@localhost",
        "http://localhost/api",
        "http://localhost?x=1",
        "http://127.0.0.1:0",
    ],
)
def test_only_credential_free_loopback_http_is_allowed(url):
    with pytest.raises(KnowledgeError):
        local_base(url)


def test_partial_collection_and_pending_missing_report_are_explicit(tmp_path):
    put_batch(
        tmp_path, "one", payload(products=[product()], evidence=[evidence()]), status="timed_out"
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["tasks"].append({"id": "pending", "status": "pending"})
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    result = offline(tmp_path)
    assert result["status"] == "partial"
    check = json.loads((tmp_path / "manifest/validation.json").read_text())
    assert check["batches"][0]["issues"] == ["research_status:timed_out"]
    assert check["batches"][1]["issues"] == ["report_missing"]
    assert result["record_versions"] == 2


def test_source_sessions_for_identical_record_are_preserved_and_global_gaps_explicit(tmp_path):
    data = payload(products=[product()], evidence=[evidence()])
    put_batch(tmp_path, "one", data, session="original-session")
    offline(tmp_path)
    put_batch(tmp_path, "one", data, session="later-session")
    result = offline(tmp_path)
    assert result["record_versions"] == 2
    rows = read_rows(tmp_path)
    assert {source["source_session"] for source in rows[0]["sources"]} == {
        "original-session",
        "later-session",
    }
    check = json.loads((tmp_path / "manifest/validation.json").read_text())
    assert set(check["missing_categories"]) == set(KINDS) - {"products", "evidence"}
    assert result["status"] == "partial"


def test_malformed_fields_and_self_links_are_preserved_as_candidates(tmp_path):
    put_batch(
        tmp_path,
        "one",
        payload(
            products=[product(parent_id="product:ghost")],
            product_relations=[
                {
                    "id": "relation:bad",
                    "from_id": "product:ghost",
                    "to_id": "product:ghost",
                    "relation": {},
                    "evidence_ids": ["evidence:one"],
                }
            ],
            technology_routes=[
                {"id": "route:bad", "product_ids": None, "evidence_ids": ["evidence:one"]}
            ],
            maturity_assessments=[
                {
                    "id": "maturity:bad",
                    "stage": {},
                    "assessment_type": [],
                    "evidence_ids": ["evidence:one"],
                }
            ],
            evidence=[evidence()],
        ),
    )
    result = offline(tmp_path)
    assert result["status"] == "partial"
    rows = {row["kind"]: row for row in read_rows(tmp_path)}
    assert "self_referential_parent_product" in rows["products"]["issues"]
    assert "self_referential_product_relation" in rows["product_relations"]["issues"]
    assert rows["technology_routes"]["payload"]["product_ids"] is None
    assert rows["maturity_assessments"]["validation_status"] == "candidate"
