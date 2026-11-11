"""Bounded topic research: inspect local knowledge, collect gaps, then re-plan.

The topic planner chooses an investigation scope; SearchOS still performs the
model-driven schema creation, agent/task decomposition and source research.
Only an explicit start creates work. Reads and service startup never spend.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict
from textwrap import shorten

from fastapi import HTTPException

from api.research_assets import asset_detail
from searchos.knowledge_store.database import ResearchDatabase

FACETS = (
    {
        "id": "products",
        "title": "产品谱系",
        "kinds": ["product"],
        "terms": ["产品", "型号", "系列", "谱系", "product", "variant"],
        "brief": "识别产品家族、准确型号、版本、组成与替代关系，收集公开参数及其测试条件。",
    },
    {
        "id": "technology",
        "title": "技术与路线",
        "kinds": ["technology", "route"],
        "terms": ["技术", "原理", "路线", "机制", "架构", "technology", "mechanism"],
        "brief": (
            "调查公开技术机制、输入输出、组成依赖、实现路线、约束和替代方案，避免仅摘录营销描述。"
        ),
    },
    {
        "id": "maturity",
        "title": "成熟度与进展",
        "kinds": ["product", "technology", "route"],
        "terms": ["成熟", "部署", "验证", "进展", "最新", "maturity", "deployment"],
        "brief": (
            "查找样机、测试、交付和使用的有日期证据，区分厂商宣称与独立结果，不凭名称推断成熟度。"
        ),
    },
    {
        "id": "references",
        "title": "独立开源参考",
        "kinds": ["reference"],
        "terms": ["开源", "复现", "github", "代码", "license", "repository"],
        "brief": (
            "寻找可独立运行的外部开源参考，核对真实仓库、许可证、固定版本、"
            "依赖和验收方法。不得推断产品采用。"
        ),
    },
)
FRESH_TERMS = re.compile(r"最新|最近|目前|截至|更新|进展|current|latest|update", re.I)
SUPPORT_LABELS = {
    "supported": "支持",
    "unsupported": "不支持",
    "partial": "部分支持",
    "planned": "计划支持",
    "claimed_unverified": "来源宣称支持",
}
FIELD_ALIASES = {
    "mechanism": ["原理", "机制", "mechanism"],
    "inputs": ["输入", "inputs"],
    "outputs": ["输出", "outputs"],
    "license": ["许可", "license"],
    "repo_url": ["仓库", "repo"],
    "family": ["家族", "系列", "family"],
    "parameters": ["参数", "parameters"],
    "maturity": ["成熟度", "maturity"],
}


def _matching_names(entities, lower):
    matched_names = {}
    for item in entities:
        aliases = item.get("attributes", {}).get("aliases", [])
        names = [item["name"], *(aliases if isinstance(aliases, list) else [aliases])]
        found = [
            name.casefold()
            for name in names
            if isinstance(name, str) and name and name.casefold() in lower
        ]
        if found:
            matched_names[item["id"]] = max(found, key=len)
    return {
        identity: name
        for identity, name in matched_names.items()
        if not any(name != other and name in other for other in matched_names.values())
    }


def local_lookup(snapshot: dict, query: str, *, legacy_fields=True) -> dict:
    """Conservative exact entity/field path. A topic-wide survey is never 'done'."""
    lower = query.casefold()
    entities = snapshot["knowledge"]["entities"]
    matched_names = _matching_names(entities, lower)
    matched = [item for item in entities if item["id"] in matched_names]
    answers = []
    remainder = lower
    for item in matched:
        remainder = remainder.replace(matched_names[item["id"]], "")
    aliases_by_field = {key: list(words) for key, words in FIELD_ALIASES.items()} if legacy_fields else {}
    for item in matched:
        for definition in snapshot.get("entity_fields", {}).get(item["id"], []):
            aliases_by_field.setdefault(definition["key"], []).extend(
                [
                    definition["key"],
                    definition["label"],
                    *definition.get("aliases", []),
                ]
            )
    matching_words = {
        key: [word.casefold() for word in words if word and word.casefold() in remainder]
        for key, words in aliases_by_field.items()
    }
    fields = [
        key
        for key, words in matching_words.items()
        if any(
            not any(
                word != other and word in other
                for other_key, others in matching_words.items()
                if other_key != key
                for other in others
            )
            for word in words
        )
    ]
    for field in fields:
        for word in sorted(aliases_by_field[field], key=len, reverse=True):
            remainder = remainder.replace(word.casefold(), "")
    for word in (
        "请告诉我", "介绍一下", "请问", "是什么", "什么", "介绍", "是否", "有没有", "多少",
        "请", "的", "是", "吗",
    ):
        remainder = remainder.replace(word, "")
    simple = (
        len(matched) == 1 and len(fields) == 1 and not re.search(r"[\w\u4e00-\u9fff]", remainder)
    )
    complete = simple and not FRESH_TERMS.search(query)
    for item in matched:
        values = {}
        for field in fields:
            projected = next(
                (
                    f
                    for f in snapshot.get("entity_fields", {}).get(item["id"], [])
                    if f["key"] == field
                ),
                None,
            )
            if projected is not None:
                observations = [
                    o for o in projected.get("observations", []) if o.get("active", True)
                ]
                if projected.get("value") is not None and projected.get("value") != "":
                    value = projected["value"]
                    citations = projected.get("citations", [])
                    if citations:
                        value = str(value) + "\n" + "\n".join(citations)
                    values[field] = value
                distinct = {
                    json.dumps(o.get("value"), ensure_ascii=False, sort_keys=True)
                    for o in observations
                }
                if (
                    projected.get("status") != "known"
                    or not projected.get("evidence_ids")
                    or any(o.get("manual") for o in observations)
                    or len(distinct) > 1
                ):
                    complete = False
                continue
            candidates = [field, *aliases_by_field[field]]
            selected = next((key for key in candidates if item["attributes"].get(key)), None)
            if selected is None or selected in item.get("field_overrides", {}):
                complete = False
            else:
                values[selected] = item["attributes"][selected]
                observed = {
                    json.dumps(
                        observation.get("attributes", {}).get(key),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    for observation in item["attributes"].get("document_observations", [])
                    for key in candidates
                    if observation.get("attributes", {}).get(key)
                }
                observed.add(json.dumps(values[selected], ensure_ascii=False, sort_keys=True))
                if len(observed) > 1:
                    complete = False
                    values[selected] = "存在多个来源值，需进一步核对：" + str(values[selected])
                # Only mapped field evidence can satisfy a field question.
                if not item.get("field_evidence", {}).get(selected):
                    complete = False
        answers.append({"id": item["id"], "name": item["name"], "values": values})
    text = "\n".join(
        item["name"] + "：" + "；".join(f"{key}：{value}" for key, value in item["values"].items())
        for item in answers
        if item["values"]
    )
    if not text:
        text = f"本专题已有 {len(entities)} 个实体、{len(snapshot['sources'])} 份资料。"
        if matched:
            text += "匹配对象：" + "、".join(item["name"] for item in matched[:8]) + "。"
            if not legacy_fields:
                text += "未找到有原文依据的匹配字段。"
        elif entities:
            text += "对象包括：" + "、".join(item["name"] for item in entities[:8]) + "。"
    return {
        "sufficient": bool(complete),
        "text": text,
        "entity_ids": [item["id"] for item in matched],
    }


def published_local_lookup(store: ResearchDatabase, slug: str, query: str, sources: list) -> dict | None:
    """Read the same accepted facts that power the asset pages and graph."""
    with store.connect() as db:
        names = [
            {"id": row["id"], "name": row["canonical_name"], "attributes": {"aliases": []}}
            for row in db.execute(
                "SELECT e.id,e.canonical_name FROM entities e "
                "JOIN topic_entities t ON t.entity_id=e.id "
                "WHERE t.topic_id=? AND e.state='active' "
                "AND e.kind NOT IN ('metric','capability','event') "
                "AND EXISTS(SELECT 1 FROM claims c WHERE c.subject_id=e.id "
                "AND c.topic_id=t.topic_id AND c.status='accepted') "
                "ORDER BY e.canonical_name,e.id",
                ("topic:" + slug,),
            )
        ]
        if not names:
            return None
        by_id = {item["id"]: item for item in names}
        has_scopes = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_name_scopes'"
        ).fetchone()
        scope_filter = (
            " AND NOT EXISTS(SELECT 1 FROM entity_name_scopes s WHERE s.name_id=n.id)"
            if has_scopes else ""
        )
        for row in db.execute(
            "SELECT n.entity_id,n.name FROM entity_names n "
            "JOIN topic_entities t ON t.entity_id=n.entity_id "
            "WHERE t.topic_id=? AND n.valid_to IS NULL AND n.name_type<>'former'" + scope_filter,
            ("topic:" + slug,),
        ):
            if row["entity_id"] in by_id:
                by_id[row["entity_id"]]["attributes"]["aliases"].append(row["name"])

    entity_fields = {}
    for entity_id in _matching_names(names, query.casefold()):
        detail = asset_detail(store, slug, entity_id)
        by_label = defaultdict(list)
        for record in detail["records"]:
            if record["kind"] in {"field", "metric", "capability", "relation", "participation"}:
                by_label[record["label"]].append(record)
        fields = []
        for label, records in by_label.items():
            observations = {}
            for record in records:
                evidence = next(
                    (item for item in record["evidence"] if item["stance"] == "supports"), None
                )
                value = record["value"]
                if record["kind"] == "metric":
                    value = value["raw_value"]
                elif record["kind"] == "capability":
                    value = SUPPORT_LABELS.get(value, value)
                if evidence is None or value is None or value == "" or value == []:
                    continue
                if isinstance(value, list):
                    value = "、".join(str(item) for item in value)
                elif isinstance(value, bool):
                    value = "是" if value else "否"
                qualifiers = [
                    str(v) for k, v in record["qualifiers"].items()
                    if k != "original_value" and v not in (None, "")
                ]
                display = str(value)
                if qualifiers:
                    display += "（" + "；".join(qualifiers) + "）"
                observations.setdefault(display, (value, evidence))
            if not observations:
                continue
            distinct = {str(value) for value, _ in observations.values()}
            aliases = {record["field_id"] for record in records if record["field_id"]}
            if label == "续航时长":
                aliases.update(("续航", "续航时间"))
            if label.startswith("支持"):
                aliases.add(label.removeprefix("支持"))
            shown = list(observations.items())[:4]
            citations = []
            for display, (_, ev) in shown:
                quote = shorten(ev["quote"], width=240, placeholder="…")
                prefix = f"{display} · " if len(shown) > 1 else ""
                citations.append(f"{prefix}［{ev['title']}］{quote}\n{ev['url']}")
            multiple = len(distinct) > 1
            conflicting = multiple and any(r["kind"] not in {"relation", "participation"} for r in records)
            value = "；".join(display for display, _ in shown)
            if conflicting:
                value = "多个来源值，请结合条件核对\n" + value
            if len(observations) > len(shown):
                value += f"\n另有 {len(observations) - len(shown)} 条记录可在知识资产中查看。"
            fields.append({
                "key": label, "label": label, "aliases": sorted(aliases),
                "status": "conflicting" if conflicting else "known",
                "value": value, "evidence_ids": [ev["revision_id"] for _, ev in observations.values()],
                "citations": list(dict.fromkeys(citations)),
            })
        entity_fields[entity_id] = fields
    return local_lookup(
        {"knowledge": {"entities": names}, "entity_fields": entity_fields, "sources": sources},
        query,
        legacy_fields=False,
    )


def plan_tasks(snapshot: dict, query: str, finished: set[str] | None = None) -> list[dict]:
    finished = finished or set()
    lower = query.casefold()
    explicit = [facet for facet in FACETS if any(word in lower for word in facet["terms"])]
    # An explicit narrow task stays narrow. A bare name receives the full template.
    facets = explicit or list(FACETS)
    scope_ids = investigation_entity_ids(snapshot, query)
    conflicts = [
        {
            "entity_id": entity["id"],
            "entity_name": entity["name"],
            "kind": entity["kind"],
            "field": field["key"],
            "label": field["label"],
            "status": "conflicting",
            "group": field.get("group", ""),
        }
        for entity in snapshot["knowledge"]["entities"]
        for field in snapshot.get("entity_fields", {}).get(entity["id"], [])
        if field.get("status") == "conflicting"
    ]
    plan = []
    for facet in facets:
        if facet["id"] in finished:
            continue
        gaps = [
            gap
            for gap in [*conflicts, *snapshot.get("gaps", [])]
            if isinstance(gap, dict)
            and gap.get("kind") in facet["kinds"]
            and (scope_ids is None or gap.get("entity_id") in scope_ids)
        ]
        # Many detailed fields must not let the first product monopolise a round.
        grouped: dict[str, list[dict]] = {}
        for gap in gaps:
            grouped.setdefault(gap["entity_id"], []).append(gap)
        selected = []
        while grouped and len(selected) < 16:
            for identity in list(grouped):
                selected.append(grouped[identity].pop(0))
                if not grouped[identity]:
                    del grouped[identity]
                if len(selected) == 16:
                    break
        plan.append(
            {
                **facet,
                "gaps": selected,
                "missing": len(gaps),
                "entity_ids": sorted(scope_ids) if scope_ids is not None else None,
            }
        )
    return plan


def investigation_entity_ids(snapshot: dict, query: str) -> set[str] | None:
    """An explicit branch/object narrows work; a bare topic keeps its full scope."""
    lower = query.casefold()
    matches = []
    for cluster in snapshot.get("clusters", []):
        names = [cluster["label"], cluster["id"].split(".")[-1], *cluster.get("keywords", [])]
        for name in names:
            if len(name) >= 2 and name.casefold() in lower:
                matches.append((name.casefold(), set(cluster["entity_ids"])))
    for entity in snapshot["knowledge"]["entities"]:
        if len(entity["name"]) >= 3 and entity["name"].casefold() in lower:
            adjacent = {entity["id"]}
            for relation in snapshot["knowledge"].get("relations", []):
                if relation["source_id"] == entity["id"]:
                    adjacent.add(relation["target_id"])
                elif relation["target_id"] == entity["id"]:
                    adjacent.add(relation["source_id"])
            matches.append((entity["name"].casefold(), adjacent))
    if not matches:
        return None
    # Keep separately named branches, suppress only a name contained in a more
    # specific matched name (Lattice vs Lattice Mesh), not an unrelated branch.
    specific = [
        (name, ids)
        for name, ids in matches
        if not any(name != other and name in other for other, _ in matches)
    ]
    return set().union(*(ids for _, ids in specific))


def investigation_prompt(topic: dict, query: str, task: dict, snapshot: dict) -> str:
    from api import field_extraction_policy

    scope_ids = task.get("entity_ids")
    existing = [
        {"name": item["name"], "kind": item["kind"], "fields": list(item["attributes"])}
        for item in snapshot["knowledge"]["entities"]
        if scope_ids is None or item["id"] in scope_ids
    ]
    return (
        f"namespace: {topic['namespace']}\n专题：{topic['title']}\n用户要求：{query}\n"
        f"本轮调查：{task['title']}。{task['brief']}\n"
        "请自主识别意图、建立所需字段、拆分子任务并按证据缺口调整检索，用户无需逐步追问。"
        "只在本专题和本轮调查范围内继续，不扩散为无关领域。"
        "先阅读已授权的本地原文，缺少、过时或互相冲突的字段再联网补采。\n"
        "本地对象索引（仅用于导航，不是新的来源证据）："
        + json.dumps(existing[:80], ensure_ascii=False)
        + "\n"
        "待补字段：" + json.dumps(task.get("gaps", []), ensure_ascii=False) + "\n"
        "如有来源值冲突，核对版本、时间和测试条件，保留各自依据，不直接覆盖。"
        "原文明示不公开或不适用的字段无需反复补采；未找到的信息保留未采集。"
        "优先官方技术文档、公开论文、实际仓库和独立验证。通过网页读取工具保存可核对的正文，"
        "搜索摘要不能冒充全文。保留数字、单位、条件、版本、日期与引用。"
        "只采集公开且可核验的技术与产业资料；外部项目只建立有依据的参考关系。"
        + ("原文归档后系统会自动抽取入库。"
           if field_extraction_policy.MODEL_EXTRACTION_ENABLED else "新原文归档后留待抽取和校验入库。")
        + "最终回答简要列出新来源、已解决缺口和剩余缺口，"
        "无需撰写长篇报告或让用户再提供下一步指令。"
    )


class TopicResearchManager:
    def __init__(self, store=None, *, research_store=None, launcher=None, poll_seconds=2):
        self.store = store
        self.research_store = research_store
        self.launcher = launcher
        self.poll_seconds = poll_seconds
        self.workers: dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()

    def _store(self):
        if self.store is not None:
            return self.store
        from api.routes.topics import _store

        return _store()

    async def start(
        self, *, query, topic_id=None, engine=None, max_tasks=4, max_seconds=1200, local_only=False,
        modules=None,
    ):
        from api import workbench
        from api.deps import sessions
        from api.innovation import normalize
        module_config = normalize({"enabled": modules or []})

        if not query.strip() or len(query) > 12000:
            raise HTTPException(422, "请输入主题词或调查要求")
        if not 1 <= max_tasks <= 8 or not 60 <= max_seconds <= 7200:
            raise HTTPException(422, "调查范围超出允许限制")
        async with self.lock:
            if any(not task.done() for task in self.workers.values()) or any(
                item.get("status") == "running" for item in sessions.values()
            ):
                raise HTTPException(409, "已有调查运行中，请等待完成或先停止")
            store = self._store()
            topic = await asyncio.to_thread(store.resolve_topic, query, topic_id)
            config = workbench.load_config()
            selected = engine or config.engine
            if selected not in {"openai", "deepseek", "ollama", "cli"}:
                raise HTTPException(422, "未知模型来源")
            model = getattr(config, selected).model
            run = await asyncio.to_thread(
                store.create_run,
                topic["id"],
                query.strip(),
                selected,
                model,
                max_tasks=max_tasks,
                max_seconds=max_seconds,
                local_only=local_only,
                modules=module_config["enabled"],
            )
            worker = asyncio.create_task(self._execute(run))
            self.workers[run["id"]] = worker
            return {"topic_id": topic["id"], "run_id": run["id"]}

    async def _sources(self, snapshot, query="", task=None):
        from api.routes.search import LocalSourceRequest, SourcesRequest

        sources = [
            item
            for item in snapshot["sources"]
            if item.get("available") and item.get("library_id") and item.get("document_id")
        ]
        # The acquisition archive is one local library. Never broaden to all files.
        if sources:
            library_id = sources[0]["library_id"]
            terms = re.findall(r"[a-z0-9][a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", query.casefold())
            terms.extend(
                item["entity_name"].casefold()
                for item in (task or {}).get("gaps", [])
                if item.get("entity_name")
            )
            terms.extend((task or {}).get("terms", []))

            def relevance(item):
                text = (item.get("title", "") + " " + item.get("url", "")).casefold()
                return (-sum(term in text for term in set(terms)), item["document_id"])

            ranked = sorted(
                (item for item in sources if item["library_id"] == library_id), key=relevance
            )
            ids = list(dict.fromkeys(item["document_id"] for item in ranked))[:100]
            if ids:
                return SourcesRequest(
                    mode="hybrid",
                    local=LocalSourceRequest(
                        library_id=library_id,
                        document_ids=ids,
                        model_access=True,
                    ),
                )
        return SourcesRequest(mode="web")

    async def _collect(self, run, task, snapshot, deadline):
        from api import workbench
        from api.deps import sessions
        from api.routes.search import SearchRequest, create_search, stop_search

        store = self._store()
        if getattr(workbench.load_config(), run["engine"]).model != run["model"]:
            raise ValueError("模型配置已改变，请重新开始调查")
        prompt = investigation_prompt(snapshot["topic"], run["query"], task, snapshot)
        request = SearchRequest(
            query=prompt,
            engine=run["engine"],
            modules=run.get("modules", []),
            effort="low",
            max_time=max(30, min(300, int(deadline - time.monotonic()))),
            sources=await self._sources(snapshot, run["query"], task),
        )
        started = await (self.launcher or create_search)(request)
        session_id = started.session_id
        store.register_session(run["topic_id"], session_id)
        local_count = len(request.sources.local.document_ids) if request.sources.local else 0
        store.update_task(
            task["id"],
            session_id=session_id,
            message=f"已选取 {local_count} 份相关本地原文；正在自主拆解与补采",
        )
        store.update_run(run["id"], session_id=session_id)
        last_collection = None
        def collection_progress():
            nonlocal last_collection
            if "adaptive_collection" not in run.get("modules", []):
                return
            from api.deps import WORKSPACE_ROOT
            from searchos.socm import WorkspaceManager
            state = WorkspaceManager(WORKSPACE_ROOT, session_id).load_state()
            policy = state.collection_policy
            batches = list(policy.batches.values())
            summary = {"batches": len(batches), "new": sum(b.new for b in batches),
                       "corroboration": sum(b.corroboration for b in batches),
                       "groups": len(policy.communities), "last_error": policy.last_error}
            if summary != last_collection:
                store.update_task(task["id"], adaptive_collection=summary)
                last_collection = summary
        try:
            while sessions.get(session_id, {}).get("status") == "running":
                await asyncio.to_thread(collection_progress)
                if time.monotonic() >= deadline:
                    await stop_search(session_id)
                    break
                await asyncio.sleep(self.poll_seconds)
            # Status can be terminal while the finally block is still archiving.
            worker = sessions.get(session_id, {}).get("task")
            if worker:
                try:
                    await asyncio.wait_for(asyncio.shield(worker), timeout=60)
                except asyncio.CancelledError:
                    if not worker.cancelled():
                        raise
                except TimeoutError:
                    return session_id, False
        except asyncio.CancelledError:
            if sessions.get(session_id, {}).get("status") == "running":
                await stop_search(session_id)
            raise
        await asyncio.to_thread(collection_progress)
        return session_id, sessions.get(session_id, {}).get("status") == "completed"

    async def _ingest(self, run, task, session_id, deadline):
        from api.deps import LOCAL_DATA_ROOT, WORKSPACE_ROOT
        from api.research_ingestion import manager as ingestion_manager
        from api.research_materials import research_materials

        store = self._store()
        await asyncio.to_thread(store.attach_session, run["topic_id"], session_id)
        materials = await asyncio.to_thread(research_materials, LOCAL_DATA_ROOT, session_id)
        count = len(materials["documents"])
        if not count:
            return False, 0
        from api import field_extraction_policy as policy

        if not policy.MODEL_EXTRACTION_ENABLED:
            store.update_task(task["id"], message=f"已归档 {count} 份原文；模型抽取已暂停")
            return False, count
        jobs = ingestion_manager(LOCAL_DATA_ROOT)
        topic = store.get_topic(run["topic_id"])
        await asyncio.to_thread(
            jobs.schedule_session,
            session_id,
            engine=policy.EXTRACTION_ENGINE,
            model=policy.EXTRACTION_MODEL,
            scope=f"namespace: {topic['namespace']}",
            workspace_root=WORKSPACE_ROOT,
        )
        jobs.kick()
        store.update_run(run["id"], status="ingesting", message=f"正在抽取 {count} 份原文")
        store.update_task(task["id"], message=f"正在核对引句并抽取 {count} 份原文")
        while True:
            receipt = await asyncio.to_thread(jobs.snapshot, session_id)
            counts = receipt["counts"]
            if not counts["queued"] and not counts["running"]:
                complete = counts["completed"] + counts["skipped"] >= count and not any(
                    counts[state] for state in ("partial", "failed")
                )
                return complete, count
            if time.monotonic() >= deadline:
                return False, count
            await asyncio.sleep(self.poll_seconds)

    async def _execute(self, run):
        store = self._store()
        task = None
        deadline = time.monotonic() + run["max_seconds"]
        try:
            store.update_run(run["id"], status="planning", message="正在检查专题已有资料与字段缺口")
            snapshot = await asyncio.to_thread(store.snapshot, run["topic_id"])
            local = None
            if self.research_store is not None and self.research_store.path.is_file():
                local = await asyncio.to_thread(
                    published_local_lookup, self.research_store, snapshot["topic"]["namespace"],
                    run["query"], snapshot["sources"],
                )
            if local is None:
                local = local_lookup(snapshot, run["query"])
            task = store.add_task(
                run["id"],
                title="查询本地知识",
                kind="local",
                status="completed",
                message=local["text"],
            )
            store.update_run(run["id"], local_result=local["text"])
            if run.get("local_only") or local["sufficient"]:
                store.update_run(
                    run["id"],
                    status="completed",
                    message=(
                        "已从本地字段与原文依据中找到结果，本次未联网"
                        if local["sufficient"]
                        else "本地查询完成；查看下方知识记录与待补字段。本次未联网"
                    ),
                )
                return
            finished, no_progress, partial = set(), 0, False
            while len(finished) < run["max_tasks"]:
                if time.monotonic() >= deadline:
                    partial = True
                    break
                snapshot = await asyncio.to_thread(store.snapshot, run["topic_id"])
                plan = plan_tasks(snapshot, run["query"], finished)
                if not plan:
                    break
                store.update_run(
                    run["id"],
                    status="researching",
                    plan=[
                        {"id": item["id"], "title": item["title"], "missing": item["missing"]}
                        for item in plan
                    ],
                    message=f"第 {len(finished) + 1} 轮：{plan[0]['title']}",
                )
                facet = plan[0]
                task = store.add_task(
                    run["id"],
                    title=facet["title"],
                    kind="research",
                    status="running",
                    facet=facet["id"],
                    message="根据已有字段与缺口规划调查",
                )
                # Keep the durable task ID distinct from the planner facet key.
                task.update({key: value for key, value in facet.items() if key != "id"})
                session_id, research_ok = await self._collect(run, task, snapshot, deadline)
                ingestion_ok, source_count = await self._ingest(run, task, session_id, deadline)
                change = await asyncio.to_thread(
                    store.sync_topic_changes, run["topic_id"], run["id"]
                )
                progress = sum(
                    int(change.get(key, 0))
                    for key in ("entities_added", "entities_updated", "relations_added")
                )
                no_progress = 0 if progress else no_progress + 1
                complete = research_ok and ingestion_ok
                partial = partial or not complete
                store.update_task(
                    task["id"],
                    status="completed" if complete else "partial",
                    message=f"归档 {source_count} 份原文；"
                    + change.get("summary", "已核对知识变化"),
                )
                finished.add(facet["id"])
                if no_progress >= 2:
                    partial = True
                    store.update_run(run["id"], stop_reason="no_progress")
                    break
            remaining = plan_tasks(
                await asyncio.to_thread(store.snapshot, run["topic_id"]), run["query"], finished
            )
            if remaining:
                partial = True
            message = "本轮自主调查结束，知识库已更新"
            if time.monotonic() >= deadline:
                message = "已到调查时限，停止安排新任务；已提交的原文抽取可继续完成"
            elif no_progress >= 2:
                message = "连续两轮没有新增或修订的知识，已停止扩展；剩余缺项保留"
            elif partial:
                message = "本轮调查已结束，仍有未覆盖或未完成入库的资料，可在行动与知识库中查看"
            store.update_run(
                run["id"],
                status="partial" if partial else "completed",
                message=message,
                remaining=[item["title"] for item in remaining],
                finished_facets=sorted(finished),
            )
        except asyncio.CancelledError:
            if task and task.get("kind") != "local":
                store.update_task(
                    task["id"], status="stopped", message="已停止后续调查，保留已采集资料"
                )
            store.update_run(
                run["id"], status="stopped", message="已停止后续调查；已提交的原文抽取继续保留结果"
            )
        except Exception as exc:
            if task and task.get("kind") != "local":
                store.update_task(
                    task["id"], status="failed", message="本轮未完成，请检查模型或搜索设置"
                )
            # Do not persist upstream exception text (may contain provider secrets).
            store.update_run(
                run["id"],
                status="failed",
                error=type(exc).__name__,
                message="调查未完成，已保存现有资料。请检查模型与搜索连接后重试",
            )

    async def stop(self, run_id):
        store = self._store()
        run = store.get_run(run_id)
        task = self.workers.get(run_id)
        if task and not task.done():
            store.update_run(run_id, status="stopping", message="正在停止后续调查")
            task.cancel()
            await task
        elif run["status"] in {"queued", "planning", "running", "researching", "ingesting"}:
            store.update_run(run_id, status="interrupted", message="服务已中断；现有资料已保留")
        return {"run_id": run_id, "status": store.get_run(run_id)["status"]}

    def recover(self):
        store = self._store()
        for run in store.list_pending_runs():
            for task in store.snapshot(run["topic_id"]).get("tasks", []):
                if task.get("run_id") == run["id"] and task.get("status") in {
                    "queued",
                    "planning",
                    "running",
                    "researching",
                    "ingesting",
                    "stopping",
                }:
                    store.update_task(
                        task["id"], status="interrupted", message="服务重启，执行已中断"
                    )
            store.update_run(
                run["id"],
                status="interrupted",
                message="服务已重新启动，调查已暂停；重新开始可复用已有知识",
            )

    async def shutdown(self):
        for identity in list(self.workers):
            if not self.workers[identity].done():
                await self.stop(identity)


_manager: TopicResearchManager | None = None


def manager():
    global _manager
    if _manager is None:
        _manager = TopicResearchManager(research_store=ResearchDatabase())
    return _manager
