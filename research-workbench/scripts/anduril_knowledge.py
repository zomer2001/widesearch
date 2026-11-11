#!/usr/bin/env python3
"""Build a versioned, evidence-linked Anduril knowledge store from saved reports.

No model calls, private settings, downloads, or third-party code execution. The
optional repository check uses only unauthenticated public GitHub metadata. The
optional archive uses the loopback API's explicitly model-free upload mode.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import re
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

KINDS = (
    "products",
    "product_relations",
    "technologies",
    "technology_routes",
    "maturity_assessments",
    "reproduction_units",
    "evidence",
)
LIBRARY_NAME = "Anduril · 产品与技术路线"
RELATIONS = {
    "series",
    "series_of",
    "variant",
    "variant_of",
    "part_of",
    "replaces",
    "derived_from",
    "renamed_from",
    "successor_of",
    "member_of",
    "component_of",
}
STAGES = {
    "unknown",
    "concept",
    "lab_demo",
    "integrated_prototype",
    "field_demo",
    "production",
    "deployed",
}
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1] / "data/collections/anduril-products-technology"
)


def now():
    return datetime.now(UTC).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str | bytes):
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def write_json(path: Path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_text(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


class KnowledgeError(Exception):
    pass


class APIError(KnowledgeError):
    def __init__(self, status=None):
        self.status = status
        super().__init__(f"HTTP {status}" if status else "request_failed")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_base(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        allowed = host == "localhost" or bool(host and ipaddress.ip_address(host).is_loopback)
        port = parsed.port
    except ValueError:
        raise KnowledgeError("API 地址须为无凭据的本机 HTTP loopback 地址") from None
    if (
        parsed.scheme != "http"
        or not allowed
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise KnowledgeError("API 地址须为无凭据的本机 HTTP loopback 地址")
    return value.rstrip("/")


class LocalArtifactAPI:
    def __init__(self, base_url="http://127.0.0.1:3000", timeout=45):
        self.base_url = local_base(base_url)
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def _send(self, method, path, data=None, headers=None):
        if not path.startswith("/api/local/"):
            raise KnowledgeError("只允许本地资料库接口")
        request = Request(self.base_url + path, data=data, method=method, headers=headers or {})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.load(response)
            if not isinstance(result, (dict, list)):
                raise APIError()
            return result
        except HTTPError as exc:
            raise APIError(exc.code) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise APIError() from None

    def request(self, method, path, payload=None):
        return self._send(
            method,
            path,
            canonical(payload).encode("utf-8") if payload is not None else None,
            {"Content-Type": "application/json", "Accept": "application/json"},
        )

    def upload(self, path, filename, content):
        boundary = "anduril-" + uuid.uuid4().hex
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{safe_name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return self._send(
            "POST",
            path,
            body,
            {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
        )


class PublicGitHubAPI:
    """No tokens or proxies, no redirects; metadata is the only remote read."""

    def __init__(self, timeout=12):
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.timeout = timeout

    def get(self, path):
        if not path.startswith("/repos/") or "\r" in path or "\n" in path:
            raise KnowledgeError("无效 GitHub 元数据路径")
        request = Request(
            "https://api.github.com" + path,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "anduril-knowledge/1",
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                # GitHub's tree endpoint can be large. Never consume arbitrary blobs.
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise APIError()
                result = json.loads(raw)
            if not isinstance(result, dict):
                raise APIError()
            return result
        except HTTPError as exc:
            raise APIError(exc.code) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise APIError() from None


def http_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not any(ord(char) < 33 for char in value)
        )
    except ValueError:
        return False


def github_repo(value):
    if not http_url(value):
        return None
    parsed = urlsplit(value)
    if parsed.hostname.casefold() != "github.com" or parsed.query or parsed.fragment:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2:
        return None
    owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo)
        or repo in {".", ".."}
    ):
        return None
    return f"{owner}/{repo}"


class RepositoryVerifier:
    """At most four reads per repo and 40 per build, shared by all units.

    Cache keys are API paths, so different refs never borrow a previous unit's
    tree. Fixed commits/trees are immutable; mutable refs and metadata expire.
    """

    def __init__(self, cache_path, enabled=True, client=None):
        self.cache_path = Path(cache_path)
        self.enabled = enabled
        self.client = client
        self.cache = read_json(self.cache_path, {"version": 1, "responses": {}})
        if not isinstance(self.cache, dict) or not isinstance(self.cache.get("responses"), dict):
            raise KnowledgeError("GitHub 缓存损坏；未覆盖原缓存")
        self.counts = {}
        self.total = 0

    def get(self, repo, path, immutable=False):
        cached = self.cache["responses"].get(path)
        if isinstance(cached, dict):
            try:
                age = (
                    datetime.now(UTC) - datetime.fromisoformat(cached["checked_at"])
                ).total_seconds()
                if immutable or age < 86400:
                    return cached["response"]
            except (KeyError, ValueError, TypeError):
                pass
        if self.total >= 40 or self.counts.get(repo.casefold(), 0) >= 4:
            raise KnowledgeError("metadata_request_budget_exhausted")
        self.counts[repo.casefold()] = self.counts.get(repo.casefold(), 0) + 1
        self.total += 1
        if self.client is None:
            self.client = PublicGitHubAPI()
        response = self.client.get(path)
        self.cache["responses"][path] = {"checked_at": now(), "response": response}
        # Persist after every successful read; interruption cannot lose the budget-saving cache.
        write_json(self.cache_path, self.cache)
        return response

    def verify(self, unit):
        result = {
            "metadata_status": "metadata_unchecked",
            "execution_status": "not_run",
            "checks": [],
            "resolved_commit": None,
        }
        repo = github_repo(unit.get("repo_url"))
        if not self.enabled:
            result["checks"].append("repository_checks_disabled")
            return result
        if not repo:
            result["checks"].append("non_github_or_unlocatable_repository")
            return result
        prefix = "/repos/" + quote(repo, safe="/")
        try:
            metadata = self.get(repo, prefix)
            if str(metadata.get("full_name", "")).casefold() != repo.casefold():
                result["checks"].append("repository_identity_mismatch")
                return result
            result["repository"] = metadata.get("full_name")
            result["repository_url"] = metadata.get("html_url")
            license_info = metadata.get("license")
            result["license"] = (
                {key: license_info.get(key) for key in ("spdx_id", "name", "url")}
                if isinstance(license_info, dict)
                else None
            )
            if not result["license"] or result["license"].get("spdx_id") in {None, "NOASSERTION"}:
                result["checks"].append("license_not_confirmed")
            expected_license = unit.get("license")
            actual_license = (result.get("license") or {}).get("spdx_id")
            if (
                isinstance(expected_license, str)
                and expected_license
                and actual_license
                and expected_license.casefold() != actual_license.casefold()
            ):
                result["checks"].append("reported_license_differs_from_repository")
            requested_commit = unit.get("commit")
            ref = requested_commit or unit.get("ref")
            if not isinstance(ref, str) or not ref.strip():
                result["checks"].append("fixed_commit_or_ref_missing")
                result["metadata_status"] = "metadata_partial"
                return result
            if len(ref) > 256 or any(ord(char) < 32 for char in ref):
                result["checks"].append("invalid_commit_or_ref")
                result["metadata_status"] = "metadata_partial"
                return result
            fixed = bool(re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", ref))
            commit = self.get(repo, prefix + "/commits/" + quote(ref, safe=""), immutable=fixed)
            sha = commit.get("sha")
            if not isinstance(sha, str) or not re.fullmatch(
                r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", sha
            ):
                result["checks"].append("commit_not_confirmed")
                result["metadata_status"] = "metadata_partial"
                return result
            if fixed and sha.casefold() != ref.casefold():
                result["checks"].append("commit_mismatch")
                result["metadata_status"] = "metadata_partial"
                return result
            result["resolved_commit"] = sha
            paths = unit.get("code_paths")
            if not isinstance(paths, list) or not paths:
                result["checks"].append("code_paths_missing")
                result["metadata_status"] = "metadata_partial"
                return result
            safe_paths = [
                path
                for path in paths
                if isinstance(path, str)
                and path.strip()
                and not path.startswith("/")
                and ".." not in PurePosixPath(path).parts
                and "\\" not in path
                and not any(ord(char) < 32 for char in path)
            ]
            if len(safe_paths) != len(paths):
                result["checks"].append("invalid_code_paths")
            tree = self.get(repo, prefix + "/git/trees/" + sha + "?recursive=1", immutable=True)
            entries = {
                entry.get("path")
                for entry in tree.get("tree", [])
                if isinstance(entry, dict) and isinstance(entry.get("path"), str)
            }
            result["code_path_checks"] = [
                {"path": path, "exists": path.rstrip("/") in entries} for path in safe_paths
            ]
            if tree.get("truncated"):
                result["checks"].append("repository_tree_truncated")
            if any(not item["exists"] for item in result["code_path_checks"]):
                result["checks"].append("code_paths_not_confirmed")
            result["metadata_status"] = (
                "metadata_partial" if result["checks"] else "metadata_verified"
            )
        except (APIError, KnowledgeError) as exc:
            result["checks"].append(
                f"github_http_{exc.status}"
                if isinstance(exc, APIError) and exc.status
                else "metadata_request_budget_exhausted"
                if str(exc) == "metadata_request_budget_exhausted"
                else "metadata_request_failed"
            )
            result["metadata_status"] = (
                "metadata_partial" if result.get("repository") else "metadata_unchecked"
            )
        return result


def extract_structured(report):
    """A damaged final block is never replaced by an earlier, valid example."""
    starts = list(re.finditer(r"^[ \t]*```json[ \t]*\r?$", report, re.M | re.I))
    if not starts:
        return None, ["structured_json_missing"]
    start = starts[-1]
    closing = re.search(r"^[ \t]*```[ \t]*\r?$", report[start.end() :], re.M)
    if not closing:
        return None, ["structured_json_unclosed"]
    raw = report[start.end() : start.end() + closing.start()].strip()

    def reject_nonfinite(value):
        raise ValueError("Non-finite numbers are not JSON")

    try:
        payload = json.loads(raw, parse_constant=reject_nonfinite)
    except ValueError:
        return None, ["structured_json_invalid"]
    if not isinstance(payload, dict):
        return None, ["structured_json_not_object"]
    issues = []
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        issues.append("schema_version_not_1")
    for kind in KINDS:
        if not isinstance(payload.get(kind), list):
            issues.append(f"missing_or_invalid_section:{kind}")
    return payload, issues


def batch_inputs(output):
    manifest = read_json(output / "manifest.json", {})
    tasks = manifest.get("tasks", []) if isinstance(manifest, dict) else []
    if not isinstance(tasks, list):
        raise KnowledgeError("批次清单 tasks 格式无效")
    entries = {}
    for task in tasks:
        if isinstance(task, dict) and isinstance(task.get("id"), str):
            entries[task["id"]] = task
    for path in sorted(output.glob("*/report.md")):
        entries.setdefault(path.parent.name, {"id": path.parent.name})
    return list(entries.values())


def collect_records(output, verifier):
    batches, records, documents = [], [], []
    for task in batch_inputs(output):
        batch = task["id"]
        session = task.get("session_id")
        info = {
            "batch": batch,
            "source_session": session if isinstance(session, str) else None,
            "issues": [],
        }
        if session is not None and not isinstance(session, str):
            info["issues"].append("invalid_source_session")
        batches.append(info)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", batch):
            info["issues"].append("invalid_batch_id")
            continue
        directory = output / batch
        if directory.is_symlink() or directory.resolve().parent != output.resolve():
            info["issues"].append("batch_path_outside_output")
            continue
        path = directory / "report.md"
        if path.is_symlink():
            info["issues"].append("report_symlink_rejected")
            continue
        if not path.is_file():
            info["issues"].append("report_missing")
            continue
        try:
            report = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            info["issues"].append("report_unreadable")
            continue
        if not info["source_session"] and (directory / "result.json").is_file():
            try:
                result = read_json(directory / "result.json", {})
                if isinstance(result, dict) and isinstance(result.get("session_id"), str):
                    info["source_session"] = result["session_id"]
            except (ValueError, OSError):
                info["issues"].append("result_json_invalid")
        if task.get("status") and task["status"] != "completed":
            info["issues"].append("research_status:" + str(task["status"]))
        report_sha = digest(report)
        documents.append(
            {
                "batch": batch,
                "source_session": info["source_session"],
                "content": report,
                "sha256": report_sha,
                "path": str(path),
            }
        )
        data, issues = extract_structured(report)
        info["issues"].extend(issues)
        if data is None:
            continue
        for kind in KINDS:
            if not isinstance(data.get(kind), list):
                continue
            for index, raw in enumerate(data[kind]):
                if (
                    not isinstance(raw, dict)
                    or not isinstance(raw.get("id"), str)
                    or not raw["id"].strip()
                ):
                    info["issues"].append(f"invalid_record:{kind}[{index}]")
                    continue
                payload = dict(raw)
                verification = {}
                if kind == "reproduction_units":
                    verification = verifier.verify(payload)
                    if payload.get("execution_status") != "not_run":
                        payload["source_execution_claim"] = payload.get("execution_status")
                    payload["execution_status"] = "not_run"
                    # Preserve author-supplied commit in source_payload; pin a missing commit
                    # only when GitHub independently returned the fixed object.
                    if verification.get("resolved_commit"):
                        payload["commit"] = verification["resolved_commit"]
                    checked_license = verification.get("license") or {}
                    if not payload.get("license") and checked_license.get("spdx_id") not in {
                        None,
                        "NOASSERTION",
                    }:
                        payload["license"] = checked_license["spdx_id"]
                    payload["repository_verification"] = verification
                records.append(
                    {
                        "kind": kind,
                        "id": raw["id"],
                        "batch": batch,
                        "source_session": info["source_session"],
                        "payload": payload,
                        "source_payload": raw,
                        "payload_sha256": digest(canonical(raw)),
                        "report_sha256": report_sha,
                        "issues": [],
                        "verification": verification,
                    }
                )
        info["record_count"] = sum(record["batch"] == batch for record in records)
        if not info["record_count"]:
            info["issues"].append("no_structured_records")
    return batches, records, documents


def validate_records(records):
    by_kind = {kind: set() for kind in KINDS}
    for record in records:
        by_kind[record["kind"]].add(record["id"])
    all_ids = set().union(*by_kind.values())
    link_fields = {
        "product_ids": "products",
        "technology_ids": "technologies",
        "route_ids": "technology_routes",
    }
    for record in records:
        item, issues, kind = record["payload"], record["issues"], record["kind"]
        if kind == "evidence":
            if not http_url(item.get("source_url")):
                issues.append("invalid_evidence_source_url")
            if item.get("origin_url") is not None and not http_url(item["origin_url"]):
                issues.append("invalid_evidence_origin_url")
            if not isinstance(item.get("quote"), str) or not item["quote"].strip():
                issues.append("evidence_quote_missing")
            if not item.get("location"):
                issues.append("evidence_location_missing")
            record["validation_status"] = "candidate" if issues else "evidence_linked"
            continue
        evidence_ids = item.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            issues.append("evidence_ids_missing")
        else:
            for evidence_id in evidence_ids:
                if not isinstance(evidence_id, str) or evidence_id not in by_kind["evidence"]:
                    issues.append("evidence_not_found:" + str(evidence_id))
        for field, target in link_fields.items():
            if field in item and not isinstance(item[field], list):
                issues.append("invalid_reference_list:" + field)
            elif isinstance(item.get(field), list):
                for target_id in item[field]:
                    if not isinstance(target_id, str) or target_id not in by_kind[target]:
                        issues.append(f"reference_not_found:{field}:{target_id}")
        if kind == "products" and item.get("parent_id") == item.get("id"):
            issues.append("self_referential_parent_product")
        if kind == "products" and item.get("parent_id") is not None:
            if (
                not isinstance(item["parent_id"], str)
                or item["parent_id"] not in by_kind["products"]
            ):
                issues.append("parent_product_not_found:" + str(item["parent_id"]))
        if kind == "product_relations":
            if not isinstance(item.get("relation"), str) or item["relation"] not in RELATIONS:
                issues.append("unknown_product_relation")
            if item.get("from_id") == item.get("to_id"):
                issues.append("self_referential_product_relation")
            for field in ("from_id", "to_id"):
                if not isinstance(item.get(field), str) or item[field] not in by_kind["products"]:
                    issues.append("relation_product_not_found:" + str(item.get(field)))
        if kind == "maturity_assessments":
            if not isinstance(item.get("stage"), str) or item["stage"] not in STAGES:
                issues.append("maturity_stage_unknown_or_missing")
            if not isinstance(item.get("subject_id"), str) or item["subject_id"] not in all_ids:
                issues.append("maturity_subject_not_found")
            if not isinstance(item.get("assessment_type"), str) or item["assessment_type"] not in {
                "source_reported",
                "inferred",
            }:
                issues.append("maturity_assessment_type_missing")
            if item.get("assessment_type") == "source_reported" and not item.get("source_stage"):
                issues.append("source_reported_maturity_text_missing")
            if item.get("trl") is not None and not item.get("basis"):
                issues.append("trl_basis_missing")
            if item.get("assessment_type") == "inferred":
                issues.append("inferred_maturity_not_source_reported")
                if not item.get("basis"):
                    issues.append("inferred_maturity_basis_missing")
        if kind == "reproduction_units":
            for field in (
                "repo_url",
                "route_ids",
                "code_paths",
                "inputs",
                "outputs",
                "steps",
                "acceptance_criteria",
                "limitations",
            ):
                if not item.get(field):
                    issues.append("reproduction_field_missing:" + field)
            if not http_url(item.get("repo_url")):
                issues.append("invalid_repository_url")
            if item.get("relationship") != "independent_reference":
                issues.append("independent_reference_relationship_not_explicit")
            if record["verification"].get("metadata_status") != "metadata_verified":
                issues.append("repository_metadata_unverified")
        record["validation_status"] = "candidate" if issues else "evidence_linked"
    # Validate evidence first, irrespective of model array order, and then propagate its gaps.
    invalid_evidence = {r["id"] for r in records if r["kind"] == "evidence" and r["issues"]}
    valid_evidence = {r["id"] for r in records if r["kind"] == "evidence" and not r["issues"]}
    for record in records:
        refs = record["payload"].get("evidence_ids", [])
        if isinstance(refs, list):
            for ref in refs:
                if isinstance(ref, str) and ref in invalid_evidence - valid_evidence:
                    marker = "evidence_invalid:" + ref
                    if marker not in record["issues"]:
                        record["issues"].append(marker)
                        record["validation_status"] = "candidate"
    grouped = {}
    for record in records:
        grouped.setdefault((record["kind"], record["id"]), []).append(record)
    conflicts = []
    for (kind, item_id), versions in grouped.items():
        if len({r["payload_sha256"] for r in versions}) > 1:
            conflicts.append(
                {
                    "kind": kind,
                    "id": item_id,
                    "versions": [
                        {
                            "batch": r["batch"],
                            "source_session": r["source_session"],
                            "sha256": r["payload_sha256"],
                        }
                        for r in versions
                    ],
                }
            )
            for record in versions:
                record["issues"].append("conflicting_record_versions")
                record["validation_status"] = "candidate"
    conflicting_evidence = {c["id"] for c in conflicts if c["kind"] == "evidence"}
    for record in records:
        refs = record["payload"].get("evidence_ids", [])
        if isinstance(refs, list) and any(
            isinstance(ref, str) and ref in conflicting_evidence for ref in refs
        ):
            record["issues"].append("conflicting_evidence_versions")
            record["validation_status"] = "candidate"
    return conflicts


def save_sqlite(path, records, documents):
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS records (
                record_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, id TEXT NOT NULL,
                batch TEXT NOT NULL, source_session TEXT, payload TEXT NOT NULL,
                source_payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                report_sha256 TEXT NOT NULL, validation_status TEXT NOT NULL,
                issues TEXT NOT NULL, first_seen TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(kind, id, batch, payload_sha256)
            );
            CREATE TABLE IF NOT EXISTS documents (
                document_id INTEGER PRIMARY KEY, batch TEXT NOT NULL, source_session TEXT,
                content TEXT NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL,
                first_seen TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(batch, sha256)
            );
            CREATE TABLE IF NOT EXISTS record_sources (
                record_id INTEGER NOT NULL REFERENCES records(record_id),
                source_session TEXT NOT NULL DEFAULT '', report_sha256 TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                UNIQUE(record_id,source_session,report_sha256)
            );
            CREATE INDEX IF NOT EXISTS records_lookup ON records(kind,id,batch);
        """)
        typed_fields = {
            "products": ("name", "kind", "parent_id", "status"),
            "product_relations": ("from_id", "relation", "to_id", "valid_time"),
            "technologies": ("name", "mechanism", "adoption_status"),
            "technology_routes": ("name", "mechanism", "adoption_status"),
            "maturity_assessments": (
                "subject_id",
                "assessment_scope",
                "stage",
                "source_stage",
                "assessment_type",
                "trl",
                "basis",
            ),
            "reproduction_units": (
                "name",
                "repo_url",
                "ref",
                "commit",
                "license",
                "execution_status",
            ),
            "evidence": ("source_url", "title", "quote", "location", "evidence_status"),
        }
        for kind, fields in typed_fields.items():
            columns = ", ".join(
                f"json_extract(payload, '$.{field}') AS \""
                + (field if field != "kind" else "product_kind")
                + '"'
                for field in fields
            )
            connection.execute(
                f"CREATE VIEW IF NOT EXISTS {kind} AS SELECT records.*, {columns} "
                f"FROM records WHERE kind='{kind}'"
            )
        with connection:
            connection.execute("UPDATE records SET active=0")
            connection.execute("UPDATE documents SET active=0")
            for record in records:
                connection.execute(
                    """
                    INSERT INTO records(kind,id,batch,source_session,payload,source_payload,
                                        payload_sha256,report_sha256,validation_status,issues,first_seen)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(kind,id,batch,payload_sha256) DO UPDATE SET
                        active=1, payload=excluded.payload, source_session=excluded.source_session,
                        report_sha256=excluded.report_sha256,
                        validation_status=excluded.validation_status, issues=excluded.issues
                    """,
                    (
                        record["kind"],
                        record["id"],
                        record["batch"],
                        record["source_session"],
                        canonical(record["payload"]),
                        canonical(record["source_payload"]),
                        record["payload_sha256"],
                        record["report_sha256"],
                        record["validation_status"],
                        canonical(record["issues"]),
                        now(),
                    ),
                )
                row_id = connection.execute(
                    "SELECT record_id FROM records WHERE kind=? AND id=? AND batch=? "
                    "AND payload_sha256=?",
                    (record["kind"], record["id"], record["batch"], record["payload_sha256"]),
                ).fetchone()[0]
                connection.execute(
                    "INSERT OR IGNORE INTO record_sources"
                    "(record_id,source_session,report_sha256,first_seen) VALUES(?,?,?,?)",
                    (row_id, record["source_session"] or "", record["report_sha256"], now()),
                )
            for doc in documents:
                connection.execute(
                    """INSERT INTO documents(batch,source_session,content,sha256,path,first_seen)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(batch,sha256) DO UPDATE SET active=1,
                    source_session=excluded.source_session, path=excluded.path""",
                    (
                        doc["batch"],
                        doc["source_session"],
                        doc["content"],
                        doc["sha256"],
                        doc["path"],
                        now(),
                    ),
                )
        connection.row_factory = sqlite3.Row
        rows = []
        for row in connection.execute("SELECT * FROM records ORDER BY kind,id,batch,record_id"):
            entry = dict(row)
            for field in ("payload", "source_payload", "issues"):
                entry[field] = json.loads(entry[field])
            entry["active"] = bool(entry["active"])
            entry["sources"] = [
                dict(source)
                for source in connection.execute(
                    "SELECT source_session,report_sha256 FROM record_sources WHERE record_id=? "
                    "ORDER BY source_session,report_sha256",
                    (entry["record_id"],),
                )
            ]
            rows.append(entry)
        # Complete the WAL into the deliverable; SQLite + sidecar is not a portable handoff.
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return rows
    finally:
        connection.close()


def cell(value):
    return str(value if value is not None else "未知").replace("|", "\\|").replace("\n", " ")


def reference_text(value):
    return ", ".join(map(str, value)) if isinstance(value, list) else cell(value)


def make_summary(records, batches, conflicts):
    counts = {kind: sum(r["kind"] == kind for r in records) for kind in KINDS}
    candidate_count = sum(bool(r["issues"]) for r in records)
    lines = [
        "# Anduril · 产品与技术路线",
        "",
        "从逐批报告构建；所有版本保留在 knowledge.sqlite 与 knowledge.jsonl。",
        "证据关联通过不代表事实已独立验证；候选记录保留原文与缺口。成熟度来源原话与分析判断分列。",
        "独立开源单元仅检查公开仓库元数据；执行状态统一为 not_run。"
        "仓库可访问不代表复现成功，也不代表 Anduril 使用该代码。",
        "",
        f"当前批次 {len(batches)}；当前记录 {len(records)}；"
        f"候选记录 {candidate_count}；冲突ID {len(conflicts)}。",
        "",
        "| 类别 | 当前记录 |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {kind} | {count} |" for kind, count in counts.items())
    missing = [kind for kind, count in counts.items() if not count]
    if missing:
        lines.extend(["", "缺少知识类别（本次构建为 partial）：" + ", ".join(missing) + "。"])
    sections = [
        (
            "产品谱系",
            "products",
            ("产品 / ID", "父产品", "状态", "批次 / session", "校验"),
            lambda p: (f"{p.get('name', '')} / {p.get('id')}", p.get("parent_id"), p.get("status")),
        ),
        (
            "谱系关系",
            "product_relations",
            ("起点", "关系", "终点", "批次 / session", "校验"),
            lambda p: (p.get("from_id"), p.get("relation"), p.get("to_id")),
        ),
        (
            "技术机制",
            "technologies",
            ("技术 / ID", "机制", "采用依据", "批次 / session", "校验"),
            lambda p: (
                f"{p.get('name', '')} / {p.get('id')}",
                p.get("mechanism"),
                p.get("adoption_status"),
            ),
        ),
        (
            "技术路线",
            "technology_routes",
            ("路线 / ID", "机制与取舍", "产品关联", "批次 / session", "校验"),
            lambda p: (
                f"{p.get('name', '')} / {p.get('id')}",
                str(p.get("mechanism", "")) + "；" + str(p.get("tradeoffs", [])),
                reference_text(p.get("product_ids", [])),
            ),
        ),
        (
            "成熟度",
            "maturity_assessments",
            ("对象 / 范围", "判断阶段 / 类型", "来源原话 / TRL", "批次 / session", "校验"),
            lambda p: (
                f"{p.get('subject_id')} / {p.get('assessment_scope')}",
                f"{p.get('stage')} / {p.get('assessment_type')}",
                f"{p.get('source_stage') or '未知'} / "
                f"{p.get('trl') if p.get('trl') is not None else '未知'}",
            ),
        ),
        (
            "独立开源复现单元",
            "reproduction_units",
            ("单元 / 路线", "仓库 / 固定commit", "元数据 / 执行", "批次 / session", "校验"),
            lambda p: (
                f"{p.get('name', p.get('id'))} / {p.get('route_ids', [])}",
                f"{p.get('repo_url')} / {p.get('commit') or '未固定'}",
                f"{p.get('repository_verification', {}).get('metadata_status')} / not_run",
            ),
        ),
    ]
    for title, kind, headings, values in sections:
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| " + " | ".join(headings) + " |",
                "| " + " | ".join("---" for _ in headings) + " |",
            ]
        )
        for record in records:
            if record["kind"] == kind:
                row = (
                    *values(record["payload"]),
                    f"{record['batch']} / {record['source_session'] or '未知'}",
                    record["validation_status"],
                )
                lines.append("| " + " | ".join(map(cell, row)) + " |")
    lines.extend(["", "## 证据索引", ""])
    for record in records:
        if record["kind"] == "evidence":
            p = record["payload"]
            url = p.get("source_url")
            link = f"<{url}>" if http_url(url) else "无有效 http(s) URL"
            lines.append(
                f"- {cell(record['id'])} · {cell(p.get('title'))} · {link} · "
                f"{cell(p.get('location'))} · {cell(record['batch'])}"
            )
    lines.extend(["", "## 仍需补齐", ""])
    for batch in batches:
        if batch["issues"]:
            lines.append(f"- 批次 {batch['batch']}：{', '.join(batch['issues'])}")
    for record in records:
        if record["issues"]:
            lines.append(
                f"- {cell(record['id'])}（{record['batch']}）：{', '.join(record['issues'])}"
            )
    if not batches:
        lines.append("- 尚无报告批次；不能把空产物视为研究完成。")
    if not missing and not any(b["issues"] for b in batches) and not candidate_count and batches:
        lines.append("- 当前未发现结构与引用缺口；事实判断仍以证据原文及适用条件为准。")
    return "\n".join(lines) + "\n"


def api_request(client, method, path, payload=None):
    if hasattr(client, "request"):
        return client.request(method, path, payload)
    return (
        getattr(client, method.casefold())(path, payload)
        if payload is not None
        else getattr(client, method.casefold())(path)
    )


def archive_artifacts(output, client, documents):
    ledger_path = output / "manifest/upload-ledger.json"
    ledger = read_json(ledger_path, {"version": 1, "artifacts": {}})
    if not isinstance(ledger, dict) or not isinstance(ledger.get("artifacts"), dict):
        raise KnowledgeError("上传清单损坏，未覆盖以免复活已删除文档")
    result = {
        "status": "completed",
        "uploaded": 0,
        "deduplicated": 0,
        "deleted_skipped": 0,
        "failures": [],
        "library_id": ledger.get("library_id"),
    }
    try:
        capabilities = api_request(client, "GET", "/api/local/capabilities")
        if not isinstance(capabilities, dict) or capabilities.get("local_only_upload") is not True:
            raise KnowledgeError("model_free_upload_not_supported")
        libraries = api_request(client, "GET", "/api/local/libraries")
        if not isinstance(libraries, list) or any(not isinstance(item, dict) for item in libraries):
            raise APIError()
        library_id = ledger.get("library_id")
        if library_id and library_id not in {lib.get("library_id") for lib in libraries}:
            ledger["library_deleted"] = True
            write_json(ledger_path, ledger)
            return {**result, "status": "deleted_skipped", "reason": "user_deleted_library"}
        if ledger.get("library_deleted"):
            return {**result, "status": "deleted_skipped", "reason": "user_deleted_library"}
        if not library_id:
            matching = [lib for lib in libraries if lib.get("name") == LIBRARY_NAME]
            if len(matching) > 1:
                raise KnowledgeError("multiple_named_libraries")
            library = (
                matching[0]
                if matching
                else api_request(client, "POST", "/api/local/libraries", {"name": LIBRARY_NAME})
            )
            library_id = library.get("library_id")
            if not isinstance(library_id, str) or not re.fullmatch(r"lib_[a-f0-9]+", library_id):
                raise APIError()
            ledger["library_id"] = library_id
            write_json(ledger_path, ledger)
        result["library_id"] = library_id
        live_documents = api_request(client, "GET", f"/api/local/libraries/{library_id}/documents")
        if not isinstance(live_documents, list) or any(
            not isinstance(doc, dict) for doc in live_documents
        ):
            raise APIError()
        live_ids = {doc.get("document_id") for doc in live_documents}
        paths = [(f"{doc['batch']}/report.md", Path(doc["path"])) for doc in documents]
        paths += [(name, output / name) for name in ("knowledge-summary.md", "knowledge.jsonl")]
        for key, path in paths:
            content = path.read_bytes()
            sha = digest(content)
            entry = ledger["artifacts"].setdefault(key, {"versions": []})
            if not isinstance(entry, dict) or not isinstance(entry.get("versions"), list):
                raise KnowledgeError("invalid_upload_artifact_ledger")
            if any(
                item.get("document_id") not in live_ids
                for item in entry["versions"]
                if item.get("document_id")
            ):
                entry["deleted"] = True
                entry["deletion_detected_at"] = entry.get("deletion_detected_at") or now()
            if entry.get("deleted"):
                result["deleted_skipped"] += 1
                write_json(ledger_path, ledger)
                continue
            previous = next((item for item in entry["versions"] if item.get("sha256") == sha), None)
            if previous:
                result["deduplicated"] += 1
                if previous.get("parse_status") == "failed":
                    result["failures"].append({"artifact": key, "reason": "saved_but_parse_failed"})
                continue
            name = "anduril-" + key.replace("/", "-")
            try:
                document = client.upload(
                    f"/api/local/libraries/{library_id}/uploads?local_only=true", name, content
                )
                if not isinstance(document, dict) or not document.get("document_id"):
                    raise APIError()
                # API contract: local-only must never schedule enrichment.
                if document.get("index_job") is not None:
                    raise KnowledgeError("local_only_contract_violated")
                entry["versions"].append(
                    {
                        "sha256": sha,
                        "document_id": document["document_id"],
                        "uploaded_at": now(),
                        "parse_status": document.get("parse_status"),
                        "deduplicated": bool(document.get("deduplicated")),
                    }
                )
                live_ids.add(document["document_id"])
                result["uploaded"] += 1
                if document.get("parse_status") == "failed":
                    result["failures"].append({"artifact": key, "reason": "saved_but_parse_failed"})
            except APIError as exc:
                if exc.status == 409:
                    entry["deleted"] = True
                    entry["deletion_detected_at"] = now()
                    result["deleted_skipped"] += 1
                else:
                    result["failures"].append({"artifact": key, "http_status": exc.status})
            write_json(ledger_path, ledger)
    except (APIError, KnowledgeError, OSError) as exc:
        result["failures"].append(
            {"reason": str(exc) if isinstance(exc, KnowledgeError) else "local_io_failed"}
        )
    if result["failures"]:
        result["status"] = "partial"
    return result


@contextmanager
def knowledge_lock(output):
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".knowledge-build.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def build_knowledge(
    output_dir: Path,
    api_base="http://127.0.0.1:3000",
    *,
    upload=True,
    verify_repos=True,
    client=None,
    repo_client=None,
) -> dict:
    """Merge saved batches; never start or repeat a research session.

    Offline validation: upload=False, verify_repos=False. Inject clients for tests.
    Record and report history is append-only; active marks the current on-disk input.
    """
    output = Path(output_dir).resolve()
    with knowledge_lock(output):
        verifier = RepositoryVerifier(
            output / "manifest/github-cache.json", verify_repos, repo_client
        )
        batches, records, documents = collect_records(output, verifier)
        conflicts = validate_records(records)
        for batch in batches:
            batch["candidate_count"] = sum(
                bool(r["issues"]) for r in records if r["batch"] == batch["batch"]
            )
            batch["status"] = (
                "partial" if batch["issues"] or batch["candidate_count"] else "completed"
            )
        missing_categories = [kind for kind in KINDS if not any(r["kind"] == kind for r in records)]
        data_status = (
            "partial"
            if missing_categories
            or not batches
            or conflicts
            or any(b["issues"] for b in batches)
            or any(r["issues"] for r in records)
            else "completed"
        )
        rows = save_sqlite(output / "knowledge.sqlite", records, documents)
        write_text(output / "knowledge.jsonl", "".join(canonical(row) + "\n" for row in rows))
        write_text(output / "knowledge-summary.md", make_summary(records, batches, conflicts))
        validation = {
            "schema_version": 1,
            "status": data_status,
            "batches": batches,
            "record_issues": [
                {"kind": r["kind"], "id": r["id"], "batch": r["batch"], "issues": r["issues"]}
                for r in records
                if r["issues"]
            ],
            "conflicts": conflicts,
            "no_reports": not bool(batches),
            "missing_categories": missing_categories,
            "repository_requests": verifier.total,
            "execution_status": "not_run",
        }
        write_json(output / "manifest/validation.json", validation)
        archive = (
            archive_artifacts(output, client or LocalArtifactAPI(api_base), documents)
            if upload
            else {"status": "skipped"}
        )
        status = (
            "partial" if data_status == "partial" or archive["status"] == "partial" else "completed"
        )
        result = {
            "schema_version": 1,
            "status": status,
            "data_status": data_status,
            "counts": {kind: sum(r["kind"] == kind for r in records) for kind in KINDS},
            "record_versions": len(rows),
            "batch_count": len(batches),
            "missing_categories": missing_categories,
            "archive": archive,
            "artifacts": {
                name: str(output / name)
                for name in (
                    "knowledge.sqlite",
                    "knowledge.jsonl",
                    "knowledge-summary.md",
                    "manifest/validation.json",
                )
            },
            "execution_status": "not_run",
        }
        write_json(output / "knowledge-manifest.json", result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--no-upload", action="store_true", help="仅生成本地知识产物，不调用资料库")
    parser.add_argument(
        "--no-repo-check", action="store_true", help="不请求GitHub；元数据标为未核验"
    )
    args = parser.parse_args(argv)
    try:
        result = build_knowledge(
            args.output_dir,
            args.base_url,
            upload=not args.no_upload,
            verify_repos=not args.no_repo_check,
        )
    except (KnowledgeError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"知识构建未完成（{type(exc).__name__}），已有研究文件保留。", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
