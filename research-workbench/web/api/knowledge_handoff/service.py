"""Business-only orchestration for offline handoff, preview, and submission."""

from __future__ import annotations

import hmac
import json
import re
from pathlib import Path

from api.research_mapping import library_source
from searchos.knowledge_store.database import ResearchDatabase
from searchos.local.index.source_registry import _atomic_write

from .models import ResultEnvelope
from .summaries import list_summaries, save_summary, validate_summary

EXPERIMENT_DIRS = {
    "widesearch_experiment",
    "widesearch_baselines",
    "widesearch_odr_lite",
    "widesearch_recovery",
    "widesearch_fetch_backup",
}


def business_path(path):
    path = Path(path).expanduser().resolve()
    if EXPERIMENT_DIRS.intersection(path.parts):
        raise ValueError("实验目录不能作为业务知识库的输入或输出")
    return path


class HandoffService:
    def __init__(self, database, data_root, catalog):
        self.database = business_path(database)
        self.data_root = business_path(data_root)
        self.root = self.database.parent / "knowledge_handoff"
        self.store = ResearchDatabase(self.database)
        self.catalog = catalog

    def source(self, slug, document_id, revision=None):
        if not re.fullmatch(r"[a-z0-9][a-z0-9:_-]{0,99}", slug):
            raise ValueError("专题编号无效")
        available = self.catalog(slug)
        doc = next((d for d in available["documents"] if d["document_id"] == document_id), None)
        if doc is None:
            raise ValueError("原文已删除或不属于当前业务专题")
        if revision and revision != doc["source_revision"]:
            raise ValueError("原文版本已变化，请重新打包或生成预览")
        try:
            source = library_source(
                self.data_root, available["library_id"], document_id, doc["source_revision"]
            )
        except (FileNotFoundError, KeyError) as exc:
            raise ValueError("原文已删除或解析文件不可用") from exc
        business_path(source.artifact_path)
        return source

    def preview(self, slug, result, *, entity_choices=None):
        from .review import _secret, _signature, build_plan

        value = ResultEnvelope.model_validate(result)
        source = self.source(slug, value.document_id, value.source_revision)
        if value.summary:
            validate_summary(source, value.summary)
        plan = build_plan(
            self.store,
            source,
            [o.model_dump() for o in value.outputs],
            slug=slug,
            artifact_root=self.root,
            entity_choices=entity_choices,
        )
        submission = self.root / "submissions" / f"{plan['plan_id']}.json"
        submission.parent.mkdir(parents=True, exist_ok=True)
        bundle = {"plan_id": plan["plan_id"], "slug": slug, "result": value.model_dump()}
        bundle["signature"] = _signature(bundle, _secret(self.root))
        _atomic_write(submission, json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
        return {**plan, "result": value.model_dump(), "model_calls": 0}

    def get_plan(self, slug, plan_id):
        from .review import _secret, _signature, load_plan

        plan = load_plan(self.root, plan_id)
        if plan.get("slug") != slug:
            raise ValueError("写入计划不属于当前专题")
        submission = self.root / "submissions" / f"{plan_id}.json"
        if submission.is_file():
            saved = json.loads(submission.read_text(encoding="utf-8"))
            if (
                saved.get("plan_id") != plan_id
                or saved.get("slug") != slug
                or not hmac.compare_digest(
                    saved.get("signature", ""), _signature(saved, _secret(self.root))
                )
            ):
                raise ValueError("预览对应的抽取或摘要文件被修改，请重新生成预览")
            value = ResultEnvelope.model_validate(saved["result"])
            if (
                value.document_id != plan["document_id"]
                or value.source_revision != plan["source_revision"]
            ):
                raise ValueError("抽取结果与写入计划的原文版本不一致")
            plan = {**plan, "result": value.model_dump()}
        # A receipt is metadata about execution, not a mutation of the signed plan.
        with self.store.connect() as db:
            committed = db.execute(
                "SELECT value FROM database_metadata WHERE key=?", ("handoff_commit:" + plan_id,)
            ).fetchone()
        if committed:
            receipt = json.loads(committed[0])
            plan = {
                **plan,
                "status": "committed",
                "can_commit": False,
                "receipt": receipt,
                "committed_at": receipt.get("committed_at"),
            }
        return plan

    def commit(self, slug, plan_id, *, allow_partial=False):
        from .review import commit_plan

        plan = self.get_plan(slug, plan_id)
        source = self.source(slug, plan["document_id"], plan["source_revision"])
        envelope = ResultEnvelope.model_validate(plan["result"]) if plan.get("result") else None
        if envelope and envelope.summary:
            validate_summary(source, envelope.summary)
        receipt = commit_plan(
            self.store, source, plan_id, artifact_root=self.root, allow_partial=allow_partial
        )
        if envelope and envelope.summary:
            # Summaries are independent derivatives, not source or fact rows.
            try:
                receipt["summary"] = save_summary(self.root, source, envelope.summary, slug=slug)
            except OSError:
                receipt["summary_error"] = "字段已入库，独立摘要保存失败；可单独重试保存摘要"
        return receipt

    def save_summary(self, slug, result):
        envelope = ResultEnvelope.model_validate(result)
        if not envelope.summary:
            raise ValueError("本次结果没有独立摘要")
        source = self.source(slug, envelope.document_id, envelope.source_revision)
        return save_summary(self.root, source, envelope.summary, slug=slug)

    def document(self, slug, document_id):
        source = self.source(slug, document_id)
        return {
            "document_id": document_id,
            "title": source.title,
            "url": source.url,
            "source_revision": source.source_revision,
            "text": source.text,
            "summaries": list_summaries(
                self.root,
                slug=slug,
                document_id=document_id,
                source_revision=source.source_revision,
                text_sha256=source.digest,
            ),
        }

    def status(self, slug):
        plans, errors = [], []
        for path in (self.root / "plans").glob("*.json"):
            if path.is_symlink():
                continue
            try:
                plan = self.get_plan(slug, path.stem)
                plans.append(
                    {
                        k: v
                        for k, v in plan.items()
                        if k
                        in {
                            "plan_id",
                            "status",
                            "title",
                            "document_id",
                            "source_revision",
                            "counts",
                            "unmapped",
                            "conflicts",
                            "created_at",
                            "slug",
                            "receipt",
                            "committed_at",
                        }
                    }
                )
            except (OSError, ValueError, KeyError) as exc:
                if "不属于当前专题" not in str(exc):
                    errors.append({"plan_id": path.stem, "error": str(exc)})
                continue
        available = self.catalog(slug)
        live = {d["document_id"]: d["source_revision"] for d in available["documents"]}
        for plan in plans:
            plan["source_current"] = live.get(plan.get("document_id")) == plan.get(
                "source_revision"
            )
        summaries = []
        for summary in list_summaries(self.root, slug=slug):
            if summary.get("document_id") not in live:
                continue
            summaries.append({k: v for k, v in summary.items() if k not in {"text", "evidence"}})
            summaries[-1]["current"] = live[summary["document_id"]] == summary["source_revision"]
        batches = []
        for manifest in (self.root / "batches").glob("*/manifest.json"):
            try:
                from .cli import inspect_package

                state = inspect_package(manifest.parent)
                if state.get("slug") == slug:
                    batches.append(state)
            except (OSError, ValueError, KeyError) as exc:
                errors.append({"batch": manifest.parent.name, "error": str(exc)})
        return {
            "plans": sorted(plans, key=lambda p: p.get("created_at", ""), reverse=True),
            "summaries": summaries,
            "batches": batches,
            "errors": errors,
            "model_calls": 0,
        }
