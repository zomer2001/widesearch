"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowRight, BookOpen, Check, Database, Download, ExternalLink, FileText, GitBranch, Layers3, Link2, Loader2, Network, Pencil, Plus, RefreshCw, Search, Table2, Trash2, Workflow, X } from "lucide-react";
import useDialogFocus from "@/hooks/useDialogFocus";
import useKnowledgeIngestion from "@/hooks/useKnowledgeIngestion";
import KnowledgeGraphCanvas from "./KnowledgeGraphCanvas";
import KnowledgeIngestionView from "./KnowledgeIngestionView";
import {
  KIND_LABELS, PREDICATE_LABELS, activeIngestion, attributeLabel, createKnowledgeEntity, createKnowledgeRelation, deleteKnowledgeEntity, deleteKnowledgeRelation, evidenceStatus, formatAttributeValue, getKnowledgeDatabase, graphRecords, knowledgeExportUrl, matchesEntity, relationKinds, safeEvidenceUrl, syncKnowledgeDatabase, updateKnowledgeEntity,
  type EntityInput, type KnowledgeEntity, type KnowledgeEvidence, type KnowledgeKind, type KnowledgeSnapshot,
} from "@/lib/knowledgeDatabase";

type Tab = "products" | "technologies" | "entities" | "relations" | "ingestion";
type Selection = { type: "entity" | "relation"; id: string };
type Editor = { type: "entity"; entity?: KnowledgeEntity } | { type: "relation" } | null;
const TABS = [
  { id: "products", name: "产品图谱", icon: Layers3 }, { id: "technologies", name: "技术图谱", icon: Network },
  { id: "entities", name: "实体表", icon: Table2 }, { id: "relations", name: "关系表", icon: GitBranch },
  { id: "ingestion", name: "自动入库", icon: Workflow },
] as const;
const SCALAR_VALUES: Record<string, string> = { not_run: "尚未运行", unknown: "未知", independent_reference: "独立参考", source_reported: "来源公开陈述", inferred: "分析判断", explicit: "原文明示", manual: "手工录入" };
function readable(value: unknown): string { const text = formatAttributeValue(value); return SCALAR_VALUES[text] || text; }
function displayDate(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? "暂无更新时间" : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }); }

export default function KnowledgeDatabasePanel({ onClose, onOpenLocalSource, sourceOpen = false, initialTab = "products" }: {
  onClose: () => void;
  onOpenLocalSource: (ref: string) => void;
  sourceOpen?: boolean;
  initialTab?: Tab;
}) {
  const root = useRef<HTMLDivElement>(null), close = useRef<HTMLButtonElement>(null);
  const editorRoot = useRef<HTMLElement>(null), editorClose = useRef<HTMLButtonElement>(null);
  const [data, setData] = useState<KnowledgeSnapshot | null>(null);
  const ingestion = useKnowledgeIngestion(setData);
  const ingestionActive = ingestion.data ? activeIngestion(ingestion.data) : 0;
  const [requestId, setRequestId] = useState(0), [loadedRequest, setLoadedRequest] = useState(-1);
  const [error, setError] = useState<string | null>(null), [notice, setNotice] = useState<string | null>(null);
  const [mutating, setMutating] = useState(false), [tab, setTab] = useState<Tab>(initialTab);
  const [query, setQuery] = useState(""), [kind, setKind] = useState<KnowledgeKind | "all">("all");
  const [selection, setSelection] = useState<Selection | null>(null), [focusId, setFocusId] = useState<string | null>(null);
  const [neighborsOnly, setNeighborsOnly] = useState(true), [editor, setEditor] = useState<Editor>(null), [pendingDelete, setPendingDelete] = useState<Selection | null>(null);
  const busy = requestId !== loadedRequest;
  useDialogFocus({ containerRef: root, initialFocusRef: close, onClose: () => { if (editor) setEditor(null); else if (pendingDelete) setPendingDelete(null); else onClose(); }, active: !sourceOpen && !editor });
  useDialogFocus({ containerRef: editorRoot, initialFocusRef: editorClose, onClose: () => setEditor(null), active: !sourceOpen && !!editor });
  useEffect(() => {
    const controller = new AbortController();
    void getKnowledgeDatabase(controller.signal).then((value) => {
      if (!controller.signal.aborted) { setData(value); setLoadedRequest(requestId); setError(null); }
    }).catch((cause) => { if (!controller.signal.aborted) { setLoadedRequest(requestId); setError(cause instanceof Error ? cause.message : "数据库读取失败。"); } });
    return () => controller.abort();
  }, [requestId]);
  const firstProduct = data?.entities.find((entity) => entity.kind === "product") ?? data?.entities[0];
  const selectedEntity = selection?.type === "entity" ? data?.entities.find((entity) => entity.id === selection.id) : selection ? undefined : firstProduct;
  const selectedRelation = selection?.type === "relation" ? data?.relations.find((relation) => relation.id === selection.id) : undefined;
  const currentFocus = focusId && data?.entities.some((entity) => entity.id === focusId) ? focusId : firstProduct?.id ?? null;
  const selectEntity = (id: string) => { setSelection({ type: "entity", id }); setPendingDelete(null); };
  const selectRelation = (id: string) => { setSelection({ type: "relation", id }); setPendingDelete(null); };
  const changeTab = (next: Tab) => {
    setTab(next); setQuery(""); setPendingDelete(null);
    if (next === "products" || next === "technologies") {
      const item = data?.entities.find((entity) => entity.kind === (next === "products" ? "product" : "technology"));
      if (item) { setFocusId(item.id); setSelection({ type: "entity", id: item.id }); }
    }
  };
  const mutate = async (action: () => Promise<unknown>, message: string) => {
    setMutating(true); setError(null); setNotice(null);
    try { await action(); setData(await getKnowledgeDatabase()); setNotice(message); setEditor(null); setPendingDelete(null); return true; }
    catch (cause) { setError(cause instanceof Error ? cause.message : "操作未完成，请重试。"); return false; }
    finally { setMutating(false); }
  };
  const sync = async () => {
    setMutating(true); setError(null); setNotice(null);
    try { const value = await syncKnowledgeDatabase(); setData(value); setNotice("已同步现有归档中的结构化条目，图谱已更新。"); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "归档同步失败。"); }
    finally { setMutating(false); }
  };
  const remove = () => {
    if (!pendingDelete) return;
    void mutate(() => pendingDelete.type === "entity" ? deleteKnowledgeEntity(pendingDelete.id) : deleteKnowledgeRelation(pendingDelete.id), pendingDelete.type === "entity" ? "条目及其关联连线已移出数据库视图，原文归档保留。" : "这条关系已移出图谱。此操作保留实体和原文。").then((success) => { if (success) setSelection(null); });
  };
  const entities = data?.entities.filter((entity) => matchesEntity(entity, query, kind)) ?? [];
  const entityMap = new Map(data?.entities.map((entity) => [entity.id, entity]));
  const relations = data?.relations.filter((relation) => !query.trim() || [entityMap.get(relation.source_id)?.name, entityMap.get(relation.target_id)?.name, relation.label].join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())) ?? [];
  const graph = data ? graphRecords(data, query, currentFocus, neighborsOnly) : { entities: [], relations: [] };
  const graphView = tab === "technologies" ? "technologies" : "products";
  const canOperate = !busy && !mutating;
  return <div className="kd-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !sourceOpen && !editor) onClose(); }}>
    <div ref={root} className="kd-panel" role="dialog" aria-modal="true" aria-labelledby="kd-title" tabIndex={-1}>
      <header className="kd-header"><div className="kd-heading"><span className="kd-logo"><Database size={23} /></span><div><span className="kd-eyebrow">知识数据库</span><h1 id="kd-title">Anduril · 产品与技术</h1></div></div><div className="kd-header-tools"><button className="kd-button" title="只同步现有归档中已结构化且有原文依据的条目；不会联网或调用模型" disabled={!canOperate} onClick={() => void sync()}><RefreshCw size={14} className={mutating ? "animate-spin" : ""} /><span>同步归档</span></button><details className="kd-export"><summary className="kd-button"><Download size={14} /><span>导出</span></summary><div><a href={knowledgeExportUrl("json")} download>完整数据库 JSON</a><a href={knowledgeExportUrl("csv")} download>实体表 CSV</a></div></details><button className="kd-icon-button" aria-label="刷新数据库" disabled={!canOperate} onClick={() => { setNotice(null); setRequestId((value) => value + 1); }}><RefreshCw size={17} className={busy ? "animate-spin" : ""} /></button><button ref={close} className="kd-icon-button" aria-label="关闭知识数据库" onClick={onClose}><X size={21} /></button></div></header>
      {busy && !data ? <div className="kd-empty kd-initial-loading" role="status"><Loader2 size={23} className="animate-spin" /><p>正在打开本地知识数据库…</p></div> : <>
        {error && <div className="kd-message kd-error" role="alert"><span>{error}</span><button disabled={!canOperate} onClick={() => setRequestId((value) => value + 1)}>重新读取</button></div>}
        {notice && <div className="kd-message kd-success" role="status"><Check size={14} /><span>{notice}</span><button aria-label="关闭提示" onClick={() => setNotice(null)}><X size={13} /></button></div>}
        {data && <>
          <div className="kd-overview"><div className="kd-overview-copy"><span className="kd-live-dot" /><span>本地保存 · 按实体与关系组织</span><span className="kd-updated">更新于 {displayDate(data.updated_at)}</span></div><div className="kd-stats"><span><strong>{data.entities.filter((item) => item.kind === "product").length}</strong>产品</span><span><strong>{data.entities.filter((item) => item.kind === "technology").length}</strong>技术</span><span><strong>{data.entities.filter((item) => item.kind === "route").length}</strong>路线</span><span><strong>{data.entities.filter((item) => item.kind === "reference").length}</strong>开源参考</span><span><strong>{data.relations.length}</strong>关系</span></div></div>
          <nav className="kd-navigation"><div role="tablist" aria-label="数据库视图" className="kd-tabs">{TABS.map(({ id, name, icon: Icon }) => <button id={`kd-tab-${id}`} key={id} role="tab" aria-selected={tab === id} aria-controls="kd-content" className={tab === id ? "active" : ""} onClick={() => changeTab(id)}><Icon size={16} />{name}{id === "ingestion" && ingestionActive > 0 && <span className="kd-tab-count">{ingestionActive}</span>}</button>)}</div><label className="kd-search"><Search size={15} /><input aria-label="搜索数据库实体与关系" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索产品、技术、路线…" />{query && <button aria-label="清除搜索" onClick={() => setQuery("")}><X size={13} /></button>}</label></nav>
          <main className={`kd-content ${tab === "ingestion" ? "kd-content-ingestion" : ""}`} id="kd-content" role="tabpanel" aria-labelledby={`kd-tab-${tab}`}>
            <section className="kd-primary">
              {tab === "ingestion" && <KnowledgeIngestionView data={ingestion.data} error={ingestion.error} actionError={ingestion.actionError} action={ingestion.action} notice={ingestion.notice} query={query} onRefresh={ingestion.refresh} onQueueLatest={() => { void ingestion.queueLatest(); }} onRetry={(id) => { void ingestion.retry(id); }} />}
              {(tab === "products" || tab === "technologies") && <>
                <div className="kd-graph-toolbar"><div><h2>{tab === "products" ? "从产品看技术组成" : "从技术看路线与应用"}</h2><p>{query.trim() ? `找到并展开「${query.trim()}」的直接关联` : neighborsOnly ? tab === "products" ? "当前为产品与技术/路线关联；型号、组件谱系待补充。" : "聚焦技术与路线、产品的直接关联，点击连线追溯依据。" : "展示全部实体和已保存的关系；未确认的关系留空。"}</p></div><div className="kd-graph-options"><select aria-label={tab === "products" ? "选择产品图谱中心" : "选择技术图谱中心"} value={currentFocus ?? ""} onChange={(event) => { setFocusId(event.target.value); selectEntity(event.target.value); setNeighborsOnly(true); setQuery(""); }}><option value="" disabled>选择中心对象</option>{data.entities.filter((entity) => entity.kind === (tab === "products" ? "product" : "technology")).map((entity) => <option key={entity.id} value={entity.id}>{entity.name}</option>)}</select><button className={`kd-button ${!neighborsOnly ? "selected" : ""}`} aria-pressed={!neighborsOnly} onClick={() => setNeighborsOnly((value) => !value)}>{neighborsOnly ? "查看全部" : "聚焦关联"}</button></div></div>
                <KnowledgeGraphCanvas key={`${graphView}-${currentFocus}-${neighborsOnly}-${query}`} entities={graph.entities} relations={graph.relations} view={graphView} selectedEntityId={selectedEntity?.id ?? null} selectedRelationId={selectedRelation?.id ?? null} onSelectEntity={selectEntity} onSelectRelation={selectRelation} />
                <footer className="kd-graph-footer"><span><strong>{graph.entities.length}</strong> 个实体 · <strong>{graph.relations.length}</strong> 条关系</span><span>连线来自数据库记录；手工关系与独立参考以虚线区分。</span></footer>
              </>}
              {tab === "entities" && <><div className="kd-table-toolbar"><div><h2>实体表</h2><p>每行是一个产品、技术机制、路线或开源参考。</p></div><div><select aria-label="按实体类型筛选" value={kind} onChange={(event) => setKind(event.target.value as KnowledgeKind | "all")}><option value="all">全部类型</option>{Object.entries(KIND_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><button className="kd-button kd-button-primary" disabled={!canOperate} onClick={() => setEditor({ type: "entity" })}><Plus size={14} />新增条目</button></div></div><div className="kd-table-wrap"><table><thead><tr><th>实体名称</th><th>类型</th><th>类别</th><th>关系</th><th>依据</th></tr></thead><tbody>{entities.map((entity) => <tr key={entity.id} className={selectedEntity?.id === entity.id ? "selected" : ""}><td><button className="kd-row-link" onClick={() => selectEntity(entity.id)}>{entity.name}<ArrowRight size={13} /></button></td><td><KindBadge kind={entity.kind} /></td><td>{entity.category || "未分类"}</td><td>{data.relations.filter((edge) => edge.source_id === entity.id || edge.target_id === entity.id).length}</td><td><span className={`kd-status kd-status-${entity.status}`}>{evidenceStatus(entity.status)}</span></td></tr>)}</tbody></table>{entities.length === 0 && <div className="kd-empty"><Database size={22} /><p>{query ? "没有找到匹配条目。" : "当前类型暂无条目。"}</p></div>}</div><div className="kd-table-foot">当前显示 {entities.length} 个实体 / 共 {data.entities.length} 个</div></>}
              {tab === "relations" && <><div className="kd-table-toolbar"><div><h2>关系表</h2><p>每条关系独立保存，并记录方向、类型和原文依据。</p></div><button className="kd-button kd-button-primary" disabled={!canOperate || data.entities.length < 2} onClick={() => setEditor({ type: "relation" })}><Link2 size={14} />新增关系</button></div><div className="kd-table-wrap"><table><thead><tr><th>起点实体</th><th>关系</th><th>终点实体</th><th>依据</th><th>操作</th></tr></thead><tbody>{relations.map((relation) => <tr key={relation.id} className={selectedRelation?.id === relation.id ? "selected" : ""}><td><button className="kd-row-link" onClick={() => selectEntity(relation.source_id)}>{entityMap.get(relation.source_id)?.name || "实体已移除"}</button></td><td><button className="kd-relation-link" onClick={() => selectRelation(relation.id)}>{relation.label || PREDICATE_LABELS[relation.predicate] || relation.predicate}<ArrowRight size={12} /></button></td><td><button className="kd-row-link" onClick={() => selectEntity(relation.target_id)}>{entityMap.get(relation.target_id)?.name || "实体已移除"}</button></td><td><span className={`kd-status kd-status-${relation.status}`}>{evidenceStatus(relation.status)}</span></td><td><button className="kd-icon-button" aria-label={`查看关系：${entityMap.get(relation.source_id)?.name} ${relation.label} ${entityMap.get(relation.target_id)?.name}`} onClick={() => selectRelation(relation.id)}><BookOpen size={14} /></button></td></tr>)}</tbody></table>{relations.length === 0 && <div className="kd-empty"><GitBranch size={22} /><p>{query ? "没有找到匹配关系。" : "尚无已保存关系。"}</p></div>}</div><div className="kd-table-foot">当前显示 {relations.length} 条关系 / 共 {data.relations.length} 条</div></>}
            </section>
            {tab !== "ingestion" && <aside className="kd-inspector" aria-label="实体与关系详情">
              {selectedEntity ? <><div className="kd-inspector-top"><KindBadge kind={selectedEntity.kind} /><span className={`kd-status kd-status-${selectedEntity.status}`}>{evidenceStatus(selectedEntity.status)}</span></div><h2>{selectedEntity.name}</h2><p className="kd-inspector-summary">{selectedEntity.summary || "尚未填写概述。"}</p><div className="kd-detail-actions"><button className="kd-button" disabled={!canOperate} onClick={() => { setFocusId(selectedEntity.id); setNeighborsOnly(true); setQuery(""); setTab(selectedEntity.kind === "product" ? "products" : "technologies"); }}><Network size={13} />以此为中心</button><button className="kd-button" disabled={!canOperate} onClick={() => setEditor({ type: "entity", entity: selectedEntity })}><Pencil size={13} />编辑</button><button className="kd-icon-button kd-danger" disabled={!canOperate} aria-label={`移除实体 ${selectedEntity.name}`} onClick={() => setPendingDelete({ type: "entity", id: selectedEntity.id })}><Trash2 size={14} /></button></div><EntityAttributes entity={selectedEntity} /><section className="kd-detail-section"><h3>关联实体 <span>{data.relations.filter((relation) => relation.source_id === selectedEntity.id || relation.target_id === selectedEntity.id).length}</span></h3>{data.relations.filter((relation) => relation.source_id === selectedEntity.id || relation.target_id === selectedEntity.id).map((relation) => { const outgoing = relation.source_id === selectedEntity.id, peer = entityMap.get(outgoing ? relation.target_id : relation.source_id); return <button key={relation.id} className="kd-adjacent" onClick={() => selectRelation(relation.id)}><span>{outgoing ? "→" : "←"} {relation.label}</span><strong>{peer?.name || "实体已移除"}</strong></button>; })}{!data.relations.some((relation) => relation.source_id === selectedEntity.id || relation.target_id === selectedEntity.id) && <p className="kd-muted">暂无已确认关系，条目保留为独立实体。</p>}</section><EvidenceNotes ids={selectedEntity.evidence_ids} evidence={data.evidence} onOpenLocalSource={onOpenLocalSource} /></> : selectedRelation ? <><span className="kd-eyebrow">关系详情</span><h2>{selectedRelation.label || selectedRelation.predicate}</h2><div className="kd-relation-detail"><button onClick={() => selectEntity(selectedRelation.source_id)}>{entityMap.get(selectedRelation.source_id)?.name}</button><span><ArrowRight size={16} />{selectedRelation.label}</span><button onClick={() => selectEntity(selectedRelation.target_id)}>{entityMap.get(selectedRelation.target_id)?.name}</button></div><span className={`kd-status kd-status-${selectedRelation.status}`}>{evidenceStatus(selectedRelation.status)}</span><p className="kd-muted kd-relation-origin">{selectedRelation.origin === "manual" ? "这条关系由你手工录入，补充来源前不视为已经核验。" : "这条关联来自已保存的结构化条目，可在下方核对原文。"}</p><button className="kd-button kd-danger" disabled={!canOperate} onClick={() => setPendingDelete({ type: "relation", id: selectedRelation.id })}><Trash2 size={13} />移除关系</button><EvidenceNotes ids={selectedRelation.evidence_ids} evidence={data.evidence} onOpenLocalSource={onOpenLocalSource} /></> : <div className="kd-empty"><Network size={24} /><p>选择一个实体或关系</p><span>在这里查看属性、关联和原文依据。</span></div>}
              {pendingDelete && <div className="kd-delete-confirm" role="alert"><strong>{pendingDelete.type === "entity" ? "移除这个实体及其关联连线？" : "移除这条关系？"}</strong><p>原文归档会保留。重新同步不会恢复已移除条目。</p><div><button className="kd-button" disabled={mutating} onClick={() => setPendingDelete(null)}>取消</button><button className="kd-button kd-danger" disabled={mutating} onClick={remove}>{mutating ? "正在移除…" : "确认移除"}</button></div></div>}
            </aside>}
          </main>
          <footer className="kd-bottom"><button className="kd-bottom-ingestion" onClick={() => changeTab("ingestion")}><Workflow size={13} />网页研究归档后自动抽取入库 · 查看进度{ingestionActive > 0 && `（${ingestionActive} 份处理中）`}</button><details><summary>当前数据范围与缺口</summary><ul>{data.gaps.length ? data.gaps.map((gap, index) => <li key={index}>{gap}</li>) : <li>当前数据范围以已保存的实体和来源为准。</li>}</ul></details></footer>
        </>}
      </>}
      {editor && data && <div className="kd-editor-backdrop"><section ref={editorRoot} className="kd-editor" role="dialog" aria-modal="true" aria-label={editor.type === "entity" ? editor.entity ? "编辑实体" : "新增实体" : "新增关系"}><div className="kd-editor-heading"><h2>{editor.type === "entity" ? editor.entity ? "编辑实体" : "新增实体" : "新增关系"}</h2><button ref={editorClose} className="kd-icon-button" aria-label="关闭编辑" disabled={mutating} onClick={() => setEditor(null)}><X size={18} /></button></div>{error && <p className="kd-form-error" role="alert">{error}</p>}{editor.type === "entity" ? <EntityEditor key={editor.entity?.id || "new"} entity={editor.entity} busy={mutating} onCancel={() => setEditor(null)} onSubmit={(input) => void mutate(async () => { const item = editor.entity ? await updateKnowledgeEntity(editor.entity.id, { name: input.name, summary: input.summary, category: input.category }) : await createKnowledgeEntity(input); setSelection({ type: "entity", id: item.id }); }, "实体已保存到本地数据库。")} /> : <RelationEditor entities={data.entities} busy={mutating} onCancel={() => setEditor(null)} onSubmit={(input) => void mutate(async () => { const relation = await createKnowledgeRelation(input); setSelection({ type: "relation", id: relation.id }); }, "关系已保存，已在图谱中更新。")} />}</section></div>}
    </div>
  </div>;
}
function KindBadge({ kind }: { kind: KnowledgeKind }) { return <span className={`kd-kind kd-kind-${kind}`}>{KIND_LABELS[kind]}</span>; }
function EntityAttributes({ entity }: { entity: KnowledgeEntity }) {
  const excluded = new Set(["name", "id", "summary", "overview", "category", "source_ids", "evidence_ids", "technology_ids", "product_ids", "route_ids", "related_technology_ids", "document_observations"]);
  const entries = Object.entries(entity.attributes ?? {}).filter(([key, value]) => !excluded.has(key) && value !== null && (!Array.isArray(value) || value.length > 0));
  const observations = Array.isArray(entity.attributes?.document_observations) ? entity.attributes.document_observations.filter((value): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value)) : [];
  return <section className="kd-detail-section"><h3>属性字段</h3><dl className="kd-attributes"><div><dt>类别</dt><dd>{entity.category || "未分类"}</dd></div>{entries.map(([key, value]) => <div key={key}><dt>{attributeLabel(key)}</dt><dd>{readable(value)}</dd></div>)}</dl>{observations.length > 0 && <details className="kd-observations"><summary>来源记录 · {observations.length}</summary><p>保留各份有效原文的独立描述，对应引句见下方原文依据。</p>{observations.map((observation, index) => <article key={index}><h4>来源记录 {index + 1}</h4>{typeof observation.summary === "string" && <p>{observation.summary}</p>}{observation.attributes !== null && typeof observation.attributes === "object" && !Array.isArray(observation.attributes) && <dl className="kd-attributes">{Object.entries(observation.attributes as Record<string, unknown>).filter(([key]) => !excluded.has(key)).map(([key, value]) => <div key={key}><dt>{attributeLabel(key)}</dt><dd>{readable(value)}</dd></div>)}</dl>}</article>)}</details>}</section>;
}
function EvidenceNotes({ ids, evidence, onOpenLocalSource }: { ids: string[]; evidence: KnowledgeEvidence[]; onOpenLocalSource: (ref: string) => void }) {
  const items = evidence.filter((item) => ids.includes(item.id));
  return <section className="kd-detail-section kd-evidence-section"><h3>原文依据 <span>{items.length}</span></h3>{items.length === 0 ? <p className="kd-muted">暂无原文引句；手工条目和关系需要继续补充证据。</p> : items.map((item, index) => <details key={item.id} className="kd-evidence" open={index === 0}><summary><FileText size={13} /><span>{item.title || "归档来源"}</span><span>{item.location}</span></summary><div>{item.quote && <blockquote>{item.quote}</blockquote>}{!item.available && <p className="kd-source-unavailable">此来源已不可用。</p>}<div className="kd-evidence-links">{safeEvidenceUrl(item.url) && <a href={safeEvidenceUrl(item.url)!} target="_blank" rel="noreferrer">原始网页<ExternalLink size={12} /></a>}{item.available && item.ref_id?.startsWith("local://") && <button onClick={() => onOpenLocalSource(item.ref_id!)}>本地原文<ArrowRight size={12} /></button>}</div></div></details>)}</section>;
}
function EntityEditor({ entity, busy, onSubmit, onCancel }: { entity?: KnowledgeEntity; busy: boolean; onSubmit: (input: EntityInput) => void; onCancel: () => void }) {
  const [kind, setKind] = useState<KnowledgeKind>(entity?.kind || "product"), [name, setName] = useState(entity?.name || ""), [summary, setSummary] = useState(entity?.summary || ""), [category, setCategory] = useState(entity?.category || "");
  const submit = (event: FormEvent) => { event.preventDefault(); if (name.trim()) onSubmit({ kind, name: name.trim(), summary: summary.trim(), category: category.trim() }); };
  return <form onSubmit={submit}><p className="kd-form-note">修改后的条目与关联会标为手工录入，需重新补充依据；原文归档保留。</p><label>实体类型<select value={kind} disabled={!!entity || busy} onChange={(event) => setKind(event.target.value as KnowledgeKind)}>{Object.entries(KIND_LABELS).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label><label>名称<input required maxLength={240} autoFocus value={name} disabled={busy} onChange={(event) => setName(event.target.value)} placeholder="例如：一种产品、技术机制或技术路线" /></label><label>类别<input maxLength={240} value={category} disabled={busy} onChange={(event) => setCategory(event.target.value)} placeholder="例如：分布式数据网络" /></label><label>概述<textarea rows={5} maxLength={12000} value={summary} disabled={busy} onChange={(event) => setSummary(event.target.value)} placeholder="记录它是什么、解决什么问题。" /></label><div className="kd-form-actions"><button type="button" className="kd-button" disabled={busy} onClick={onCancel}>取消</button><button type="submit" className="kd-button kd-button-primary" disabled={busy || !name.trim()}>{busy ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}保存实体</button></div></form>;
}
function RelationEditor({ entities, busy, onSubmit, onCancel }: { entities: KnowledgeEntity[]; busy: boolean; onSubmit: (input: { source_id: string; target_id: string; predicate: string }) => void; onCancel: () => void }) {
  const [source, setSource] = useState(""), [target, setTarget] = useState(""), [predicate, setPredicate] = useState("uses_technology");
  const kinds = relationKinds(predicate);
  const sourceChoices = entities.filter((entity) => kinds.source.includes(entity.kind));
  const sourceId = sourceChoices.some((entity) => entity.id === source) ? source : sourceChoices[0]?.id || "";
  const targetChoices = entities.filter((entity) => kinds.target.includes(entity.kind) && entity.id !== sourceId);
  const targetId = targetChoices.some((entity) => entity.id === target) ? target : targetChoices[0]?.id || "";
  const submit = (event: FormEvent) => { event.preventDefault(); if (sourceId && targetId && sourceId !== targetId) onSubmit({ source_id: sourceId, target_id: targetId, predicate }); };
  const choices = (items: KnowledgeEntity[]) => items.length ? items.map((entity) => <option key={entity.id} value={entity.id}>{entity.name} · {KIND_LABELS[entity.kind]}</option>) : <option value="">暂无适合的实体，请先新增条目</option>;
  return <form onSubmit={submit}><p className="kd-form-note">关系会以虚线显示并标为手工录入。独立开源参考不代表产品实际采用。</p><label>关系类型<select value={predicate} disabled={busy} onChange={(event) => setPredicate(event.target.value)}>{Object.entries(PREDICATE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>起点实体<select value={sourceId} disabled={busy || !sourceChoices.length} onChange={(event) => setSource(event.target.value)}>{choices(sourceChoices)}</select></label><label>终点实体<select value={targetId} disabled={busy || !targetChoices.length} onChange={(event) => setTarget(event.target.value)}>{choices(targetChoices)}</select></label><p className="kd-form-note">方向：{kinds.source.map((kind) => KIND_LABELS[kind]).join("或")} → {kinds.target.map((kind) => KIND_LABELS[kind]).join("或")}</p>{(!sourceId || !targetId) && <p className="kd-form-error">当前缺少适合这类关系的实体，请先新增对应条目。</p>}<div className="kd-form-actions"><button type="button" className="kd-button" disabled={busy} onClick={onCancel}>取消</button><button type="submit" className="kd-button kd-button-primary" disabled={busy || !sourceId || !targetId || sourceId === targetId}><Link2 size={14} />保存关系</button></div></form>;
}
