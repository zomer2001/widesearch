"""Export only selected business-library documents, never environment or DB files."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

from api.research_mapping import library_source
from searchos.knowledge_store.database import ResearchDatabase, encode, now

from . import SCHEMA_VERSION
from .contracts import build_contract
from .models import ResultEnvelope

DOCS = Path(__file__).resolve().parents[3] / "design/knowledge-ingestion-v2"
EXPERIMENT_DIRS = {
    "widesearch_experiment",
    "widesearch_baselines",
    "widesearch_recovery",
    "widesearch_odr_lite",
}


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _example():
    quote = "Fictional Sensor v2 supports publish-subscribe messaging."
    return dict(
        schema_version=SCHEMA_VERSION,
        document_id="fictional_example_only",
        source_revision=hashlib.sha256(quote.encode()).hexdigest(),
        outputs=[
            dict(
                entities=[
                    dict(
                        entity_key="p1",
                        kind="product",
                        name="Fictional Sensor v2",
                        namespace="example",
                        identity_quote=quote,
                        fields=[
                            dict(
                                field_id="product_supports_publish_subscribe",
                                value=True,
                                quote=quote,
                                qualifiers={"version": "v2"},
                            )
                        ],
                    )
                ],
                relations=[],
                records=[],
                notes=[],
                unmapped=[],
            )
        ],
        summary=dict(text=quote, kind="extractive", quotes=[quote]),
        status="completed",
        notes=["虚构格式示例，不得作为真实资料提交"],
    )


def _business_evidence(source, document, *, workspace_root=None, cache=None):
    """Reuse only named business sessions and quotes re-anchored to this original."""
    root = Path(
        workspace_root
        or os.environ.get("SF_WORKSPACE_ROOT")
        or DOCS.parents[1] / "searchos_workspace"
    ).resolve()
    if any(part in EXPERIMENT_DIRS for part in root.parts):
        return []
    session_values = document.get("session_ids") or []
    sessions = list(session_values) if isinstance(session_values, list) else []
    origin = document.get("origin") or {}
    if origin.get("session_id"):
        sessions.append(origin["session_id"])
    allowed_urls = {source.url, origin.get("request_url"), origin.get("final_url")}
    cache = cache if cache is not None else {}
    result, seen = [], set()
    for session in dict.fromkeys(sessions):
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", session):
            continue
        path = (root / session).resolve()
        if path.parent != root:
            continue
        # Legacy aliases are resolved only within the explicitly referenced session.
        if not path.exists() and re.fullmatch(r"[a-f0-9]{12}", session):
            matches = [
                p.resolve()
                for p in root.glob(f"*_{session}")
                if re.fullmatch(r"\d{8}_\d{6}_" + session, p.name) and p.resolve().parent == root
            ]
            if len(matches) == 1:
                path = matches[0]
        state_path = (path / "search_state.json").resolve()
        if state_path.parent != path:
            continue
        if state_path not in cache:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                graph = state.get("evidence_graph", state.get("evidenceGraph", {}))
                nodes = graph.get("nodes", [])
                cache[state_path] = nodes if isinstance(nodes, list) else []
            except (OSError, ValueError, AttributeError):
                cache[state_path] = []
        for node in cache[state_path]:
            if not isinstance(node, dict) or node.get("status") != "active":
                continue
            url = node.get("source", "")
            if (
                not isinstance(url, str)
                or not url.startswith(("https://", "http://"))
                or url not in allowed_urls
            ):
                continue
            quote = node.get("source_excerpt", node.get("sourceExcerpt", ""))
            try:
                quote = source.quote(quote)
            except ValueError:
                continue
            finding = node.get("finding", "")
            if not isinstance(finding, str):
                continue
            fingerprint = (url, quote, finding)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            start = source.text.index(quote)
            result.append(
                dict(
                    session_id=path.name,
                    evidence_id=node.get("id", ""),
                    source=url,
                    finding=finding,
                    source_excerpt=quote,
                    span=[start, start + len(quote)],
                    span_basis="packaged_source_text",
                    entity=node.get("entity", ""),
                    attribute=node.get("attribute", ""),
                    value=node.get("value", ""),
                    candidate_only=True,
                )
            )
    return result


def export_package(*, data_root, database, slug, documents, library_id, output_dir, limit=None):
    """Build an immutable handoff directory and ZIP without invoking any model.

    `documents` must be the caller's topic-filtered business-library selection.
    Every item is still revalidated against the live library before exporting.
    Existing destinations are never silently replaced.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9:_-]{0,99}", slug):
        raise ValueError("专题标识无效")
    if limit is not None and (isinstance(limit, bool) or limit < 1):
        raise ValueError("limit 必须是正整数")
    data_root = Path(data_root).resolve()
    if any(p in EXPERIMENT_DIRS for p in data_root.parts):
        raise ValueError("交接包只能读取业务资料库，不能读取实验目录")
    output_dir = Path(output_dir).resolve()
    zip_path = output_dir.parent / (output_dir.name + ".zip")
    if output_dir.exists() or zip_path.exists():
        raise FileExistsError("交接目录或压缩包已存在；请使用新的批次名称")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    contract = build_contract(ResearchDatabase(database))
    seen = set()
    selected = []
    for item in documents:
        doc_id = item.get("document_id", "")
        if doc_id and doc_id not in seen:
            seen.add(doc_id)
            selected.append(item)
    selected = selected[:limit] if limit is not None else selected
    if not selected:
        raise ValueError("该专题没有选中的业务原文可打包")
    with tempfile.TemporaryDirectory(prefix=".knowledge-package-", dir=output_dir.parent) as temp:
        root = Path(temp) / "package"
        root.mkdir()
        for name in (
            "sources",
            "evidence",
            "templates",
            "results",
            "summaries",
            "schemas",
            "examples",
        ):
            (root / name).mkdir()
        sources, excluded, evidence_cache = [], [], {}
        for document in selected:
            doc_id = document["document_id"]
            revision = document.get("source_revision", "")
            try:
                if not re.fullmatch(r"[a-f0-9]{64}", revision):
                    raise ValueError("原文缺少有效版本编号")
                source = library_source(data_root, library_id, doc_id, revision)
                sid = (
                    "src_"
                    + hashlib.sha256(encode([library_id, doc_id, revision]).encode()).hexdigest()[
                        :24
                    ]
                )
                relative = f"sources/{sid}.md"
                (root / relative).write_text(source.text, encoding="utf-8")
                entry = dict(
                    source_id=sid,
                    document_id=doc_id,
                    library_id=library_id,
                    source_revision=revision,
                    text_sha256=source.digest,
                    title=source.title,
                    url=source.url,
                    path=relative,
                    result_path=f"results/{sid}.json",
                    output_path=f"results/{sid}.json",
                    status="ready",
                    research_evidence="not_exported",
                    representation="archived_markdown",
                )
                evidence = _business_evidence(source, document, cache=evidence_cache)
                if evidence:
                    evidence_path = f"evidence/{sid}.json"
                    _json(
                        root / evidence_path,
                        dict(
                            source_id=sid,
                            text_sha256=source.digest,
                            notice="仅为当前任务摘录，不保证覆盖全文；按本次正文重新定位，待抽取器复核。",
                            findings=evidence,
                        ),
                    )
                    entry.update(
                        research_evidence="matched_business_quotes",
                        evidence_path=evidence_path,
                        evidence_count=len(evidence),
                    )
                sources.append(entry)
                _json(
                    root / "templates" / f"{sid}.json",
                    dict(
                        schema_version=SCHEMA_VERSION,
                        document_id=doc_id,
                        source_revision=revision,
                        outputs=[],
                        summary=None,
                        status="partial",
                        notes=["这是未填写模板，不能直接作为完成结果；先阅读对应原文。"],
                    ),
                )
            except (ValueError, FileNotFoundError, KeyError) as exc:
                excluded.append(dict(document_id=doc_id, status="excluded", reason=str(exc)))
        if not sources:
            raise ValueError("所选资料均未通过原文版本/可读性检查，未生成空任务包")
        manifest = dict(
            schema_version=SCHEMA_VERSION,
            created_at=now(),
            topic=slug,
            slug=slug,
            library_id=library_id,
            scope="business_library_only",
            contract_version=contract["version"],
            contract_sha256=contract["sha256"],
            sources=sources,
            excluded=excluded,
            source_count=len(sources),
            summary_policy="独立摘要；正文不可覆盖；摘要不能代替事实原句",
            model_calls=0,
        )
        manifest["package_id"] = (
            "package_"
            + hashlib.sha256(
                encode({k: v for k, v in manifest.items() if k != "created_at"}).encode()
            ).hexdigest()[:24]
        )
        manifest["batch_id"] = manifest["package_id"]
        _json(root / "manifest.json", manifest)
        _json(root / "schemas" / "field_contract.json", contract)
        _json(root / "schemas" / "result.schema.json", ResultEnvelope.model_json_schema())
        table = io.StringIO()
        writer = csv.writer(table)
        writer.writerow(
            [
                "field_id",
                "label",
                "subject_kind",
                "value_type",
                "definition",
                "allowed_values",
                "not_to_confuse",
                "repeat_group",
                "element_id",
                "target_table",
                "definition_status",
            ]
        )
        for f in contract["fields"]:
            writer.writerow(
                [
                    json.dumps(f.get(k), ensure_ascii=False)
                    if isinstance(f.get(k), (list, dict))
                    else f.get(k, "")
                    for k in [
                        "field_id",
                        "label",
                        "subject_kind",
                        "value_type",
                        "definition",
                        "allowed_values",
                        "not_to_confuse",
                        "repeat_group",
                        "element_id",
                        "target_table",
                        "definition_status",
                    ]
                ]
            )
        (root / "schemas" / "field_contract.csv").write_text(table.getvalue(), encoding="utf-8-sig")
        example = _example()
        ResultEnvelope.model_validate(example)
        _json(root / "examples" / "fictional_result.json", example)
        (root / "examples" / "fictional_source.md").write_text(
            example["summary"]["text"] + "\n", encoding="utf-8"
        )
        for name in ("WORKBUDDY_TASK.md", "FIELD_DESIGN.md", "REFERENCES.md", "README.md"):
            (root / name).write_text((DOCS / name).read_text(encoding="utf-8"), encoding="utf-8")
        _json(
            root / "progress.json",
            dict(
                package_id=manifest["package_id"],
                total=len(sources),
                completed=0,
                partial=0,
                failed=0,
                current_source_id=None,
                updated_at=now(),
            ),
        )
        (root / "errors.jsonl").write_text("", encoding="utf-8")
        temporary_zip = Path(temp) / "package.zip"
        with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root).as_posix()
                if path.is_file():
                    archive.write(path, relative)
                elif relative in {"results", "summaries"}:
                    archive.writestr(relative + "/", "")
        # Contents came only from the allowlisted files above, never from a repo copy.
        os.replace(root, output_dir)
        os.replace(temporary_zip, zip_path)
    return dict(
        package_id=manifest["package_id"],
        output_dir=str(output_dir),
        zip_path=str(zip_path),
        sources=len(sources),
        excluded=excluded,
        contract_sha256=contract["sha256"],
        model_calls=0,
    )
