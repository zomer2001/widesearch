#!/usr/bin/env python3
"""Sequential, resumable collection through the existing local workbench API.

No arguments prints the plan without making requests. Only --run / --resume can
start research. The driver never reads credentials or changes server settings.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = ROOT / "plans/anduril-products-technology.json"
DEFAULT_OUTPUT = ROOT / "data/collections/anduril-products-technology"
TERMINAL = {"completed", "partial", "failed", "timed_out"}
ACTIVE = {"running", "queued", "pending", "stopping"}
QUALITY_POLICY = "anduril-evidence-v1"
KNOWLEDGE_TABLES = (
    "products",
    "product_relations",
    "technologies",
    "technology_routes",
    "maturity_assessments",
    "reproduction_units",
    "evidence",
)


class CollectionError(Exception):
    """A safe, user-facing error with no remote response body."""


class APIError(CollectionError):
    def __init__(self, status: int | None = None):
        self.status = status
        super().__init__(f"本地 API 返回 HTTP {status}" if status else "本地 API 连接或响应异常")


def now() -> str:
    return datetime.now(UTC).isoformat()


def redact(value):
    """Do not persist credential fields or echo server exception bodies."""
    if isinstance(value, dict):
        return {
            key: redact(item)
            for key, item in value.items()
            if not re.search(r"api[_-]?key|secret|password|authorization|access_token", key, re.I)
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
    return value


def atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(redact(data), stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise CollectionError(f"无法读取有效 JSON：{path.name}") from None


def validate_plan(plan: dict) -> dict:
    if not isinstance(plan, dict) or plan.get("version") != 1:
        raise CollectionError("计划格式必须为 version: 1")
    if plan.get("engine") != "openai" or plan.get("model") != "gpt-4o":
        raise CollectionError("计划必须明确指定 engine=openai 和 model=gpt-4o")
    if not isinstance(plan.get("common_prompt"), str):
        raise CollectionError("计划缺少 common_prompt")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise CollectionError("计划必须包含非空 tasks 数组")
    ids = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise CollectionError("计划任务格式无效")
        task_id = task.get("id", "")
        if not isinstance(task_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", task_id
        ):
            raise CollectionError("任务 ID 只能使用字母、数字、下划线和连字符")
        if task_id in ids:
            raise CollectionError("计划含有重复任务 ID")
        ids.add(task_id)
        if not all(
            isinstance(task.get(key), str) and task[key].strip() for key in ("title", "prompt")
        ):
            raise CollectionError("任务必须有非空 title 和 prompt")
        if task.get("effort", "medium") not in {"low", "medium", "high", "max"}:
            raise CollectionError("任务 effort 无效")
        budget = task.get("max_time", 600)
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise CollectionError("max_time 必须为正整数秒")
    return plan


def load_plan(path: Path) -> dict:
    return validate_plan(read_json(Path(path)))


def plan_hash(plan: dict) -> str:
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def requires_content_quality(plan: dict) -> bool:
    """Apply the evidence contract to the real plan, without changing its hash.

    Small HTTP-driver fixtures and other custom plans retain the basic nonempty
    answer contract. Custom plans can explicitly opt into the stronger policy.
    """
    return plan.get("content_quality") == QUALITY_POLICY or (
        "Anduril 产品与技术知识库" in plan.get("common_prompt", "")
        and any(task.get("id") == "01-product-lineage" for task in plan.get("tasks", []))
    )


def _public_source(value) -> bool:
    if not isinstance(value, str) or re.search(
        r"invalid-|\.\.\.|[<>]|example\.(com|org)|placeholder", value, re.I
    ):
        return False
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and "." in parsed.hostname
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def _report_json(answer: str) -> list[dict]:
    candidates = [answer.strip(), *re.findall(r"```(?:json)?\s*\n(.*?)```", answer, re.S | re.I)]
    records = []
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and any(key in parsed for key in KNOWLEDGE_TABLES):
            if parsed not in records:
                records.append(parsed)
    return records


def audit_content_quality(result: dict, *, strict: bool = True) -> dict:
    """Local, deterministic integrity checks; never infer knowledge from run status.

    A traceable fact requires a real source, an excerpt, and a persisted anchor.
    A URL typed by an agent, or an agent:// assertion, is a candidate rather than
    grounded evidence. This checks provenance integrity, not semantic truth or
    exhaustive research coverage.
    """
    answer = result.get("answer") if isinstance(result.get("answer"), str) else ""
    state = result.get("search_state")
    state = state if isinstance(state, dict) else {}
    graph = state.get("evidence_graph", {})
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    nodes = [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []
    active = [node for node in nodes if node.get("status", "active") == "active"]
    source_candidates = [
        node for node in active if _public_source(node.get("source")) and node.get("source_excerpt")
    ]
    grounded = [
        node
        for node in source_candidates
        if any(node.get(key) for key in ("page_id", "observation_id", "text_hash"))
    ]
    asserted = [node for node in active if str(node.get("source", "")).startswith("agent://")]
    structured = _report_json(answer)
    row_counts = {
        key: sum(
            len(record.get(key, [])) for record in structured if isinstance(record.get(key), list)
        )
        for key in KNOWLEDGE_TABLES
    }
    coverage = state.get("coverage_map", {})
    tables = coverage.get("tables", {}) if isinstance(coverage, dict) else {}
    table_rows = {
        key: len(table.get("entities", []))
        for key, table in tables.items()
        if isinstance(table, dict) and isinstance(table.get("entities", []), list)
    }
    invalid_count = len(re.findall(r"invalid-(?:evidence|local)-reference", answer))
    asserted_count = len(re.findall(r"agent://[^\s\]<>\"']+", answer))
    unresolved = sorted(set(re.findall(r"evidence:[A-Za-z0-9_-]+", answer)))
    node_ids = {str(node.get("id")) for node in active}
    unresolved = [ref for ref in unresolved if ref.removeprefix("evidence:") not in node_ids]
    bad_examples = []
    for match in re.finditer(r"invalid-(?:evidence|local)-reference|agent://[^\s\]<>\"']+", answer):
        excerpt = answer[max(0, match.start() - 60) : match.end() + 80].replace("\n", " ")
        if excerpt not in bad_examples:
            bad_examples.append(excerpt)
        if len(bad_examples) == 3:
            break
    incomplete = re.search(
        r"任务尚未完成|(?:尚未找到|没有找到相关的)[^。\n]{0,100}证据|具体证据仍然不足",
        answer[:1200],
    )
    issues = []
    if not answer.strip():
        issues.append("empty_report")
    if strict:
        if incomplete:
            issues.append("report_admits_missing_evidence")
        if not structured:
            issues.append("structured_knowledge_missing")
        elif not any(row_counts[key] for key in KNOWLEDGE_TABLES if key != "evidence"):
            issues.append("structured_knowledge_empty")
        if tables and not any(table_rows.values()):
            issues.append("coverage_tables_empty")
        if invalid_count or unresolved:
            issues.append("invalid_citations")
        if asserted_count:
            issues.append("agent_assertion_citations")
        if not grounded:
            issues.append("no_traceable_fact_evidence")
    return {
        "version": 1,
        "policy": QUALITY_POLICY if strict else "nonempty-report-v1",
        "strict": strict,
        "session_status": result.get("status"),
        "content_status": "partial" if issues else "meets_minimum_checks",
        "issues": issues,
        "report_characters": len(answer),
        "structured_blocks": len(structured),
        "structured_row_counts": row_counts,
        "coverage_table_row_counts": table_rows,
        "reported_evidence_count": result.get("evidence_count"),
        "graph_node_count": len(nodes),
        "agent_assertion_node_count": len(asserted),
        "source_excerpt_candidate_count": len(source_candidates),
        "unanchored_source_candidate_count": len(source_candidates) - len(grounded),
        "traceable_fact_evidence_count": len(grounded),
        "invalid_citation_occurrences": invalid_count,
        "agent_citation_occurrences": asserted_count,
        "unresolved_evidence_refs": unresolved[:10],
        "bad_citation_examples": bad_examples,
        "self_reported_gap_excerpt": incomplete.group(0) if incomplete else "",
        "observation_count": len(graph.get("observations", [])) if isinstance(graph, dict) else 0,
        "explored_path_count": len(state.get("explored_paths", [])),
        "scope": (
            "Local provenance and content checks; does not certify truth or complete coverage."
        ),
    }


def audit_saved_sources(result: dict) -> dict:
    """Count saved public pages, excluding explicitly labelled snippet fallbacks.

    Only the session's public page/archive directory is inspected. No HTTP,
    credentials, settings, conversation logs, or arbitrary workspace paths.
    """
    sid = result.get("session_id", "")
    unavailable = {
        "available": False,
        "saved_page_count": 0,
        "full_text_candidate_count": 0,
        "snippet_fallback_page_count": 0,
        "archive_record_count": 0,
        "verified_original_record_count": 0,
        "verified_original_unique_url_count": 0,
        "corrupt_original_record_count": 0,
        "pages": [],
        "verified_original_urls": [],
    }
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", sid):
        return unavailable
    pages = ROOT / "data/runs" / sid / "pages"
    if not pages.is_dir():
        return unavailable
    entries = []
    for metadata in sorted(pages.glob("*.meta.json")):
        page_id = metadata.name.removesuffix(".meta.json")
        content = pages / (page_id + ".md")
        if not content.is_file():
            continue
        record = read_json(metadata)
        text = content.read_text(encoding="utf-8")
        snippet = bool(
            re.search(r"Live fetch failed|search-engine snippet|not the full page", text, re.I)
        )
        entries.append(
            {
                "page_id": page_id,
                "url": record.get("url"),
                "characters": len(text),
                "snippet_fallback": snippet,
                "full_text_candidate": not snippet and len(text.strip()) >= 150,
            }
        )
    original_urls = set()
    original_count = 0
    corrupt_count = 0
    records = list((pages / "archive/records").glob("*.json"))
    for path in records:
        record = read_json(path)
        original = record.get("original")
        if not isinstance(original, dict):
            continue
        # Archive records are untrusted input; only hashed atomic objects qualify.
        relative = str(original.get("path", ""))
        if not re.fullmatch(r"objects/[a-f0-9]{64}\.[a-z0-9]+", relative):
            corrupt_count += 1
            continue
        saved = pages / "archive" / relative
        if not saved.is_file():
            corrupt_count += 1
            continue
        payload = saved.read_bytes()
        if len(payload) != original.get("bytes") or hashlib.sha256(
            payload
        ).hexdigest() != original.get("sha256"):
            corrupt_count += 1
            continue
        original_count += 1
        original_urls.add(record.get("source_url", ""))
    return {
        "available": True,
        "saved_page_count": len(entries),
        "full_text_candidate_count": sum(page["full_text_candidate"] for page in entries),
        "snippet_fallback_page_count": sum(page["snippet_fallback"] for page in entries),
        "archive_record_count": len(records),
        "verified_original_record_count": original_count,
        "verified_original_unique_url_count": len(original_urls),
        "verified_original_urls": sorted(original_urls),
        "corrupt_original_record_count": corrupt_count,
        "pages": entries,
    }


def write_collection_quality_audit(output: Path, *, strict: bool = True) -> dict:
    """Write sidecars only; historical collection/run states remain untouched."""
    output = Path(output)
    checkpoint = (
        read_json(output / "checkpoint.json") if (output / "checkpoint.json").exists() else {}
    )
    entries = []
    for path in sorted(output.glob("*/result.json")):
        result = read_json(path)
        if not isinstance(result, dict):
            raise CollectionError(f"结果不是 JSON 对象：{path.parent.name}")
        audit = audit_content_quality(result, strict=strict)
        audit.update(
            task_id=path.parent.name,
            audited_at=now(),
            collection_status=checkpoint.get("tasks", {}).get(path.parent.name, {}).get("status"),
            saved_sources=audit_saved_sources(result),
            input_sha256={
                name: hashlib.sha256((path.parent / name).read_bytes()).hexdigest()
                for name in ("result.json", "report.md")
                if (path.parent / name).exists()
            },
        )
        atomic_json(path.parent / "quality-audit.json", audit)
        entries.append(audit)
    if not entries:
        raise CollectionError("没有可审计的本地 result.json；未启动研究")
    summary = {
        "version": 1,
        "policy": QUALITY_POLICY if strict else "nonempty-report-v1",
        "audited_at": now(),
        "read_only_inputs": True,
        "original_collection_states_preserved": True,
        "input_sha256": {
            name: hashlib.sha256((output / name).read_bytes()).hexdigest()
            for name in ("checkpoint.json", "manifest.json")
            if (output / name).exists()
        },
        "task_count": len(entries),
        "content_status": "partial"
        if any(item["issues"] for item in entries)
        else "meets_minimum_checks",
        "counts": {
            "completed_sessions": sum(item["session_status"] == "completed" for item in entries),
            "partial_content_tasks": sum(bool(item["issues"]) for item in entries),
            "traceable_fact_evidence": sum(
                item["traceable_fact_evidence_count"] for item in entries
            ),
            "agent_assertion_nodes": sum(item["agent_assertion_node_count"] for item in entries),
            "unanchored_source_candidates": sum(
                item["unanchored_source_candidate_count"] for item in entries
            ),
            "invalid_citation_occurrences": sum(
                item["invalid_citation_occurrences"] for item in entries
            ),
            "full_text_candidates_by_session": sum(
                item["saved_sources"].get("full_text_candidate_count", 0) for item in entries
            ),
            "full_text_candidate_unique_urls": len(
                {
                    page["url"]
                    for item in entries
                    for page in item["saved_sources"].get("pages", [])
                    if page["full_text_candidate"]
                }
            ),
            "snippet_fallback_pages_by_session": sum(
                item["saved_sources"].get("snippet_fallback_page_count", 0) for item in entries
            ),
            "verified_original_records_by_session": sum(
                item["saved_sources"].get("verified_original_record_count", 0) for item in entries
            ),
            "verified_original_unique_urls": len(
                {
                    url
                    for item in entries
                    for url in item["saved_sources"].get("verified_original_urls", [])
                }
            ),
        },
        "structured_row_counts": {
            key: sum(item["structured_row_counts"][key] for item in entries)
            for key in KNOWLEDGE_TABLES
        },
        "tasks": entries,
    }
    atomic_json(output / "quality-audit.json", summary)
    return summary


def build_payload(
    plan: dict,
    task: dict,
    inventory: str = "",
    max_time: int | None = None,
    extra_context: str = "",
):
    query = (
        f"{plan['common_prompt'].strip()}\n\n本批任务：{task['title']}\n\n{task['prompt'].strip()}"
    )
    if inventory:
        excerpt = inventory[:12000]
        truncation = "（原报告超过12000字，以下已明确截断）" if len(inventory) > 12000 else ""
        query += (
            "\n\n第一批库存报告，仅供发现线索；不是本批事实证据，必须重新查验原始来源。"
            + truncation
            + "\n---线索开始---\n"
            + excerpt
            + "\n---线索结束---"
        )
    if extra_context:
        excerpt = extra_context[:24000]
        truncation = "（已按24000字上限截断）" if len(extra_context) > 24000 else ""
        query += (
            "\n\n前序批次简报，仅供跨批发现和核对线索。"
            "它们不是独立证据；下列本地路径只作定位说明，本轮仍须读取原始公开来源核验。"
            + truncation
            + "\n---前序线索开始---\n"
            + excerpt
            + "\n---前序线索结束---"
        )
    return {
        "query": query,
        "engine": "openai",
        "modules": [],
        "sources": {"mode": "web"},
        "effort": task.get("effort", "medium"),
        "max_time": max_time if max_time is not None else task.get("max_time", 600),
    }


def validate_base_url(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        allowed = host == "localhost" or (host and ipaddress.ip_address(host).is_loopback)
        port = parsed.port
    except ValueError:
        allowed = False
        parsed = None
    if not parsed or parsed.scheme != "http" or not allowed or parsed.username or parsed.password:
        raise CollectionError("API 地址必须是无凭据的本机 loopback HTTP 地址")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or port == 0:
        raise CollectionError("API 地址不能包含路径、查询或片段")
    return base_url.rstrip("/")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalAPI:
    def __init__(self, base_url: str, timeout: float = 30):
        self.base_url = validate_base_url(base_url)
        self.timeout = timeout
        # A local request must not pass through environment proxies or follow
        # a redirect to a remote service. No retries, especially for POST.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method: str, path: str, payload=None):
        if not path.startswith("/api/"):
            raise CollectionError("只能调用本地 /api/ 接口")
        data = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        )
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.load(response)
            if not isinstance(result, (dict, list)):
                raise APIError()
            return result
        except HTTPError as exc:
            raise APIError(exc.code) from None
        except (
            URLError,
            TimeoutError,
            OSError,
            UnicodeError,
            ValueError,
            http.client.HTTPException,
        ):
            raise APIError() from None

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload):
        return self.request("POST", path, payload)


@contextmanager
def collection_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CollectionError("另一个采集驱动正在运行，请先等待它结束") from None
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class CollectionRunner:
    def __init__(
        self,
        plan: dict,
        output_dir: Path,
        client,
        *,
        max_time=None,
        poll_interval=5,
        retry_failed=False,
        strict_quality=None,
        wait_ingestion=False,
        ingestion_timeout=1800,
        clock=time.time,
        sleep=time.sleep,
    ):
        self.plan = validate_plan(plan)
        self.output = Path(output_dir)
        self.client = client
        self.max_time = max_time
        self.poll_interval = poll_interval
        self.retry_failed = retry_failed
        self.strict_quality = (
            requires_content_quality(plan) if strict_quality is None else strict_quality
        )
        self.wait_ingestion = wait_ingestion
        self.ingestion_timeout = ingestion_timeout
        self.clock, self.sleep = clock, sleep
        self.checkpoint_path = self.output / "checkpoint.json"
        self.output.mkdir(parents=True, exist_ok=True)
        digest = plan_hash(plan)
        if self.checkpoint_path.exists():
            self.state = read_json(self.checkpoint_path)
            if not isinstance(self.state, dict) or self.state.get("plan_hash") != digest:
                raise CollectionError("计划 hash 与已有进度不同；请使用新的 --output-dir")
            if not isinstance(self.state.get("tasks"), dict):
                raise CollectionError("进度文件格式无效；不会重新提交研究")
            expected_ids = {task["id"] for task in plan["tasks"]}
            if set(self.state["tasks"]) != expected_ids:
                raise CollectionError("进度文件的任务清单不完整；不会重新提交研究")
        else:
            self.state = {
                "version": 1,
                "title": plan.get("title", "Anduril collection"),
                "plan_hash": digest,
                "created_at": now(),
                "tasks": {},
            }
        for task in plan["tasks"]:
            self.state["tasks"].setdefault(
                task["id"],
                {"id": task["id"], "title": task["title"], "status": "pending", "attempt": 1},
            )
        self.save()

    def save(self):
        self.state["updated_at"] = now()
        atomic_json(self.checkpoint_path, self.state)
        self.write_manifest()

    def write_manifest(self):
        entries = []
        for task in self.plan["tasks"]:
            entry = dict(self.state["tasks"][task["id"]])
            entry["directory"] = str(self.output / task["id"])
            entries.append(entry)
        counts = {
            status: sum(item["status"] == status for item in entries)
            for status in sorted({item["status"] for item in entries})
        }
        archive_ok = all(item.get("backfill", {}).get("status") == "completed" for item in entries)
        if counts.get("completed") == len(entries) and archive_ok:
            status = "completed"
        elif any(item["status"] == "submission_unknown" for item in entries):
            status = "partial" if any(item["status"] in TERMINAL for item in entries) else "failed"
        elif any(item["status"] in {"running", "submitting", "pending"} for item in entries):
            status = "in_progress"
        elif any(item["status"] in {"completed", "partial", "timed_out"} for item in entries):
            status = "partial"
        else:
            status = "failed"
        if (
            self.wait_ingestion
            and status == "completed"
            and not all(
                item.get("knowledge_ingestion", {}).get("status") == "completed" for item in entries
            )
        ):
            status = "partial"
        manifest = {
            "version": 1,
            "title": self.state["title"],
            "plan_hash": self.state["plan_hash"],
            "status": status,
            "counts": counts,
            "updated_at": self.state["updated_at"],
            "tasks": entries,
        }
        atomic_json(self.output / "manifest.json", manifest)
        lines = [
            f"# {self.state['title']}",
            "",
            f"状态：{status}；更新时间：{self.state['updated_at']}",
            "",
            "| 批次 | 研究状态 | Session | 归档状态 | 资料数 | 报告 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for entry in entries:
            backfill = entry.get("backfill", {})
            lines.append(
                f"| {entry['id']} {entry['title']} | {entry['status']} | "
                f"{entry.get('session_id', '')} | {backfill.get('status', '')} | "
                f"{backfill.get('document_count', '未知')} | "
                f"[{entry['id']}]({entry['id']}/report.md) |"
            )
        lines.extend(
            [
                "",
                "第一批报告只作为后续发现线索；每批事实应回到其原始证据。",
                "failed/partial/timed_out 不代表采集成功；资料归档状态与研究状态分别记录。",
                "--resume 继续已有 session；失败批次仅在显式 --retry-failed 时重新研究。",
            ]
        )
        if self.state.get("driver_error"):
            lines.extend(["", f"驱动提示：{self.state['driver_error']}"])
        (self.output / "summary.md").write_text(redact("\n".join(lines) + "\n"), encoding="utf-8")

    def ensure_idle(self, own_session: str | None = None):
        history = self.client.get("/api/history")
        if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
            raise CollectionError("历史列表响应无效；不会提交研究")
        running = [
            item
            for item in history
            if item.get("status") == "running" and item.get("session_id") != own_session
        ]
        if running:
            raise CollectionError("发现其他正在运行的研究；本驱动已停止，不会干扰它")

    def check_settings(self):
        settings = self.client.get("/api/workbench/settings")
        openai = settings.get("openai", {}) if isinstance(settings, dict) else {}
        if not isinstance(openai, dict) or openai.get("model") != "gpt-4o":
            raise CollectionError("工作台 OpenAI 模型必须已设置为 gpt-4o；驱动不会修改设置")
        if openai.get("api_key_configured") is not True:
            raise CollectionError("工作台 OpenAI 尚未配置；驱动不会读取或写入密钥")
        if self.plan.get("search_provider") == "openai_web_search" and (
            settings.get("search", {}).get("provider") != "openai_web_search"
        ):
            raise CollectionError("本计划需要 OpenAI web_search，请在联网检索设置中选中它")

    def snapshot(self, task: dict, entry: dict, result: dict):
        directory = self.output / task["id"]
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / "result.json", result)
        answer = result.get("answer", "")
        if not isinstance(answer, str):
            answer = ""
        report = (
            f"# {task['title']}\n\n研究状态：{entry['status']}；"
            f"session_id：{entry.get('session_id', '')}\n\n"
            + (answer if answer.strip() else "尚未获得报告正文。")
        )
        (directory / "report.md").write_text(redact(report) + "\n", encoding="utf-8")

    def record_quality(self, task: dict, entry: dict, result: dict):
        audit = audit_content_quality(result, strict=self.strict_quality)
        audit.update(
            task_id=task["id"], audited_at=now(), saved_sources=audit_saved_sources(result)
        )
        atomic_json(self.output / task["id"] / "quality-audit.json", audit)
        entry["quality_audit"] = {
            key: audit[key]
            for key in (
                "policy",
                "content_status",
                "issues",
                "traceable_fact_evidence_count",
                "invalid_citation_occurrences",
                "agent_assertion_node_count",
            )
        }
        entry["quality_audit"]["path"] = f"{task['id']}/quality-audit.json"
        if (
            entry["status"] == "completed"
            and audit["issues"]
            and self.plan.get("knowledge_destination") != "entity_database"
        ):
            entry["status"] = "partial"
            entry["error"] = "API 已结束，但内容质量检查未通过；详见 quality-audit.json"

    def fetch_session(self, sid):
        path_id = quote(sid, safe="")
        try:
            result = self.client.get(f"/api/search/{path_id}")
        except APIError as exc:
            if exc.status != 404:
                raise
            result = self.client.get(f"/api/history/{path_id}")
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise CollectionError("研究状态响应无效；保留 session，使用 --resume 重查")
        return result

    def prior_context(self, task):
        index = next(
            i for i, candidate in enumerate(self.plan["tasks"]) if candidate["id"] == task["id"]
        )
        if index < 9:
            return ""
        records = []
        for previous in self.plan["tasks"][1:index]:
            path = self.output / previous["id"] / "result.json"
            if not path.exists():
                continue
            result = read_json(path)
            answer = result.get("answer") if isinstance(result, dict) else None
            if not isinstance(answer, str) or not answer.strip():
                continue
            entry = self.state["tasks"][previous["id"]]
            heading = (
                f"批次 {previous['id']}：{previous['title']}；状态：{entry['status']}；"
                f"session_id：{entry.get('session_id', '')}\n"
                f"本地报告定位：{path.parent / 'report.md'}\n"
            )
            records.append((heading, answer))
        if not records:
            return ""
        # Divide the bounded space across all reports instead of dropping the
        # most recent maturity/software batches when the collection grows.
        available = max(0, 24000 - sum(len(heading) + 20 for heading, _ in records))
        per_report = min(3000, available // len(records))
        context = "\n\n".join(
            heading
            + answer[:per_report]
            + ("\n[本批简报已截断]" if len(answer) > per_report else "")
            for heading, answer in records
        )
        return context[:24000]

    def submit(self, task, entry):
        self.ensure_idle()
        self.check_settings()
        first_id = self.plan["tasks"][0]["id"]
        inventory = ""
        if task["id"] != first_id:
            first_result = self.output / first_id / "result.json"
            if first_result.exists():
                data = read_json(first_result)
                if isinstance(data, dict) and isinstance(data.get("answer"), str):
                    inventory = data["answer"]
        payload = build_payload(
            self.plan, task, inventory, self.max_time, extra_context=self.prior_context(task)
        )
        atomic_json(self.output / task["id"] / "payload.json", payload)
        entry.update(
            status="submitting",
            submitted_at=now(),
            submitted_epoch=self.clock(),
            max_time=payload["max_time"],
        )
        self.save()  # A crash before receipt must never turn into an automatic POST retry.
        try:
            response = self.client.post("/api/search", payload)
        except BaseException as exc:
            # A 4xx is a definite rejection; all other outcomes may have created
            # a paid session. Do not inspect or copy an error response body.
            definite_rejection = (
                isinstance(exc, APIError)
                and exc.status is not None
                and 400 <= exc.status < 500
                and exc.status != 408
            )
            entry.update(
                status="failed" if definite_rejection else "submission_unknown",
                error="提交被拒绝"
                if definite_rejection
                else "提交结果未知，禁止自动重发；请到工作台历史核对",
            )
            self.snapshot(task, entry, {"status": entry["status"], "error": entry["error"]})
            self.save()
            if isinstance(exc, KeyboardInterrupt):
                raise
            if definite_rejection:
                return
            raise CollectionError(
                "提交结果未知，已停止；请先核对工作台历史，不要重新提交"
            ) from None
        sid = response.get("session_id") if isinstance(response, dict) else None
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", sid):
            entry.update(
                status="submission_unknown", error="提交响应缺少有效 session_id；禁止自动重发"
            )
            self.snapshot(task, entry, {"status": entry["status"], "error": entry["error"]})
            self.save()
            raise CollectionError(entry["error"])
        entry.update(status="running", session_id=sid)
        self.save()

    def track(self, task, entry):
        sid = entry["session_id"]
        deadline = entry["submitted_epoch"] + entry.get("max_time", task.get("max_time", 600)) + 180
        while True:
            self.ensure_idle(sid)
            result = self.fetch_session(sid)
            status = result["status"]
            entry["session_status"] = status
            if status == "completed":
                answer = result.get("answer")
                entry["status"] = (
                    "completed"
                    if self.plan.get("knowledge_destination") == "entity_database"
                    or (isinstance(answer, str) and answer.strip())
                    else "partial"
                )
                if entry["status"] == "partial":
                    entry["error"] = "API 已结束但没有报告正文"
            elif status not in ACTIVE:
                has_answer = isinstance(result.get("answer"), str) and bool(
                    result["answer"].strip()
                )
                entry["status"] = "partial" if has_answer else "failed"
                entry["error"] = (
                    "研究未成功完成；部分内容已保留" if has_answer else "研究未成功完成"
                )
            if status not in ACTIVE:
                self.record_quality(task, entry, result)
            self.snapshot(task, entry, result)
            self.save()
            if entry["status"] != "running":
                entry["finished_at"] = now()
                self.save()
                return
            if self.clock() >= deadline:
                if not entry.get("stop_requested_at"):
                    entry["stop_requested_at"] = now()
                    self.save()  # Send stop at most once, including across a crash.
                    try:
                        self.client.post(f"/api/search/{quote(sid, safe='')}/stop", {})
                        entry["stop_result"] = "requested"
                    except APIError:
                        entry["stop_result"] = "unknown"
                entry.update(
                    status="timed_out",
                    error="超过 max_time + 180 秒，已请求停止一次",
                    finished_at=now(),
                )
                try:
                    result = self.fetch_session(sid)
                except (CollectionError, OSError):
                    pass
                self.snapshot(task, entry, result)
                self.save()
                return
            self.sleep(min(self.poll_interval, max(0, deadline - self.clock())))

    def backfill(self, entry):
        sid = entry.get("session_id")
        if not sid:
            return
        archive = entry.setdefault("backfill", {})
        if archive.get("status") == "completed":
            return
        deadline = self.clock() + 300
        try:
            if archive.get("job_id") and archive.get("status") in {"queued", "running", "tracking"}:
                job = self.client.get(
                    "/api/local/research-materials/backfill/" + quote(archive["job_id"], safe="")
                )
            else:
                job = self.client.post(
                    "/api/local/research-materials/backfill", {"session_id": sid}
                )
            while True:
                if not isinstance(job, dict) or not isinstance(job.get("job_id"), str):
                    raise CollectionError("本地归档响应无效")
                archive.update(redact(job))
                self.save()
                if job.get("status") not in {"queued", "running"}:
                    break
                if self.clock() >= deadline:
                    archive.update(status="tracking", error="本地归档仍在后台，恢复时继续查询")
                    self.save()
                    return
                self.sleep(min(self.poll_interval, max(0, deadline - self.clock())))
                job = self.client.get(
                    "/api/local/research-materials/backfill/" + quote(job["job_id"], safe="")
                )
            materials = self.client.get(
                "/api/local/research-materials?" + urlencode({"session_id": sid})
            )
            documents = materials.get("documents") if isinstance(materials, dict) else None
            if not isinstance(documents, list):
                raise CollectionError("本地归档资料计数响应无效")
            archive["document_count"] = len(documents)
            if archive.get("error_count") or archive.get("error"):
                archive["status"] = (
                    "completed_with_errors"
                    if archive.get("status") == "completed"
                    else archive.get("status", "failed")
                )
        except (CollectionError, OSError):
            archive.update(status="failed", error="本地资料归档或计数未完成；研究不会因此重新提交")
        self.save()

    def ingest_archived(self, entry):
        """Wait for the archive-driven entity store, never parse a report as facts."""
        if not self.wait_ingestion or not entry.get("session_id"):
            return
        receipt = entry.setdefault("knowledge_ingestion", {})
        if receipt.get("status") == "completed":
            return
        archive = entry.get("backfill", {})
        if archive.get("status") != "completed" or not archive.get("document_count"):
            receipt.update(status="partial", message="没有可供抽取的已解析原文，未宣称入库完成。")
            self.save()
            return
        sid = entry["session_id"]
        deadline = self.clock() + self.ingestion_timeout
        try:
            self.client.post(
                "/api/knowledge/ingestion",
                {"session_id": sid, "engine": "openai", "model": "gpt-4o"},
            )
            while True:
                snapshot = self.client.get(
                    "/api/knowledge/ingestion?" + urlencode({"session_id": sid})
                )
                jobs = snapshot.get("jobs") if isinstance(snapshot, dict) else None
                if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
                    raise CollectionError("自动入库状态响应无效")
                counts = snapshot.get("counts", {})
                active = any(job.get("status") in {"queued", "running"} for job in jobs) or any(
                    counts.get(status, 0) for status in ("queued", "running")
                )
                receipt.update(
                    status="running" if active else "completed",
                    document_count=snapshot.get("total_jobs", len(jobs)),
                    counts=counts,
                    entities=sum(int(job.get("entities") or 0) for job in jobs),
                    relations=sum(int(job.get("relations") or 0) for job in jobs),
                    job_ids=[job["id"] for job in jobs if isinstance(job.get("id"), str)],
                )
                receipt["unprocessed_documents"] = max(
                    0, archive["document_count"] - receipt["document_count"]
                )
                if not active:
                    if (
                        not jobs
                        or receipt["unprocessed_documents"]
                        or any(job.get("status") != "completed" for job in jobs)
                        or any(counts.get(status, 0) for status in ("failed", "partial", "skipped"))
                    ):
                        receipt.update(
                            status="partial",
                            message="部分原文尚未形成完整入库记录，请查看自动入库。",
                        )
                    self.save()
                    print(
                        f"自动入库：{receipt['status']}，{receipt['entities']} 实体 / "
                        f"{receipt['relations']} 关系（按文档累计）",
                        flush=True,
                    )
                    return
                self.save()
                if self.clock() >= deadline:
                    receipt.update(
                        status="tracking", message="自动入库仍在后台；--resume 可继续跟踪。"
                    )
                    self.save()
                    return
                self.sleep(min(self.poll_interval, max(0, deadline - self.clock())))
        except (CollectionError, OSError):
            receipt.update(
                status="tracking",
                message="入库状态尚未确认；不会重复提交研究，请用 --resume 重查。",
            )
            self.save()

    def retry(self, task, entry):
        self.ensure_idle()  # A timed-out session can still be running if stop failed.
        attempt = entry.get("attempt", 1)
        directory = self.output / task["id"]
        destination = directory / "attempts" / str(attempt)
        destination.mkdir(parents=True, exist_ok=True)
        for filename in ("payload.json", "result.json", "report.md", "quality-audit.json"):
            source = directory / filename
            if source.exists():
                shutil.copy2(source, destination / filename)
        previous = dict(entry)
        previous.pop("attempts", None)
        attempts = [*entry.get("attempts", []), previous]
        atomic_json(destination / "attempt.json", previous)
        entry.clear()
        entry.update(
            id=task["id"],
            title=task["title"],
            status="pending",
            attempt=attempt + 1,
            attempts=attempts,
        )
        self.save()
        # The previous attempt is durable above. Do not let its report look
        # like output from a newly submitted session if the next poll fails.
        for filename in ("payload.json", "result.json", "report.md", "quality-audit.json"):
            (directory / filename).unlink(missing_ok=True)

    def run(self) -> int:
        self.state.pop("driver_error", None)
        try:
            for task in self.plan["tasks"]:
                entry = self.state["tasks"][task["id"]]
                status = entry["status"]
                if status in {"submitting", "submission_unknown"}:
                    entry["status"] = "submission_unknown"
                    self.save()
                    raise CollectionError(
                        "有一批提交结果未知；请到历史核对并人工填回 session_id 后恢复"
                    )
                if status in TERMINAL:
                    # Resuming a previously completed real collection also applies
                    # the content gate, without restarting or rewriting its result.
                    result_path = self.output / task["id"] / "result.json"
                    if self.strict_quality and result_path.exists():
                        self.record_quality(task, entry, read_json(result_path))
                        status = entry["status"]
                    if self.retry_failed and status in {"partial", "failed", "timed_out"}:
                        self.retry(task, entry)
                    else:
                        self.backfill(entry)
                        self.ingest_archived(entry)
                        continue
                if entry["status"] == "pending":
                    print(f"提交批次 {task['id']}：{task['title']}", flush=True)
                    self.submit(task, entry)
                if entry["status"] == "running":
                    if not entry.get("session_id") or not isinstance(
                        entry.get("submitted_epoch"), (int, float)
                    ):
                        raise CollectionError("运行中进度缺少 session 或提交时间；不会重复提交")
                    print(f"跟踪批次 {task['id']}：{entry['session_id']}", flush=True)
                    self.track(task, entry)
                self.backfill(entry)
                self.ingest_archived(entry)
                print(f"批次 {task['id']}：{entry['status']}", flush=True)
            self.save()
            return 0 if read_json(self.output / "manifest.json")["status"] == "completed" else 1
        except KeyboardInterrupt:
            self.state["driver_error"] = (
                "驱动已中断；后台研究保留。使用 --resume 继续，未取消研究。"
            )
            if any(
                entry["status"] == "submission_unknown" for entry in self.state["tasks"].values()
            ):
                self.state["driver_error"] += " 提交结果未知的批次须先到历史核对，禁止自动重发。"
            self.save()
            print(self.state["driver_error"], file=sys.stderr)
            return 130
        except (CollectionError, OSError) as exc:
            message = (
                str(exc) if isinstance(exc, CollectionError) else "本地文件读写失败；已保存进度"
            )
            self.state["driver_error"] = message
            self.save()
            print(message, file=sys.stderr)
            return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="明确授权按计划逐批启动研究")
    mode.add_argument("--resume", action="store_true", help="恢复已有进度，重用 session")
    mode.add_argument("--status", action="store_true", help="只显示本地进度，不请求 API")
    mode.add_argument("--dry-run", action="store_true", help="输出请求 payload，不请求 API")
    mode.add_argument(
        "--audit-quality",
        action="store_true",
        help="只审计本地结果并写独立 sidecar；不请求 API，不改原结果与进度",
    )
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--max-time", type=int, help="覆盖尚未提交批次的 max_time（秒）")
    parser.add_argument("--poll-interval", type=float, default=5)
    parser.add_argument("--request-timeout", type=float, default=30)
    parser.add_argument(
        "--strict-quality",
        action="store_true",
        help="为自定义计划启用 Anduril 证据质量检查（正式计划默认启用）",
    )
    parser.add_argument(
        "--wait-ingestion",
        action="store_true",
        help="逐批等待归档原文自动抽取入实体关系数据库；不从报告构造知识",
    )
    parser.add_argument(
        "--ingestion-timeout",
        type=int,
        default=1800,
        help="每批等待自动入库的秒数；超时保留后台任务",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="显式重试失败批次（会重新付费），保留旧 attempts",
    )
    parser.add_argument(
        "--build-knowledge",
        action="store_true",
        help="研究结束后规范化成果、核对公开项目元数据并导入本机资料库",
    )
    args = parser.parse_args(argv)
    try:
        validate_base_url(args.base_url)
        if args.max_time is not None and args.max_time < 1:
            raise CollectionError("--max-time 必须为正整数")
        if (
            not math.isfinite(args.poll_interval)
            or not math.isfinite(args.request_timeout)
            or args.poll_interval <= 0
            or args.request_timeout <= 0
        ):
            raise CollectionError("轮询间隔和请求超时必须为正数")
        if args.retry_failed and not (args.run or args.resume):
            raise CollectionError("--retry-failed 需要 --run 或 --resume")
        if args.build_knowledge and not (args.run or args.resume):
            raise CollectionError("--build-knowledge 需要 --run 或 --resume")
        if args.wait_ingestion and args.build_knowledge:
            raise CollectionError("--wait-ingestion 已直接入实体关系库，无需旧版 --build-knowledge")
        if args.ingestion_timeout < 1:
            raise CollectionError("入库等待时间必须为正整数秒")
        if args.status:
            checkpoint = args.output_dir / "checkpoint.json"
            if not checkpoint.exists():
                print("尚无本地进度。")
            else:
                state = read_json(checkpoint)
                print(json.dumps(redact(state), ensure_ascii=False, indent=2))
            return 0
        plan = load_plan(args.plan)
        if (
            plan.get("knowledge_destination") == "entity_database"
            and (args.run or args.resume)
            and not args.wait_ingestion
        ):
            raise CollectionError("数据库调查计划请加 --wait-ingestion，确保跟踪到实体关系入库状态")
        if args.audit_quality:
            audit = write_collection_quality_audit(
                args.output_dir, strict=args.strict_quality or requires_content_quality(plan)
            )
            print(
                json.dumps(
                    {
                        "content_status": audit["content_status"],
                        "counts": audit["counts"],
                        "audit_path": str(args.output_dir / "quality-audit.json"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1 if audit["content_status"] == "partial" else 0
        if args.dry_run:
            print(
                json.dumps(
                    [
                        {
                            "task_id": task["id"],
                            "payload": build_payload(plan, task, max_time=args.max_time),
                        }
                        for task in plan["tasks"]
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if not (args.run or args.resume):
            print(
                f"{plan.get('title', 'Anduril collection')}：{len(plan['tasks'])} 批；"
                "openai / gpt-4o / web"
            )
            for task in plan["tasks"]:
                print(
                    f"  {task['id']}  {task['title']}  "
                    f"({args.max_time or task.get('max_time', 600)} 秒)"
                )
            print("仅展示计划，未发送请求。--run 启动；--resume 续跑；--status 查看本地进度。")
            return 0
        if args.resume and not (args.output_dir / "checkpoint.json").exists():
            raise CollectionError("没有可恢复的 checkpoint；首次启动请用 --run")
        with collection_lock(ROOT / "data/collections/.anduril-collection.lock"):
            runner = CollectionRunner(
                plan,
                args.output_dir,
                LocalAPI(args.base_url, args.request_timeout),
                max_time=args.max_time,
                poll_interval=args.poll_interval,
                retry_failed=args.retry_failed,
                strict_quality=args.strict_quality or requires_content_quality(plan),
                wait_ingestion=args.wait_ingestion,
                ingestion_timeout=args.ingestion_timeout,
            )
            code = runner.run()
            if args.build_knowledge and code != 130:
                try:
                    # Imported only on this explicitly requested export path.
                    sys.path.insert(0, str(Path(__file__).resolve().parent))
                    from anduril_knowledge import build_knowledge

                    knowledge = build_knowledge(args.output_dir, args.base_url)
                    if isinstance(knowledge, dict):
                        atomic_json(args.output_dir / "knowledge-build.json", knowledge)
                        if knowledge.get("status") in {"failed", "partial", "error"}:
                            code = code or 1
                    print("已有成果的知识库构建已结束。")
                except KeyboardInterrupt:
                    print(
                        "知识库构建已中断；研究进度保留，使用 --resume --build-knowledge 继续。",
                        file=sys.stderr,
                    )
                    code = 130
                except Exception as exc:
                    # A dependency may include request headers in exceptions.
                    # Report the class only, never its unsanitized text.
                    atomic_json(
                        args.output_dir / "knowledge-build.json",
                        {"status": "failed", "error_type": type(exc).__name__},
                    )
                    print("已有成果的知识库构建未完成；研究进度已保留，可恢复。", file=sys.stderr)
                    code = code or 1
            print(f"本地汇总：{args.output_dir / 'summary.md'}")
            return code
    except (CollectionError, OSError) as exc:
        print(str(exc) if isinstance(exc, CollectionError) else "本地文件读写失败", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
