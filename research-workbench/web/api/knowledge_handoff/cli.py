"""Package/check/preview/commit offline extraction. No model client is imported."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from api.research_mapping import Source
from searchos.knowledge_store.database import DEFAULT_DATABASE, ROOT, encode, now
from searchos.local.index.source_registry import _atomic_write

from .models import ResultEnvelope
from .service import HandoffService, business_path
from .summaries import validate_summary


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 30_000_000:
        raise ValueError("结果文件过大，请按文档拆分")
    return json.loads(path.read_text(encoding="utf-8"))


def package_path(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError("交接包只能使用内部相对路径")
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("交接包文件路径越界")
    return target


def validate_result(result, source, contract):
    from api.research_extraction import validate_window
    from api.research_mapping import DocumentExtraction

    from .contracts import normalize_by_contract

    value = ResultEnvelope.model_validate(result)
    for output in value.outputs:
        validate_window({"segment": {"text": source.text}}, output.model_dump())
    outputs, issues = normalize_by_contract(
        contract, [output.model_dump() for output in value.outputs], source
    )
    if issues:
        # A rejected fact cannot disappear behind the model's "completed" flag.
        details = "；".join(f"{issue['path']}: {issue['reason']}" for issue in issues[:20])
        raise ValueError(f"抽取契约校验未通过（{len(issues)} 项）：{details}")
    value.outputs = [DocumentExtraction.model_validate(output) for output in outputs]
    if value.summary:
        validate_summary(source, value.summary)
    return value


def inspect_package(directory, *, write_report=False):
    """Recompute progress from files, never trust a model's claimed completion."""
    root = business_path(directory)
    manifest = read_json(root / "manifest.json")
    contract = read_json(root / "schemas" / "field_contract.json")
    contract_hash = hashlib.sha256(
        encode({k: v for k, v in contract.items() if k != "sha256"}).encode()
    ).hexdigest()
    if contract.get("sha256") != contract_hash or manifest.get("contract_sha256") != contract_hash:
        raise ValueError("字段规范与交接清单的校验值不一致")
    counts = {
        "total": len(manifest["sources"]),
        "pending": 0,
        "completed": 0,
        "partial": 0,
        "no_relevant_content": 0,
        "invalid": 0,
        "failed": 0,
    }
    reported_errors, log_errors = {}, []
    error_log = root / "errors.jsonl"
    if error_log.is_file():
        if error_log.stat().st_size > 5_000_000:
            log_errors.append("错误日志超过 5 MB，请分批归档后检查")
        else:
            for number, line in enumerate(error_log.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if (
                        not isinstance(entry, dict)
                        or not entry.get("source_id")
                        or not entry.get("reason")
                    ):
                        raise ValueError("缺少 source_id/reason")
                    reported_errors[entry["source_id"]] = entry
                except (ValueError, TypeError):
                    log_errors.append(f"errors.jsonl 第 {number} 行格式无效")
    items = []
    expected = set()
    for item in manifest["sources"]:
        report = {"source_id": item["source_id"], "document_id": item["document_id"], "errors": []}
        result_path = package_path(root, item.get("output_path") or item["result_path"])
        expected.add(result_path)
        try:
            source_path = package_path(root, item["path"])
            body = source_path.read_bytes()
            if hashlib.sha256(body).hexdigest() != item["text_sha256"]:
                raise ValueError("正文副本被修改；请从原始包恢复，不能依据被修改正文抽取")
            if not result_path.exists():
                failure = reported_errors.get(item["source_id"])
                report["status"] = "failed" if failure else "pending"
                if failure:
                    report["reported_error"] = failure
            else:
                value = read_json(result_path)
                if (
                    value.get("document_id") != item["document_id"]
                    or value.get("source_revision") != item["source_revision"]
                ):
                    raise ValueError("结果的文档编号或原文版本与清单不一致")
                source = Source(
                    item["url"],
                    item["title"],
                    body.decode("utf-8"),
                    str(source_path),
                    item["document_id"],
                    library_id=manifest["library_id"],
                    source_revision=item["source_revision"],
                )
                result = validate_result(value, source, contract)
                report["status"] = result.status
                report["facts"] = sum(
                    len(e.fields) for o in result.outputs for e in o.entities
                ) + sum(len(o.records) + len(o.relations) for o in result.outputs)
                report["unmapped"] = sum(len(o.unmapped) for o in result.outputs)
                report["summary"] = result.summary is not None
                if report["status"] == "completed" and not any(
                    o.entities or o.records or o.relations for o in result.outputs
                ):
                    report["status"] = "partial"
                    report["note"] = "仅提交了摘要，不能据此判定字段抽取完成"
                if report["unmapped"] and report["status"] == "completed":
                    report["status"] = "partial"
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report.update(status="invalid", errors=[str(exc)])
        counts[report["status"]] += 1
        items.append(report)
    unexpected = [
        str(p.relative_to(root))
        for p in (root / "results").glob("*.json")
        if p.resolve() not in expected
    ]
    result = {
        "batch_id": manifest.get("batch_id") or manifest["package_id"],
        "slug": manifest.get("slug") or manifest["topic"],
        "path": str(root),
        "counts": counts,
        "documents": items,
        "unexpected_results": unexpected,
        "log_errors": log_errors,
        "status": "needs_attention"
        if counts["invalid"] or counts["failed"] or unexpected or log_errors
        else "completed"
        if not counts["pending"] and not counts["partial"]
        else "in_progress",
        "updated_at": now(),
        "model_calls": 0,
        "validation_scope": "结构、来源、引句与字段类型；不等于语义已人工核实",
    }
    if write_report:
        _atomic_write(
            root / "validation-report.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
    return result


def business_catalog(data_root, topics_database):
    def read(slug):
        from api.research_materials import research_materials

        data = research_materials(data_root)
        ids = set()
        path = business_path(topics_database)
        if path.exists():
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                ids = {
                    r[0]
                    for r in db.execute(
                        "SELECT d.document_id FROM topic_documents d "
                        "JOIN topics t ON t.id=d.topic_id WHERE t.namespace=?",
                        (slug,),
                    )
                }
        documents = []
        for doc in data["documents"]:
            if doc["document_id"] not in ids:
                continue
            origin = doc.get("origin") or {}
            documents.append(
                {
                    **doc,
                    "name": origin.get("title") or doc["name"],
                    "url": origin.get("final_url") or origin.get("url", ""),
                }
            )
        return {"library_id": data["library_id"], "documents": documents}

    return read


def main(argv=None):
    parser = argparse.ArgumentParser(description="业务资料离线抽取交接；不会调用模型或搜索接口")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--data-root", type=Path, default=ROOT / "knowledge_base")
    parser.add_argument(
        "--topics-database", type=Path, default=ROOT / "data/knowledge/topics.sqlite"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("pack", help="导出正文、规范、任务说明和 ZIP")
    pack.add_argument("--topic", default="anduril")
    pack.add_argument("--output", type=Path)
    pack.add_argument("--limit", type=int)
    pack.add_argument("--document", action="append", default=[], help="可重复提供业务文档编号")
    check = commands.add_parser("check", help="核对 Workbuddy 结果，不写数据库")
    check.add_argument("--package", type=Path, required=True)
    status = commands.add_parser("status", help="查看当前产物进度")
    status.add_argument("--package", type=Path)
    status.add_argument("--topic", default="anduril")
    preview = commands.add_parser("preview", help="产生 SQL 写入预览，不写数据库")
    preview.add_argument("--topic", default="anduril")
    preview.add_argument("--result", type=Path, required=True)
    preview.add_argument("--choices", type=Path)
    commit = commands.add_parser("commit", help="明确确认一份预览后提交 SQL")
    commit.add_argument("--topic", default="anduril")
    commit.add_argument("--plan", required=True)
    commit.add_argument("--confirm", action="store_true", required=True)
    commit.add_argument("--allow-partial", action="store_true")
    summary = commands.add_parser("save-summary", help="独立保存摘要，不发布字段")
    summary.add_argument("--topic", default="anduril")
    summary.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check" or (args.command == "status" and args.package):
            result = inspect_package(args.package, write_report=args.command == "check")
        else:
            catalog = business_catalog(args.data_root, args.topics_database)
            service = HandoffService(args.database, args.data_root, catalog)
            if args.command == "pack":
                from .package import export_package

                available = catalog(args.topic)
                documents = available["documents"]
                if args.document:
                    requested = set(args.document)
                    documents = [d for d in documents if d["document_id"] in requested]
                    if requested != {d["document_id"] for d in documents}:
                        raise ValueError("所选文档不存在或不属于该业务专题")
                suffix = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
                output = args.output or service.root / "batches" / f"{args.topic}-{suffix}"
                result = export_package(
                    data_root=service.data_root,
                    database=service.database,
                    slug=args.topic,
                    documents=documents,
                    library_id=available["library_id"],
                    output_dir=business_path(output),
                    limit=args.limit,
                )
            elif args.command == "preview":
                result = service.preview(
                    args.topic,
                    read_json(args.result),
                    entity_choices=read_json(args.choices) if args.choices else None,
                )
                # SQL with parameters is retained in the server-side plan file.
                result = {
                    k: v
                    for k, v in result.items()
                    if k not in {"sql_statements", "outputs", "result", "signature"}
                }
            elif args.command == "commit":
                result = service.commit(args.topic, args.plan, allow_partial=args.allow_partial)
            elif args.command == "save-summary":
                result = service.save_summary(args.topic, read_json(args.result))
            else:
                result = service.status(args.topic)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return (
            2
            if args.command == "check"
            and (
                result["counts"]["invalid"]
                or result["counts"]["failed"]
                or result["unexpected_results"]
                or result["log_errors"]
            )
            else 0
        )
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc), "model_calls": 0}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
