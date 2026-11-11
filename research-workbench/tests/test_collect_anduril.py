"""Collection safety checks: all HTTP operations use an in-memory fake."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collect_anduril.py"
_SPEC = importlib.util.spec_from_file_location("collect_anduril_driver", _SCRIPT)
driver = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(driver)


def plan(task_count=1):
    return {
        "version": 1,
        "title": "Anduril collection fixture",
        "engine": "openai",
        "model": "gpt-4o",
        "common_prompt": "Use independently checked public sources.",
        "tasks": [
            {
                "id": f"{index:02}-task",
                "title": f"Task {index}",
                "prompt": f"Collect topic {index}.",
                "effort": "medium",
                "max_time": 600,
            }
            for index in range(1, task_count + 1)
        ],
    }


class FakeClock:
    def __init__(self):
        self.timestamp = 1000.0

    def __call__(self):
        return self.timestamp

    def sleep(self, seconds):
        self.timestamp += seconds


class FakeClient:
    """Strict API fake; unexpected paths fail instead of reaching the network."""

    def __init__(self, results=None):
        self.settings = {"openai": {"model": "gpt-4o", "api_key_configured": True}}
        self.history = []
        self.results = results or [[{"status": "completed", "answer": "Verified report."}]]
        self.sessions = {}
        self.calls = []
        self.search_count = 0
        self.submit_error = None
        self.search_404 = set()
        self.archive_results = [
            {"job_id": "job1", "status": "completed", "saved_documents": 1, "error_count": 0}
        ]

    def get(self, path):
        self.calls.append(("GET", path, None))
        if path == "/api/workbench/settings":
            return copy.deepcopy(self.settings)
        if path == "/api/history":
            return copy.deepcopy(self.history)
        if path.startswith("/api/search/") or path.startswith("/api/history/"):
            sid = path.rsplit("/", 1)[1]
            if path.startswith("/api/search/") and sid in self.search_404:
                raise driver.APIError(404)
            outcomes = self.sessions[sid]
            outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
            if isinstance(outcome, BaseException):
                raise outcome
            return copy.deepcopy(outcome)
        if path.startswith("/api/local/research-materials/backfill/"):
            outcome = (
                self.archive_results.pop(0)
                if len(self.archive_results) > 1
                else self.archive_results[0]
            )
            return copy.deepcopy(outcome)
        if path.startswith("/api/local/research-materials?session_id="):
            return {"documents": [{"document_id": "doc1"}], "sessions": []}
        raise AssertionError(f"Unexpected GET: {path}")

    def post(self, path, payload):
        self.calls.append(("POST", path, copy.deepcopy(payload)))
        if path == "/api/search":
            self.search_count += 1
            if self.submit_error:
                raise self.submit_error
            sid = f"sid{self.search_count}"
            self.sessions[sid] = copy.deepcopy(self.results[self.search_count - 1])
            return {"session_id": sid, "status": "running"}
        if path == "/api/local/research-materials/backfill":
            return copy.deepcopy(self.archive_results[0])
        if path.startswith("/api/search/") and path.endswith("/stop"):
            return {"status": "stopping"}
        raise AssertionError(f"Unexpected POST: {path}")


def runner(tmp_path, fake, collection_plan=None, **kwargs):
    timer = FakeClock()
    return driver.CollectionRunner(
        collection_plan or plan(), tmp_path, fake, clock=timer, sleep=timer.sleep, **kwargs
    )


def checkpoint(tmp_path):
    return json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))


class IngestionClient(FakeClient):
    def __init__(self, statuses):
        super().__init__()
        self.ingestion_statuses = list(statuses)

    def post(self, path, payload):
        if path == "/api/knowledge/ingestion":
            self.calls.append(("POST", path, copy.deepcopy(payload)))
            return {"scheduled": 0, "reused": 1, "jobs": []}
        return super().post(path, payload)

    def get(self, path):
        if path.startswith("/api/knowledge/ingestion?"):
            self.calls.append(("GET", path, None))
            status = (
                self.ingestion_statuses.pop(0)
                if len(self.ingestion_statuses) > 1
                else self.ingestion_statuses[0]
            )
            return {
                "counts": {status: 1},
                "jobs": [{"id": "ingest1", "status": status, "entities": 2, "relations": 1}],
            }
        return super().get(path)


def test_database_driver_waits_for_real_ingestion_and_resume_reuses_receipt(tmp_path):
    fake = IngestionClient(["queued", "running", "completed"])
    assert runner(tmp_path, fake, wait_ingestion=True).run() == 0
    saved = checkpoint(tmp_path)["tasks"]["01-task"]["knowledge_ingestion"]
    assert saved["status"] == "completed" and saved["entities"] == 2
    before = list(fake.calls)
    assert runner(tmp_path, fake, wait_ingestion=True).run() == 0
    assert fake.calls == before and fake.search_count == 1


def test_completed_research_with_failed_ingestion_is_partial_not_database_success(tmp_path):
    fake = IngestionClient(["failed"])
    assert runner(tmp_path, fake, wait_ingestion=True).run() == 1
    saved = checkpoint(tmp_path)["tasks"]["01-task"]
    assert saved["status"] == "completed" and saved["knowledge_ingestion"]["status"] == "partial"
    assert fake.search_count == 1


def test_successful_web_retry_then_resume_finishes_without_resubmitting_research(tmp_path):
    fake = IngestionClient(["failed"])
    assert runner(tmp_path, fake, wait_ingestion=True).run() == 1
    fake.ingestion_statuses = ["completed"]
    assert runner(tmp_path, fake, wait_ingestion=True).run() == 0
    assert fake.search_count == 1
    assert checkpoint(tmp_path)["tasks"]["01-task"]["status"] == "completed"


def test_database_destination_requires_archive_and_ingestion_not_a_report_body(tmp_path):
    fake = IngestionClient(["completed"])
    fake.results = [[{"status": "completed", "answer": ""}]]
    database_plan = plan()
    database_plan["knowledge_destination"] = "entity_database"
    assert runner(tmp_path, fake, collection_plan=database_plan, wait_ingestion=True).run() == 0
    assert checkpoint(tmp_path)["tasks"]["01-task"]["knowledge_ingestion"]["status"] == "completed"


def test_unqueued_archived_document_prevents_claiming_complete_database_ingestion(tmp_path):
    class UnqueuedDocument(IngestionClient):
        def get(self, path):
            if path.startswith("/api/local/research-materials?session_id="):
                return {"documents": [{"document_id": "doc1"}, {"document_id": "doc2"}]}
            return super().get(path)

    fake = UnqueuedDocument(["completed"])
    assert runner(tmp_path, fake, wait_ingestion=True).run() == 1
    receipt = checkpoint(tmp_path)["tasks"]["01-task"]["knowledge_ingestion"]
    assert receipt["status"] == "partial" and receipt["unprocessed_documents"] == 1
    assert fake.search_count == 1


def test_database_ingestion_deadline_preserves_background_job_and_does_not_resubmit_research(
    tmp_path,
):
    fake = IngestionClient(["running"])
    assert runner(tmp_path, fake, wait_ingestion=True, ingestion_timeout=1).run() == 1
    assert checkpoint(tmp_path)["tasks"]["01-task"]["knowledge_ingestion"]["status"] == "tracking"
    fake.ingestion_statuses = ["completed"]
    assert runner(tmp_path, fake, wait_ingestion=True).run() == 0
    assert fake.search_count == 1


def test_completed_run_skips_research_and_archive_on_second_run(tmp_path):
    fake = FakeClient()
    assert runner(tmp_path, fake).run() == 0
    first_calls = list(fake.calls)
    assert runner(tmp_path, fake).run() == 0
    assert fake.calls == first_calls
    assert fake.search_count == 1
    assert checkpoint(tmp_path)["tasks"]["01-task"]["status"] == "completed"
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["tasks"][0]["backfill"]["document_count"] == 1
    assert "Verified report." in (tmp_path / "01-task" / "report.md").read_text()


def test_submission_timeout_is_unknown_and_never_automatically_reposted(tmp_path):
    fake = FakeClient()
    fake.submit_error = driver.APIError()
    assert runner(tmp_path, fake).run() != 0
    assert checkpoint(tmp_path)["tasks"]["01-task"]["status"] == "submission_unknown"
    fake.submit_error = None
    assert runner(tmp_path, fake, retry_failed=True).run() != 0
    assert fake.search_count == 1
    assert not any("backfill" in call[1] for call in fake.calls)


def test_crash_after_durable_submitting_checkpoint_never_reposts(tmp_path):
    fake = FakeClient()
    initial = runner(tmp_path, fake)
    initial.state["tasks"]["01-task"].update(status="submitting", submitted_epoch=1000)
    initial.save()
    assert runner(tmp_path, fake).run() != 0
    assert fake.search_count == 0
    assert checkpoint(tmp_path)["tasks"]["01-task"]["status"] == "submission_unknown"


def test_interrupt_during_submission_keeps_unknown_and_never_reposts(tmp_path):
    fake = FakeClient()
    fake.submit_error = KeyboardInterrupt()
    assert runner(tmp_path, fake).run() == 130
    assert checkpoint(tmp_path)["tasks"]["01-task"]["status"] == "submission_unknown"
    assert "后台研究保留" in checkpoint(tmp_path)["driver_error"]
    fake.submit_error = None
    assert runner(tmp_path, fake).run() != 0
    assert fake.search_count == 1
    assert not any(call[1].endswith("/stop") for call in fake.calls)


def test_interrupt_preserves_running_session_and_resume_polls_same_id(tmp_path):
    fake = FakeClient([[{"status": "running", "answer": "Partial progress."}]])
    initial = runner(tmp_path, fake)

    def interrupt(_seconds):
        raise KeyboardInterrupt

    initial.sleep = interrupt
    assert initial.run() == 130
    state = checkpoint(tmp_path)["tasks"]["01-task"]
    assert state["status"] == "running"
    assert state["session_id"] == "sid1"
    assert not any(call[1].endswith("/stop") for call in fake.calls)
    fake.sessions["sid1"] = [{"status": "completed", "answer": "Resumed report."}]
    assert runner(tmp_path, fake).run() == 0
    assert fake.search_count == 1
    assert "Resumed report." in (tmp_path / "01-task" / "report.md").read_text()


def test_resume_uses_history_fallback_without_restarting_research(tmp_path):
    fake = FakeClient()
    initial = runner(tmp_path, fake)
    initial.state["tasks"]["01-task"].update(
        status="running", session_id="older-session", submitted_epoch=1000, max_time=600
    )
    initial.save()
    fake.sessions["older-session"] = [{"status": "completed", "answer": "Durable report."}]
    fake.search_404.add("older-session")
    assert runner(tmp_path, fake).run() == 0
    assert fake.search_count == 0
    assert ("GET", "/api/history/older-session", None) in fake.calls


def test_poll_network_failure_keeps_session_and_resume_does_not_repost(tmp_path):
    fake = FakeClient([[driver.APIError()]])
    assert runner(tmp_path, fake).run() != 0
    assert checkpoint(tmp_path)["tasks"]["01-task"]["session_id"] == "sid1"
    assert checkpoint(tmp_path)["tasks"]["01-task"]["status"] == "running"
    fake.sessions["sid1"] = [{"status": "completed", "answer": "Recovered report."}]
    assert runner(tmp_path, fake).run() == 0
    assert fake.search_count == 1


def test_failed_research_keeps_partial_report_continues_and_is_not_success(tmp_path):
    fake = FakeClient(
        [
            [{"status": "error", "answer": "Incomplete but useful report.", "partial": True}],
            [{"status": "completed", "answer": "Second report."}],
        ]
    )
    collection_plan = plan(2)
    assert runner(tmp_path, fake, collection_plan).run() != 0
    tasks = checkpoint(tmp_path)["tasks"]
    assert tasks["01-task"]["status"] == "partial"
    assert tasks["02-task"]["status"] == "completed"
    assert tasks["01-task"]["session_status"] == "error"
    assert "Incomplete but useful" in (tmp_path / "01-task" / "report.md").read_text()
    result = json.loads((tmp_path / "01-task" / "result.json").read_text())
    assert result["status"] == "error"
    assert json.loads((tmp_path / "manifest.json").read_text())["status"] == "partial"
    assert runner(tmp_path, fake, collection_plan).run() != 0
    assert fake.search_count == 2


def test_completed_response_without_report_is_not_reported_as_success(tmp_path):
    fake = FakeClient([[{"status": "completed", "answer": "   "}]])
    assert runner(tmp_path, fake).run() != 0
    assert checkpoint(tmp_path)["tasks"]["01-task"]["status"] == "partial"
    assert json.loads((tmp_path / "manifest.json").read_text())["status"] == "partial"


def test_failed_response_without_report_is_failed_and_not_retried(tmp_path):
    fake = FakeClient([[{"status": "error", "answer": ""}]])
    assert runner(tmp_path, fake).run() != 0
    assert checkpoint(tmp_path)["tasks"]["01-task"]["status"] == "failed"
    assert json.loads((tmp_path / "manifest.json").read_text())["status"] == "failed"
    assert runner(tmp_path, fake).run() != 0
    assert fake.search_count == 1


def test_retry_failed_is_explicit_and_preserves_prior_attempt(tmp_path):
    fake = FakeClient(
        [
            [{"status": "error", "answer": "Prior partial report."}],
            [{"status": "completed", "answer": "Retry completed report."}],
        ]
    )
    assert runner(tmp_path, fake).run() != 0
    assert runner(tmp_path, fake).run() != 0
    assert fake.search_count == 1
    assert runner(tmp_path, fake, retry_failed=True).run() == 0
    assert fake.search_count == 2
    entry = checkpoint(tmp_path)["tasks"]["01-task"]
    assert entry["attempt"] == 2
    assert entry["attempts"][0]["session_id"] == "sid1"
    assert entry["attempts"][0]["status"] == "partial"
    archived = tmp_path / "01-task" / "attempts" / "1"
    assert json.loads((archived / "result.json").read_text())["status"] == "error"
    assert "Prior partial report." in (archived / "report.md").read_text()


def test_retry_submission_failure_does_not_leave_old_report_as_current(tmp_path):
    fake = FakeClient([[{"status": "error", "answer": "Old partial report."}]])
    assert runner(tmp_path, fake).run() != 0
    fake.submit_error = driver.APIError()
    assert runner(tmp_path, fake, retry_failed=True).run() != 0
    entry = checkpoint(tmp_path)["tasks"]["01-task"]
    assert entry["status"] == "submission_unknown"
    assert (tmp_path / "01-task" / "attempts" / "1" / "result.json").exists()
    current_result = json.loads((tmp_path / "01-task" / "result.json").read_text())
    assert current_result["status"] == "submission_unknown"
    assert not current_result.get("answer")
    assert "Old partial report." not in (tmp_path / "01-task" / "report.md").read_text()


def test_plan_hash_change_refuses_existing_progress_without_network(tmp_path):
    fake = FakeClient()
    runner(tmp_path, fake)
    changed = plan()
    changed["tasks"][0]["prompt"] = "A new scope requires a new collection."
    with pytest.raises(driver.CollectionError, match="hash"):
        runner(tmp_path, fake, changed)
    assert fake.calls == []


def test_other_running_research_prevents_submission_and_is_not_stopped(tmp_path):
    fake = FakeClient()
    fake.history = [{"session_id": "user-research", "status": "running"}]
    assert runner(tmp_path, fake).run() != 0
    assert fake.search_count == 0
    assert not any(call[0] == "POST" for call in fake.calls)


@pytest.mark.parametrize(
    "openai",
    [
        {"model": "gpt-4.1", "api_key_configured": True},
        {"model": "gpt-4o", "api_key_configured": False},
        {"model": "gpt-4o"},
    ],
)
def test_wrong_or_unconfigured_openai_never_submits(tmp_path, openai):
    fake = FakeClient()
    fake.settings = {"openai": openai}
    assert runner(tmp_path, fake).run() != 0
    assert fake.search_count == 0
    assert not any(call[0] == "POST" for call in fake.calls)


def test_timeout_sends_stop_once_and_keeps_timed_out_on_resume(tmp_path):
    fake = FakeClient([[{"status": "running", "answer": "Saved unfinished progress."}]])
    assert runner(tmp_path, fake, max_time=1, poll_interval=181).run() != 0
    entry = checkpoint(tmp_path)["tasks"]["01-task"]
    assert entry["status"] == "timed_out"
    stop_calls = [call for call in fake.calls if call[1].endswith("/stop")]
    assert len(stop_calls) == 1
    assert runner(tmp_path, fake).run() != 0
    assert len([call for call in fake.calls if call[1].endswith("/stop")]) == 1
    assert fake.search_count == 1


def test_archive_failure_never_resubmits_completed_research(tmp_path):
    fake = FakeClient()
    fake.archive_results = [{"job_id": "job1", "status": "failed", "error_count": 1}]
    assert runner(tmp_path, fake).run() != 0
    assert checkpoint(tmp_path)["tasks"]["01-task"]["status"] == "completed"
    fake.archive_results = [{"job_id": "job2", "status": "completed", "error_count": 0}]
    assert runner(tmp_path, fake, retry_failed=True).run() == 0
    assert fake.search_count == 1


def test_queued_archive_is_polled_and_counted_without_another_research(tmp_path):
    fake = FakeClient()
    fake.archive_results = [
        {"job_id": "job1", "status": "queued", "saved_documents": 0, "error_count": 0},
        {"job_id": "job1", "status": "completed", "saved_documents": 1, "error_count": 0},
    ]
    assert runner(tmp_path, fake).run() == 0
    assert fake.search_count == 1
    assert any(call[1] == "/api/local/research-materials/backfill/job1" for call in fake.calls)
    assert checkpoint(tmp_path)["tasks"]["01-task"]["backfill"]["document_count"] == 1


def test_payload_fixes_engine_modules_web_scope_and_truncates_inventory():
    collection_plan = plan()
    task = collection_plan["tasks"][0]
    payload = driver.build_payload(collection_plan, task, inventory="x" * 15000, max_time=45)
    assert payload["engine"] == "openai"
    assert payload["modules"] == []
    assert payload["sources"] == {"mode": "web"}
    assert payload["effort"] == "medium"
    assert payload["max_time"] == 45
    assert "不是本批事实证据" in payload["query"]
    assert "已明确截断" in payload["query"]
    assert "x" * 12000 in payload["query"]
    assert "x" * 12001 not in payload["query"]


def test_extra_context_is_bounded_and_explicitly_requires_original_source_verification():
    collection_plan = plan()
    payload = driver.build_payload(
        collection_plan, collection_plan["tasks"][0], extra_context="z" * 30000
    )
    query = payload["query"]
    assert "z" * 24000 in query
    assert "z" * 24001 not in query
    assert "24000字上限截断" in query
    assert "仅供跨批发现和核对线索" in query
    assert "不是独立证据" in query
    assert "仍须读取原始公开来源核验" in query


def test_late_batches_receive_bounded_prior_reports_including_recent_batches(tmp_path):
    collection_plan = plan(13)
    fake = FakeClient()
    collection = runner(tmp_path, fake, collection_plan)
    for index, task in enumerate(collection_plan["tasks"][:12], start=1):
        marker = chr(0x5100 + index)
        driver.atomic_json(tmp_path / task["id"] / "result.json", {"answer": marker * 4000})
        collection.state["tasks"][task["id"]].update(
            status="partial" if index == 12 else "completed", session_id=f"existing-{index}"
        )
    assert collection.prior_context(collection_plan["tasks"][8]) == ""
    tenth_context = collection.prior_context(collection_plan["tasks"][9])
    assert "批次 02-task" in tenth_context
    assert "批次 09-task" in tenth_context
    context = collection.prior_context(collection_plan["tasks"][12])
    assert len(context) <= 24000
    assert "批次 01-task" not in context
    for index in range(2, 13):
        task_id = f"{index:02}-task"
        marker = chr(0x5100 + index)
        assert f"批次 {task_id}" in context
        assert f"session_id：existing-{index}" in context
        assert str(tmp_path / task_id / "report.md") in context
        assert marker in context
        assert marker * 3001 not in context
    assert "状态：partial" in context
    assert fake.calls == []


def test_build_knowledge_runs_after_partial_collection_and_keeps_nonzero_exit(
    tmp_path, monkeypatch
):
    plan_path = tmp_path / "plan.json"
    output_dir = tmp_path / "out"
    plan_path.write_text(json.dumps(plan()), encoding="utf-8")
    fake = FakeClient([[{"status": "error", "answer": "Saved partial collection."}]])
    calls = []

    def build_knowledge(collection_dir, base_url):
        calls.append((collection_dir, base_url))
        assert checkpoint(collection_dir)["tasks"]["01-task"]["status"] == "partial"
        return {"status": "completed", "unit_count": 1}

    knowledge_module = types.ModuleType("anduril_knowledge")
    knowledge_module.build_knowledge = build_knowledge
    monkeypatch.setitem(sys.modules, "anduril_knowledge", knowledge_module)
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    monkeypatch.setattr(driver, "LocalAPI", lambda *args, **kwargs: fake)
    monkeypatch.syspath_prepend(str(tmp_path))
    exit_code = driver.main(
        [
            "--run",
            "--build-knowledge",
            "--plan",
            str(plan_path),
            "--output-dir",
            str(output_dir),
        ]
    )
    assert exit_code != 0
    assert calls == [(output_dir, "http://127.0.0.1:3000")]
    assert fake.search_count == 1
    assert json.loads((output_dir / "knowledge-build.json").read_text())["status"] == "completed"
    assert json.loads((output_dir / "manifest.json").read_text())["status"] == "partial"


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:3000",
        "http://example.com:3000",
        "http://0.0.0.0:3000",
        "http://127.0.0.1:3000/api",
        "http://user:password@localhost:3000",
    ],
)
def test_api_base_rejects_non_loopback_or_credentialed_urls(url):
    with pytest.raises(driver.CollectionError):
        driver.validate_base_url(url)


def test_plan_status_and_dry_run_modes_do_not_construct_http_client(tmp_path, monkeypatch):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan()), encoding="utf-8")

    def forbid_http(*args, **kwargs):
        raise AssertionError("Read-only modes must not construct an HTTP client")

    monkeypatch.setattr(driver, "LocalAPI", forbid_http)
    for mode in ([], ["--status"], ["--dry-run"]):
        assert (
            driver.main(["--plan", str(plan_path), "--output-dir", str(tmp_path / "out"), *mode])
            == 0
        )
    assert not (tmp_path / "out").exists()


def test_global_lock_rejects_a_second_driver_until_first_releases(tmp_path):
    lock_path = tmp_path / "collection.lock"
    with driver.collection_lock(lock_path):
        with pytest.raises(driver.CollectionError, match="另一个"):
            with driver.collection_lock(lock_path):
                pytest.fail("Second driver acquired the active collection lock")
    with driver.collection_lock(lock_path):
        pass


def quality_result(*, source="https://www.anduril.com/omen", anchor=True):
    """A source-backed shape; the quality gate tests integrity, not model truth."""
    node = {
        "id": "fact_1",
        "source": source,
        "source_excerpt": "Saved original excerpt.",
        "page_id": "page_1" if anchor else "",
        "status": "active",
    }
    report = {key: [] for key in driver.KNOWLEDGE_TABLES}
    report["products"] = [{"id": "product:omen", "evidence_ids": ["evidence:fact_1"]}]
    return {
        "status": "completed",
        "answer": "```json\n" + json.dumps(report) + "\n```",
        "search_state": {
            "evidence_graph": {"nodes": [node], "observations": []},
            "coverage_map": {"tables": {"products": {"entities": ["product:omen"]}}},
        },
    }


def test_real_plan_enables_strict_quality_without_changing_plan_hash():
    collection_plan = driver.load_plan(driver.DEFAULT_PLAN)
    before = driver.plan_hash(collection_plan)
    assert driver.requires_content_quality(collection_plan)
    assert driver.plan_hash(collection_plan) == before
    assert not driver.requires_content_quality(plan())


def test_strict_quality_preserves_api_completion_but_rejects_empty_knowledge(tmp_path):
    # Matches actual 01/04/10-13: completed + nonempty admission + all empty JSON.
    report = {key: [] for key in driver.KNOWLEDGE_TABLES}
    result = {
        "status": "completed",
        "answer": "任务尚未完成，目前没有找到相关的证据记录。\n```json\n"
        + json.dumps(report)
        + "\n```",
        "search_state": {
            "evidence_graph": {"nodes": []},
            "coverage_map": {"tables": {"products": {"entities": []}}},
        },
    }
    fake = FakeClient([[result]])
    assert runner(tmp_path, fake, strict_quality=True).run() == 1
    entry = checkpoint(tmp_path)["tasks"]["01-task"]
    assert entry["status"] == "partial"
    assert entry["session_status"] == "completed"
    saved = json.loads((tmp_path / "01-task/result.json").read_text())
    assert saved["status"] == "completed"
    audit = json.loads((tmp_path / "01-task/quality-audit.json").read_text())
    assert {
        "report_admits_missing_evidence",
        "structured_knowledge_empty",
        "coverage_tables_empty",
        "no_traceable_fact_evidence",
    } <= set(audit["issues"])
    assert runner(tmp_path, fake, strict_quality=True).run() == 1
    assert fake.search_count == 1  # Quality failure never causes an implicit paid retry.


@pytest.mark.parametrize(
    "bad_answer,issue",
    [
        ("invalid-evidence-reference", "invalid_citations"),
        ("[agent://orchestrator/asserted]", "agent_assertion_citations"),
        ("[evidence:missing_id]", "invalid_citations"),
    ],
)
def test_real_bad_citation_forms_are_quality_failures(bad_answer, issue):
    result = quality_result()
    result["answer"] += "\n" + bad_answer
    audit = driver.audit_content_quality(result)
    assert issue in audit["issues"]
    assert audit["content_status"] == "partial"


@pytest.mark.parametrize(
    "source,anchor,candidates,assertions",
    [
        ("agent://orchestrator/asserted", False, 0, 1),
        ("https://anduril.com/omen", False, 1, 0),
        ("https://...", True, 0, 0),
    ],
)
def test_asserted_and_unanchored_nodes_are_not_traceable_facts(
    source, anchor, candidates, assertions
):
    # Matches actual 03/05/06/08/09 assertions and 07 manually edited excerpts.
    audit = driver.audit_content_quality(quality_result(source=source, anchor=anchor))
    assert audit["traceable_fact_evidence_count"] == 0
    assert audit["source_excerpt_candidate_count"] == candidates
    assert audit["agent_assertion_node_count"] == assertions
    assert "no_traceable_fact_evidence" in audit["issues"]


def test_minimum_valid_content_and_custom_http_fixture_are_compatible(tmp_path):
    audit = driver.audit_content_quality(quality_result())
    assert audit["content_status"] == "meets_minimum_checks"
    assert audit["traceable_fact_evidence_count"] == 1
    assert audit["structured_row_counts"]["products"] == 1
    assert runner(tmp_path, FakeClient()).run() == 0
    assert (
        json.loads((tmp_path / "01-task/quality-audit.json").read_text())["policy"]
        == "nonempty-report-v1"
    )


def test_offline_audit_writes_only_sidecars_and_never_constructs_http_client(tmp_path, monkeypatch):
    collection_plan = plan()
    collection_plan["content_quality"] = driver.QUALITY_POLICY
    plan_path = tmp_path / "plan.json"
    driver.atomic_json(plan_path, collection_plan)
    output = tmp_path / "collection"
    report_dir = output / "01-task"
    driver.atomic_json(
        report_dir / "result.json", {"status": "completed", "answer": "任务尚未完成"}
    )
    (report_dir / "report.md").write_text("Original report bytes\n", encoding="utf-8")
    driver.atomic_json(output / "checkpoint.json", {"tasks": {"01-task": {"status": "completed"}}})
    driver.atomic_json(output / "manifest.json", {"status": "completed"})
    before = {path: path.read_bytes() for path in output.rglob("*") if path.is_file()}

    def forbid_http(*args, **kwargs):
        raise AssertionError("Offline quality audit must not construct an HTTP client")

    monkeypatch.setattr(driver, "LocalAPI", forbid_http)
    assert (
        driver.main(["--audit-quality", "--plan", str(plan_path), "--output-dir", str(output)]) == 1
    )
    assert all(path.read_bytes() == data for path, data in before.items())
    audit = json.loads((output / "quality-audit.json").read_text())
    assert audit["original_collection_states_preserved"] is True
    assert audit["tasks"][0]["collection_status"] == "completed"
    assert audit["tasks"][0]["content_status"] == "partial"
    assert set(path for path in output.rglob("*") if path.is_file()) - before.keys() == {
        output / "quality-audit.json",
        report_dir / "quality-audit.json",
    }


def test_saved_source_audit_excludes_snippet_fallback_and_verifies_original(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    pages = tmp_path / "data/runs/session_1/pages"
    for page_id, content in [
        ("full", "Full original text. " * 20),
        ("snippet", "[NOTE] Live fetch failed; search-engine snippet, not the full page. " * 5),
    ]:
        driver.atomic_json(
            pages / f"{page_id}.meta.json", {"url": f"https://www.anduril.com/{page_id}"}
        )
        (pages / f"{page_id}.md").write_text(content)
    payload = b"original HTML"
    digest = driver.hashlib.sha256(payload).hexdigest()
    objects = pages / "archive/objects"
    objects.mkdir(parents=True)
    (objects / f"{digest}.bin").write_bytes(payload)
    driver.atomic_json(
        pages / "archive/records/doc.json",
        {
            "source_url": "https://www.anduril.com/full",
            "original": {"path": f"objects/{digest}.bin", "bytes": len(payload), "sha256": digest},
        },
    )
    audit = driver.audit_saved_sources({"session_id": "session_1"})
    assert audit["saved_page_count"] == 2
    assert audit["full_text_candidate_count"] == 1
    assert audit["snippet_fallback_page_count"] == 1
    assert audit["verified_original_record_count"] == 1
    assert audit["corrupt_original_record_count"] == 0


def test_offline_audit_cannot_certify_a_missing_collection(tmp_path):
    with pytest.raises(driver.CollectionError, match="没有可审计"):
        driver.write_collection_quality_audit(tmp_path)
