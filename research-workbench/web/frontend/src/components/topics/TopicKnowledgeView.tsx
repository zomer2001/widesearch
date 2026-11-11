"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { flexRender, getCoreRowModel, getSortedRowModel, useReactTable, type ColumnDef, type SortingState } from "@tanstack/react-table";
import type { Core, ElementDefinition, StylesheetJson } from "cytoscape";
import { ArrowDown, ArrowDownToLine, ArrowUp, BookOpen, Check, ChevronRight, Database, ExternalLink, FileText, GitBranch, Loader2, Maximize2, Minus, Network, Pencil, Plus, Search, Trash2, X, Combine, Undo2, Layers, ChevronDown } from "lucide-react";
import { API_BASE } from "@/lib/api";
import { KIND_LABELS, PREDICATE_LABELS, safeEvidenceUrl, type KnowledgeEntity, type KnowledgeEvidence, type KnowledgeKind, type KnowledgeRelation } from "@/lib/knowledgeDatabase";
import { additionalFields, branchKnowledge, changeText, defaultTopicGraphFocus, fieldEvidence, fieldStatusLabels, graphDegrees, graphLeafIds, expandGraph, observationContextText, projectedFieldCell, restorableFieldKeys, sourcesForBranch, scopedGraph, topicGraphRecords, topicUpdateText, type FieldDefinition } from "@/lib/topicKnowledge";
import type { TopicCluster, TopicSnapshot } from "@/lib/topics";

type View = "sources" | "data" | "graph" | "outputs";
interface Props { snapshot: TopicSnapshot; onOpenLocalSource: (ref: string) => void; onRefresh: () => void; onResearch?: (query: string) => void }
const views: { id: View; label: string; icon: typeof Database }[] = [{ id: "sources", label: "资料", icon: FileText }, { id: "data", label: "数据", icon: Database }, { id: "graph", label: "图谱", icon: Network }, { id: "outputs", label: "产出", icon: BookOpen }];
const kinds = Object.keys(KIND_LABELS) as KnowledgeKind[];
const colors: Record<KnowledgeKind, string> = { product: "#2563eb", technology: "#0891b2", route: "#7c3aed", reference: "#b45309" };
const shortKinds: Record<KnowledgeKind, string> = { product: "产品", technology: "技术", route: "路线", reference: "开源参考" };
function localDate(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }); }


export default function TopicKnowledgeView({ snapshot, onOpenLocalSource, onRefresh, onResearch }: Props) {
  const [branch, setBranch] = useState("");
  const [expanded, setExpanded] = useState<string[]>([]);
  const [hidden, setHidden] = useState<string[]>([]);
  const [showReview, setShowReview] = useState(false);
  const [reviewPair, setReviewPair] = useState<string | null>(null);
  const [view, setView] = useState<View>("data");
  const [kind, setKind] = useState<KnowledgeKind>("product");
  const [query, setQuery] = useState("");
  const [selection, setSelection] = useState<{ entity?: string; relation?: string; field?: FieldDefinition } | null>(null);
  const [perspective, setPerspective] = useState<"product" | "technology">("product");
  const [density, setDensity] = useState<"compact" | "full">("compact");
  const [mode, setMode] = useState<"network" | "lineage">("network");
  const [focus, setFocus] = useState<string | null>(() => defaultTopicGraphFocus(snapshot.knowledge, perspective));
  const [sorting, setSorting] = useState<SortingState>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const detailRef = useRef<HTMLElement>(null);
  const data = snapshot.knowledge;
  const clusters = snapshot.clusters ?? [];
  const activeBranch = clusters.find((item) => item.id === branch);
  const scoped = useMemo(() => branchKnowledge(data, activeBranch?.entity_ids), [data, activeBranch]);
  const branchRoot = activeBranch?.parent_id || activeBranch?.id;
  const roots = clusters.filter((item) => !item.parent_id);
  const children = clusters.filter((item) => item.parent_id === branchRoot);
  const projected = snapshot.entity_fields;
  const fieldCell = (item: KnowledgeEntity, definition: FieldDefinition) => projectedFieldCell(item, definition, projected?.[item.id]);
  const entity = data.entities.find((item) => item.id === selection?.entity);
  const relation = data.relations.find((item) => item.id === selection?.relation);
  const filter = query.trim().toLocaleLowerCase();
  const entities = useMemo(() => scoped.entities.filter((item) => item.kind === kind && (!query.trim() || [item.name, item.category, item.summary, ...Object.values(item.attributes).filter((value) => typeof value === "string")].join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))), [scoped.entities, kind, query]);
  // Heterogeneous topic-wide records share only identity and summary. Branch tables expose useful populated attributes.
  const templates = useMemo(() => {
    if (!activeBranch) return [];
    const minimum = Math.max(1, Math.ceil(entities.length * 0.25));
    const available = new Map<string, FieldDefinition>();
    for (const item of entities) for (const definition of projected?.[item.id] ?? snapshot.templates[kind] ?? []) available.set(definition.key, definition);
    return [...available.values()].filter((definition) => !["overview", "summary", "description", "limitations"].includes(definition.key)).map((definition) => ({ definition, count: entities.filter((item) => projectedFieldCell(item, definition, projected?.[item.id]).values.length > 0).length }))
      .filter((item) => item.count >= minimum).sort((a, b) => b.count - a.count).slice(0, 4).map((item) => item.definition);
  }, [activeBranch, entities, projected, snapshot.templates, kind]);
  const selectedFields = entity ? projected?.[entity.id] ?? [...(snapshot.templates[entity.kind] ?? []), ...additionalFields(entity, snapshot.templates[entity.kind] ?? [])] : [];
  const groups = [...new Set(selectedFields.map((item) => item.group || "其他字段"))];
  const sources = (activeBranch ? sourcesForBranch(snapshot.sources, scoped) : snapshot.sources).filter((source) => !filter || `${source.title} ${source.url}`.toLocaleLowerCase().includes(filter));
  const baseGraph = useMemo(() => topicGraphRecords(scoped, { perspective, density, mode, focusId: scoped.entities.some((item) => item.id === focus) ? focus : null, query }), [scoped, perspective, density, mode, focus, query]);
  const graph = useMemo(() => expandGraph({ ...scoped, relations: scoped.relations.filter((edge) => (mode !== "lineage" || ["variant_of", "part_of", "replaces", "evolves_from"].includes(edge.predicate)) && (density === "full" || edge.status === "source_backed" && edge.evidence_ids.length > 0)) }, baseGraph, expanded, hidden), [scoped, baseGraph, expanded, hidden, mode, density]);
  const field = selection?.field;
  const cell = entity && field ? fieldCell(entity, field) : null;
  const restoreKeys = entity && field ? restorableFieldKeys(entity, field, projected?.[entity.id]) : [];
  const selectedEvidence = cell ? fieldEvidence(cell, data.evidence) : [];
  const entityEvidence = data.evidence.filter((item) => (entity?.evidence_ids ?? relation?.evidence_ids ?? []).includes(item.id));
  const evidence = selectedEvidence.length ? selectedEvidence : entityEvidence;
  const selectEntity = (id: string, selectedField?: FieldDefinition) => { setSelection({ entity: id, field: selectedField }); setDeleteConfirm(false); setEditing(false); setError(""); };
  const selectBranch = (id: string) => { setBranch(id); setSelection(null); setQuery(""); setExpanded([]); setHidden([]); const next = branchKnowledge(data, clusters.find((item) => item.id === id)?.entity_ids); setFocus(density === "compact" ? defaultTopicGraphFocus(next, perspective) : null); };
  const candidates = (snapshot.duplicate_candidates ?? []).filter((item) => !activeBranch || activeBranch.entity_ids.includes(item.canonical_id) || activeBranch.entity_ids.includes(item.alias_id));
  const columns = useMemo<ColumnDef<KnowledgeEntity>[]>(() => [
    { id: "name", accessorKey: "name", header: "名称", cell: ({ row }) => <button className="tk-name" onClick={() => selectEntity(row.original.id)}><span>{row.original.name}</span><ChevronRight size={13} /></button> },
    { id: "summary", accessorKey: "summary", header: "概述", cell: ({ row }) => <button className="tk-field" onClick={() => selectEntity(row.original.id)}><span>{row.original.summary || row.original.category || "尚无概述"}</span></button> },
    ...templates.map((definition): ColumnDef<KnowledgeEntity> => ({ id: definition.key, accessorFn: (item) => projectedFieldCell(item, definition, projected?.[item.id]).values.map((value) => value.text).join("；"), header: definition.label, cell: ({ row }) => {
      const value = projectedFieldCell(row.original, definition, projected?.[row.original.id]);
      return <button className={`tk-field tk-field-${value.status}`} title={value.values.map((item) => item.text).join("；") || fieldStatusLabels[value.status]} onClick={() => selectEntity(row.original.id, definition)}>{value.values.length ? <><span>{value.values[0].text}</span>{["multiple", "contextual"].includes(value.status) && <small>{value.status === "contextual" ? "不同条件 · " : "来源差异 · "}{value.values.length} 个值</small>}{value.status === "manual" && <small>手工</small>}</> : <span className="tk-missing">{fieldStatusLabels[value.status]}</span>}</button>;
    } })),
    { id: "coverage", header: "字段", cell: ({ row }) => { const fields = projected?.[row.original.id] ?? []; const known = fields.filter((item) => item.status === "known" || item.status === "conflicting").length; return <button className="tk-coverage" onClick={() => selectEntity(row.original.id)}>{known}<span> / {fields.length || "—"}</span><ChevronRight size={12} /></button>; } },
  ], [templates, projected]);
  const table = useReactTable({ data: entities, columns, state: { sorting }, onSortingChange: setSorting, getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel() });

  useEffect(() => {
    if (!selection) return;
    detailRef.current?.focus();
    const key = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) { setSelection(null); setEditing(false); } };
    window.addEventListener("keydown", key); return () => window.removeEventListener("keydown", key);
  }, [selection, busy]);

  async function mutate(path: string, method: string, payload?: unknown) {
    setBusy(true); setError("");
    try {
      const response = await fetch(`${API_BASE}${path}`, { method, headers: { "Content-Type": "application/json" }, ...(payload === undefined ? {} : { body: JSON.stringify(payload) }), signal: AbortSignal.timeout(30000) });
      if (!response.ok) { let message = `操作失败（${response.status}）`; try { const body = await response.json(); if (typeof body.detail === "string") message = body.detail; } catch {} throw new Error(message); }
      onRefresh(); return true;
    } catch (failure) { setError(failure instanceof Error ? failure.message : "操作失败，请重试"); return false; }
    finally { setBusy(false); }
  }
  async function saveField(restore = false) {
    if (!entity || !field) return;
    const keys = restore ? restoreKeys : [field.key];
    if (!keys.length) return;
    for (const key of keys) {
      if (!await mutate(`/api/knowledge/entities/${encodeURIComponent(entity.id)}/fields/${encodeURIComponent(key)}`, "PATCH", { value: restore ? null : draft.trim() })) return;
    }
    setEditing(false);
  }

  function download() {
    const url = URL.createObjectURL(new Blob([topicUpdateText(snapshot)], { type: "text/plain;charset=utf-8" }));
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = `${snapshot.topic.title.replace(/[\\/:*?"<>|]/g, "_")}-知识库更新.txt`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const openSource = (ref: string) => { if (ref.startsWith("local://")) onOpenLocalSource(ref); };
  return <section className="tk-root" aria-label="专题知识库">
    <header className="tk-top"><nav className="tk-tabs" aria-label="知识库视图">{views.map(({ id, label, icon: Icon }) => <button key={id} aria-current={view === id ? "page" : undefined} className={view === id ? "active" : ""} onClick={() => { setView(id); setSelection(null); setQuery(""); }}><Icon size={16} />{label}</button>)}</nav><span className="tk-total">{data.entities.length} 个实体 · {data.relations.length} 条关系</span></header>
    {clusters.length > 0 && <nav className="tk-branches" aria-label="专题分层"><div><button className={!branch ? "active" : ""} onClick={() => selectBranch("")}><Layers size={14} />全部<span>{data.entities.length}</span></button>{roots.map((item) => <button key={item.id} className={branchRoot === item.id ? "active" : ""} onClick={() => selectBranch(item.id)}>{item.label}<span>{item.count}</span></button>)}<button className={`tk-review-toggle ${showReview ? "active" : ""}`} onClick={() => setShowReview(!showReview)}><Combine size={14} />实体整理{candidates.length > 0 && <span>{candidates.length}</span>}</button></div>{children.length > 0 && <div className="tk-subbranches">{children.map((item) => <button key={item.id} className={branch === item.id ? "active" : ""} onClick={() => selectBranch(item.id)}>{item.label}<span>{item.count}</span></button>)}</div>}</nav>}
    {showReview && <section className="tk-review" aria-label="实体消歧与合并"><div className="tk-section-head"><h3>实体整理</h3><button className="tk-link" onClick={() => setShowReview(false)}>收起</button></div><p>核对名称与依据后合并。原始记录保留，随时可以撤销。</p>{!candidates.length && <p className="tk-muted">当前范围没有待核对的重复候选</p>}{candidates.map((item) => <article key={item.alias_id}><div><strong>{item.alias_name}</strong><span>→</span><strong>{item.canonical_name}</strong></div><p>{item.reason}</p><div><button className="tk-link" onClick={() => selectEntity(item.alias_id)}>查看前者</button><button className="tk-link" onClick={() => selectEntity(item.canonical_id)}>查看后者</button><button className="tk-button" disabled={busy} onClick={() => setReviewPair(reviewPair === item.alias_id ? null : item.alias_id)}>核对合并</button></div>{reviewPair === item.alias_id && <div className="tk-merge-confirm"><span>将“{item.alias_name}”作为“{item.canonical_name}”的别名？</span><button className="tk-button tk-primary" disabled={busy} onClick={async () => { if (await mutate(`/api/topics/${encodeURIComponent(snapshot.topic.id)}/merges`, "POST", { canonical_id: item.canonical_id, alias_id: item.alias_id })) setReviewPair(null); }}>确认合并</button></div>}</article>)}{(snapshot.merges ?? []).length > 0 && <details><summary>已合并记录 · {snapshot.merges!.length}</summary>{snapshot.merges!.map((item) => <article key={item.alias_id}><div><span>{item.alias_name} → {item.canonical_name}</span><button className="tk-link" disabled={busy} onClick={() => void mutate(`/api/topics/${encodeURIComponent(snapshot.topic.id)}/merges/${encodeURIComponent(item.alias_id)}`, "DELETE")}><Undo2 size={12} />撤销</button></div></article>)}</details>}{error && <p className="tk-error" role="alert">{error}</p>}</section>}
    {activeBranch && <div className="tk-branch-context"><span>{activeBranch.label} <small>{activeBranch.count} 个对象</small></span>{onResearch && <button className="tk-link" onClick={() => onResearch(`调查 ${snapshot.topic.title} 的${activeBranch.label}，先核对本地资料，再补齐缺失的型号版本、时间、参数条件、技术路线与关系证据。`)}>调查此分支 <ChevronRight size={13} /></button>}</div>}
    {view !== "outputs" && <div className="tk-toolbar">
      {view === "data" && <div className="tk-kinds" aria-label="实体类型">{kinds.map((value) => <button key={value} className={kind === value ? "active" : ""} aria-pressed={kind === value} onClick={() => { setKind(value); setSorting([]); setSelection(null); }}>{shortKinds[value]}<span>{scoped.entities.filter((item) => item.kind === value).length}</span></button>)}</div>}
      {view === "sources" && <span className="tk-view-label">{sources.length} 份归档资料</span>}
      {view === "graph" && <div className="tk-graph-options"><select aria-label="图谱视角" value={perspective} onChange={(event) => { const next = event.target.value as "product" | "technology"; setExpanded([]); setHidden([]); setPerspective(next); setFocus(density === "compact" ? defaultTopicGraphFocus(scoped, next) : null); setMode("network"); setQuery(""); setSelection(null); }}><option value="product">产品图谱</option><option value="technology">技术图谱</option></select><div className="tk-graph-density" role="group" aria-label="图谱信息量"><button className={density === "compact" ? "active" : ""} aria-pressed={density === "compact"} onClick={() => { setExpanded([]); setHidden([]); setDensity("compact"); setFocus(defaultTopicGraphFocus(scoped, perspective)); setQuery(""); }}>精简</button><button className={density === "full" ? "active" : ""} aria-pressed={density === "full"} onClick={() => { setExpanded([]); setHidden([]); setDensity("full"); setFocus(null); setQuery(""); }}>全量</button></div><select value={mode} onChange={(event) => { setExpanded([]); setHidden([]); setMode(event.target.value as "network" | "lineage"); setFocus(event.target.value === "network" ? defaultTopicGraphFocus(scoped, perspective) : null); }} aria-label="图谱布局"><option value="network">网络布局</option>{perspective === "product" && <option value="lineage">谱系布局</option>}</select><select value={focus ?? ""} aria-label="聚焦实体" onChange={(event) => { setExpanded([]); setHidden([]); setFocus(event.target.value || null); setQuery(""); }}><option value="">全部对象</option>{scoped.entities.filter((item) => item.kind === perspective).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>}
      <label className="tk-search"><Search size={14} /><input aria-label={view === "sources" ? "搜索专题资料" : "搜索专题实体"} placeholder={view === "sources" ? "搜索资料" : "搜索实体"} value={query} onChange={(event) => setQuery(event.target.value)} />{query && <button aria-label="清空搜索" onClick={() => setQuery("")}><X size={13} /></button>}</label>
    </div>}
    <div className="tk-content">
      {view === "data" && <><div className="tk-table-scroll"><table className="tk-table"><thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th key={header.id}><button onClick={header.column.getToggleSortingHandler()}>{flexRender(header.column.columnDef.header, header.getContext())}{header.column.getIsSorted() === "asc" ? <ArrowUp size={12} /> : header.column.getIsSorted() === "desc" ? <ArrowDown size={12} /> : null}</button></th>)}</tr>)}</thead><tbody>{table.getRowModel().rows.map((row) => <tr key={row.original.id} className={entity?.id === row.original.id ? "selected" : ""}>{row.getVisibleCells().map((column) => <td key={column.id}>{flexRender(column.column.columnDef.cell, column.getContext())}</td>)}</tr>)}</tbody></table></div>{!entities.length && <Empty icon={Database} text={query ? "没有匹配的实体" : `还没有${shortKinds[kind]}记录`} />}<footer className="tk-foot"><span>{entities.length} 条记录</span><span>点击记录查看完整字段与来源</span></footer></>}
      {view === "sources" && <div className="tk-source-list">{sources.map((source) => <article className="tk-source" key={source.id}><span className="tk-source-icon"><FileText size={19} /></span><div><button className="tk-source-title" disabled={!source.available || !source.ref_id?.startsWith("local://")} onClick={() => openSource(source.ref_id)}>{source.title || "未命名资料"}</button><p>{safeEvidenceUrl(source.url) ? new URL(source.url).hostname : "本地资料"}<span>·</span>{source.available ? "正文已归档" : "原文不可用"}</p></div><div className="tk-source-actions">{source.available && source.ref_id?.startsWith("local://") && <button onClick={() => openSource(source.ref_id)} aria-label={`阅读 ${source.title}`}><BookOpen size={15} /></button>}{safeEvidenceUrl(source.url) && <a href={source.url} target="_blank" rel="noreferrer" aria-label={`打开原始网页 ${source.title}`}><ExternalLink size={15} /></a>}</div></article>)}{!sources.length && <Empty icon={FileText} text={query ? "没有匹配的资料" : activeBranch ? "该分支暂无已关联资料" : "本专题尚无归档资料"} />}</div>}
      {view === "graph" && <><TopicGraph storageKey={`${snapshot.topic.id}:${branch}`} graph={graph} mode={mode} focusId={focus} onSelect={(type, id) => { setSelection(type === "entity" ? { entity: id } : { relation: id }); setEditing(false); setDeleteConfirm(false); setError(""); }} /><footer className="tk-foot"><div className="tk-legend">{kinds.map((value) => <span key={value}><i style={{ background: colors[value] }} />{shortKinds[value]}</span>)}</div><span>{graph.entities.length} 个节点 · {graph.relations.length} 条关系{density === "compact" && baseGraph.hidden > 0 && ` · 可切换全量展开`}</span></footer>{mode === "lineage" && !graph.relations.length && graph.entities.length > 0 && <p className="tk-graph-note">尚无有依据的型号或组成关系</p>}</>}
      {view === "outputs" && <div className="tk-output"><div className="tk-output-title"><div><h3>知识库更新</h3><p>{snapshot.changes.length} 次已记录变化</p></div><button className="tk-button" onClick={download}><ArrowDownToLine size={14} />导出记录</button></div>{snapshot.changes.length ? <ol className="tk-changes">{snapshot.changes.map((change) => <li key={change.id}><span className="tk-change-dot"><Check size={12} /></span><div><time dateTime={change.created_at}>{localDate(change.created_at)}</time><p>{changeText(change)}</p></div></li>)}</ol> : <Empty icon={GitBranch} text="首次研究入库后，变化会出现在这里" />}{snapshot.gaps.length > 0 && <details className="tk-gaps"><summary>待补字段 <span>{snapshot.gaps.length}</span></summary><ul>{snapshot.gaps.slice(0, 30).map((gap, index) => <li key={`${gap.entity_id}-${gap.field}-${index}`}><button onClick={() => { setView("data"); setKind(gap.kind as KnowledgeKind); selectEntity(gap.entity_id, { key: gap.field, label: gap.label, type: "text" }); }}>{gap.entity_name}<span>{gap.label}</span><ChevronRight size={13} /></button></li>)}</ul>{snapshot.gaps.length > 30 && <p>其余缺项可在数据表中查看</p>}</details>}</div>}
    </div>
    {(entity || relation) && <aside ref={detailRef} tabIndex={-1} className="tk-inspector" aria-label="知识详情"><div className="tk-inspector-head"><span>{entity ? shortKinds[entity.kind] : "关系依据"}</span><button aria-label="关闭知识详情" disabled={busy} onClick={() => setSelection(null)}><X size={18} /></button></div>
      {entity ? <><h3>{entity.name}</h3>{entity.summary && <p className="tk-summary">{entity.summary}</p>}<div className="tk-detail-actions"><button className="tk-button" onClick={() => { setFocus(entity.id); setPerspective(entity.kind === "product" ? "product" : "technology"); setView("graph"); setSelection(null); setMode("network"); }}><Network size={14} />查看关联</button>{view === "graph" && <><button className="tk-button" onClick={() => { setExpanded((ids) => [...new Set([...ids, entity.id])]); setHidden([]); }}><Plus size={13} />展开一层</button><button className="tk-button" onClick={() => { const leaves = graphLeafIds(graph, entity.id).filter((id) => id !== focus); setHidden((ids) => [...new Set([...ids, ...leaves])]); }}><Minus size={13} />收起叶子</button></>}<button className="tk-button tk-remove" onClick={() => setDeleteConfirm(!deleteConfirm)}><Trash2 size={13} />移出专题</button></div>
        {!field && clusters.length > 0 && <ClassificationEditor key={`${entity.id}:${JSON.stringify(snapshot.classification_overrides?.[entity.id] ?? snapshot.entity_clusters?.[entity.id])}`} clusters={clusters} selected={snapshot.entity_clusters?.[entity.id] ?? []} manual={snapshot.entity_classification_origin?.[entity.id] === "manual"} busy={busy} onSave={(ids) => mutate(`/api/topics/${encodeURIComponent(snapshot.topic.id)}/entities/${encodeURIComponent(entity.id)}/classification`, "PATCH", { cluster_ids: ids })} />}
        {deleteConfirm && <div className="tk-confirm"><p>将此实体移出本专题？原文和其他专题中的记录会保留。</p><button className="tk-button" disabled={busy} onClick={async () => { if (await mutate(`/api/topics/${encodeURIComponent(snapshot.topic.id)}/entities/${encodeURIComponent(entity.id)}`, "DELETE")) { setSelection(null); setDeleteConfirm(false); } }}>{busy ? "处理中…" : "确认移出"}</button></div>}
        {field && cell ? <section className="tk-field-detail"><div className="tk-section-head"><h4>{field.label}</h4><button className="tk-link" onClick={() => { setEditing(true); setDraft(cell.values[0]?.text ?? ""); }}><Pencil size={13} />编辑</button></div>{!cell.values.length ? <p className="tk-muted">{fieldStatusLabels[cell.status]}</p> : cell.values.map((value, index) => <p key={index} className="tk-value">{value.text}{value.contexts?.map((context, contextIndex) => <small className="tk-observation-context" key={contextIndex}>{observationContextText(context)}</small>)}{cell.status === "manual" && <small>手工维护</small>}</p>)}{cell.status === "multiple" && <p className="tk-muted">保留了不同来源的值</p>}{cell.status === "contextual" && <p className="tk-muted">各值对应不同版本或条件</p>}{projected?.[entity.id]?.find((item) => item.key === field.key)?.observations.some((item) => item.active === false) && <details className="tk-field-history"><summary>查看来源中的历史值</summary>{projected[entity.id].find((item) => item.key === field.key)!.observations.filter((item) => item.active === false).map((item, index) => <p key={index}>{typeof item.value === "string" ? item.value : JSON.stringify(item.value)}{item.context && <small className="tk-observation-context">{observationContextText(item.context)}</small>}</p>)}</details>}
          {editing && <form className="tk-edit" onSubmit={(event) => { event.preventDefault(); void saveField(); }}><textarea aria-label={`编辑 ${field.label}`} value={draft} onChange={(event) => setDraft(event.target.value)} rows={4} maxLength={12000} autoFocus /><p>修改会应用到所有引用此实体的专题，并标记为手工维护。</p><div><button className="tk-button tk-primary" type="submit" disabled={busy || !draft.trim()}>{busy ? <Loader2 size={13} className="tk-spin" /> : <Check size={13} />}保存</button><button className="tk-button" type="button" disabled={busy} onClick={() => setEditing(false)}>取消</button>{restoreKeys.length > 0 && <button className="tk-link" type="button" disabled={busy} onClick={() => void saveField(true)}>恢复来源值</button>}</div></form>}
          <button className="tk-back" onClick={() => setSelection({ entity: entity.id })}>查看全部字段</button>
        </section> : <div className="tk-field-groups">{groups.map((group, groupIndex) => {
          const definitions = selectedFields.filter((item) => (item.group || "其他字段") === group);
          const recorded = definitions.filter((definition) => fieldCell(entity, definition).values.length > 0);
          const absent = definitions.filter((definition) => !fieldCell(entity, definition).values.length);
          const renderRows = (items: FieldDefinition[]) => items.map((definition) => { const value = fieldCell(entity, definition); return <div key={definition.key} className={`tk-detail-status-${value.status}`}><dt>{definition.label}</dt><dd><button onClick={() => selectEntity(entity.id, definition)}><span>{value.values[0]?.text ?? fieldStatusLabels[value.status]}</span>{value.values.length > 1 && <small>另 {value.values.length - 1} 个值</small>}<ChevronRight size={12} /></button></dd></div>; });
          return <details key={group} open={recorded.length > 0 || groupIndex === 0}><summary>{group}<span>{recorded.length} / {definitions.length}</span><ChevronDown size={13} /></summary>{recorded.length > 0 && <dl className="tk-attributes">{renderRows(recorded)}</dl>}{absent.length > 0 && <details className="tk-absent-fields"><summary>{["missing", "not_public", "not_applicable"].map((status) => { const count = absent.filter((definition) => fieldCell(entity, definition).status === status).length; return count ? `${fieldStatusLabels[status as keyof typeof fieldStatusLabels]} ${count}` : ""; }).filter(Boolean).join(" · ")}</summary><dl className="tk-attributes">{renderRows(absent)}</dl></details>}</details>;
        })}</div>}


      </> : relation ? <><h3>{relation.label || PREDICATE_LABELS[relation.predicate] || relation.predicate}</h3><div className="tk-relation-path"><button onClick={() => selectEntity(relation.source_id)}>{data.entities.find((item) => item.id === relation.source_id)?.name}</button><ArrowDown size={16} /><button onClick={() => selectEntity(relation.target_id)}>{data.entities.find((item) => item.id === relation.target_id)?.name}</button></div></> : null}
      {entity && !field && data.relations.some((edge) => edge.source_id === entity.id || edge.target_id === entity.id) && <section className="tk-adjacent"><h4>关联对象</h4>{data.relations.filter((edge) => edge.source_id === entity.id || edge.target_id === entity.id).map((edge) => <button key={edge.id} onClick={() => setSelection({ relation: edge.id })}><span>{edge.label || PREDICATE_LABELS[edge.predicate] || edge.predicate}</span><strong>{data.entities.find((item) => item.id === (edge.source_id === entity.id ? edge.target_id : edge.source_id))?.name}</strong><ChevronRight size={12} /></button>)}</section>}
      {error && <p className="tk-error" role="alert">{error}</p>}
      {entity && field && !selectedEvidence.length && <p className="tk-muted">此字段尚未关联专属引句，以下为实体层面的来源。</p>}
      <Evidence items={evidence} title={selectedEvidence.length ? "字段依据" : entity ? "实体来源" : "关系依据"} onOpenSource={openSource} />
    </aside>}
  </section>;
}

function ClassificationEditor({ clusters, selected, manual, busy, onSave }: { clusters: TopicCluster[]; selected: string[]; manual: boolean; busy: boolean; onSave: (ids: string[] | null) => Promise<boolean> }) {
  const [draft, setDraft] = useState(selected);
  return <details className="tk-classification"><summary><Layers size={13} />所属分类<span>{manual ? "手工调整" : "自动归类"}</span><ChevronDown size={13} /></summary><div className="tk-classification-options">{clusters.map((cluster) => <label className={cluster.parent_id ? "child" : ""} key={cluster.id}><input type="checkbox" checked={draft.includes(cluster.id)} disabled={busy} onChange={(event) => setDraft((ids) => event.target.checked ? [...new Set([...ids, cluster.id])] : ids.filter((id) => id !== cluster.id))} /><span>{cluster.label}</span></label>)}</div><div className="tk-classification-actions"><button className="tk-button" disabled={busy} onClick={() => void onSave(draft)}>保存分类</button><button className="tk-link" disabled={busy} onClick={() => { setDraft([]); }}>清空选择</button>{manual && <button className="tk-link" disabled={busy} onClick={() => void onSave(null)}><Undo2 size={12} />恢复自动</button>}</div></details>;
}

function Empty({ icon: Icon, text }: { icon: typeof Database; text: string }) { return <div className="tk-empty"><Icon size={26} strokeWidth={1.3} /><p>{text}</p></div>; }
function Evidence({ items, title, onOpenSource }: { items: KnowledgeEvidence[]; title: string; onOpenSource: (ref: string) => void }) {
  return <section className="tk-evidence"><h4>{title}<span>{items.length}</span></h4>{!items.length && <p className="tk-muted">尚无可追溯引句</p>}{items.map((item, index) => <details key={item.id} open={index === 0}><summary><FileText size={13} /><span>{item.title || "来源"}</span></summary>{item.quote && <blockquote>{item.quote}</blockquote>}<div className="tk-evidence-links">{item.available && item.ref_id?.startsWith("local://") && <button onClick={() => onOpenSource(item.ref_id!)}>查看原文 {item.location || ""}<ChevronRight size={12} /></button>}{safeEvidenceUrl(item.url) && <a href={item.url} target="_blank" rel="noreferrer"><ExternalLink size={13} />网页</a>}</div>{!item.available && <p className="tk-muted">本地原文不可用</p>}</details>)}</section>;
}

function TopicGraph({ storageKey, graph, mode, focusId, onSelect }: { storageKey: string; graph: ReturnType<typeof scopedGraph>; mode: "network" | "lineage"; focusId: string | null; onSelect: (type: "entity" | "relation", id: string) => void }) {
  const container = useRef<HTMLDivElement>(null), instance = useRef<Core | null>(null), callback = useRef(onSelect);
  const [failure, setFailure] = useState("");
  // Polling snapshots are new objects. Only rebuild when rendered graph data changes.
  const graphData = JSON.stringify({ entities: graph.entities.map(({ id, name, kind }) => ({ id, name, kind })), relations: graph.relations.map(({ id, source_id, target_id, label, predicate, origin }) => ({ id, source_id, target_id, label, predicate, origin })) });
  useEffect(() => { callback.current = onSelect; }, [onSelect]);
  useEffect(() => {
    const content = JSON.parse(graphData) as { entities: Pick<KnowledgeEntity, "id" | "name" | "kind">[]; relations: KnowledgeRelation[] };
    const element = container.current;
    if (!element || !content.entities.length) return;
    let disposed = false;
    let observer: ResizeObserver | undefined;
    void import("cytoscape").then(({ default: cytoscape }) => {
      if (disposed) return;
      const degrees = graphDegrees(content as Pick<TopicSnapshot["knowledge"], "entities" | "relations">);
      const items: ElementDefinition[] = [
        ...content.entities.map((entity) => ({ data: { id: entity.id, label: entity.name, color: colors[entity.kind], kind: entity.kind, degree: degrees.get(entity.id) ?? 0, size: Math.min(78, (entity.kind === "product" ? 38 : 30) + Math.sqrt(degrees.get(entity.id) ?? 0) * 9) } })),
        ...content.relations.map((relation) => ({ data: { id: `edge:${relation.id}`, relationId: relation.id, source: relation.source_id, target: relation.target_id, label: relation.label || PREDICATE_LABELS[relation.predicate] || relation.predicate, manual: relation.origin === "manual" ? "yes" : "no" } })),
      ];
      const style: StylesheetJson = [
        { selector: "node", style: { "background-color": "data(color)", width: "data(size)", height: "data(size)", label: "data(label)", color: "#334155", "font-size": 12, "font-weight": 500, "font-family": "Inter, PingFang SC, Microsoft YaHei, sans-serif", "text-valign": "bottom", "text-margin-y": 10, "text-wrap": "wrap", "text-max-width": "128px", "text-overflow-wrap": "anywhere", "line-height": 1.35, "border-color": "#ffffff", "border-width": 3, "overlay-opacity": 0 } },
        { selector: 'node[kind="product"]', style: { shape: "round-rectangle", "corner-radius": "12px" } },
        { selector: 'node[kind="reference"]', style: { shape: "hexagon" } },
        { selector: 'node[kind="route"]', style: { shape: "diamond" } },
        { selector: "node.hover", style: { "border-color": "#93b4ed", "border-width": 4 } },
        { selector: "edge", style: { width: 1.3, "line-color": "#aebfd2", "target-arrow-color": "#8fa6bf", "target-arrow-shape": "triangle", "arrow-scale": 0.65, "curve-style": "bezier", "overlay-opacity": 0, "font-size": 10, color: "#475569", "text-rotation": "autorotate", "text-background-color": "#fff", "text-background-opacity": 0.9, "text-background-padding": "3px" } },
        { selector: 'edge[manual="yes"]', style: { "line-style": "dashed" } },
        { selector: "node:selected", style: { "border-color": "#0f172a", "border-width": 3 } },
        { selector: "edge:selected, edge.hover", style: { "line-color": "#2563eb", "target-arrow-color": "#2563eb", width: 2.5, label: "data(label)" } },
      ];
      const cy = cytoscape({ container: element, elements: items, style,
        layout: mode === "lineage"
          ? { name: "breadthfirst", directed: true, padding: 55, spacingFactor: 1.65, nodeDimensionsIncludeLabels: true, avoidOverlap: true }
          : content.entities.length <= 10
            ? { name: "concentric", animate: false, padding: 45, nodeDimensionsIncludeLabels: true, avoidOverlap: true, minNodeSpacing: 58, spacingFactor: 1.15, concentric: (node) => node.id() === focusId ? 2 : 0, levelWidth: () => 1, startAngle: -Math.PI / 2 }
            : { name: "cose", animate: false, padding: 45, nodeDimensionsIncludeLabels: true, randomize: true, nodeRepulsion: () => 160000, idealEdgeLength: () => 230, edgeElasticity: () => 80, gravity: 0.15, nodeOverlap: 30, componentSpacing: 150, numIter: 1800 },
        minZoom: 0.12, maxZoom: 3, wheelSensitivity: 0.2, boxSelectionEnabled: false, autoungrabify: false });
      const positionKey = `topic-graph-positions:${storageKey}:${mode}`;
      let saved: Record<string, { x: number; y: number }> = {};
      try { saved = JSON.parse(sessionStorage.getItem(positionKey) || "{}"); } catch { /* Storage is optional. */ }
      for (const [id, position] of Object.entries(saved)) if (position && Number.isFinite(position.x) && Number.isFinite(position.y)) cy.getElementById(id).position(position);
      cy.on("dragfree", "node", (event) => { saved[event.target.id()] = event.target.position(); try { sessionStorage.setItem(positionKey, JSON.stringify(saved)); } catch { /* Dragging still works without storage. */ } });
      cy.on("mouseover", "node", (event) => event.target.addClass("hover"));
      cy.on("mouseout", "node", (event) => event.target.removeClass("hover"));
      const fit = () => { cy.fit(undefined, 45); if (cy.zoom() > 1.15) cy.zoom(1.15); cy.center(); };
      fit();
      instance.current = cy;
      cy.on("tap", "node", (event) => callback.current("entity", event.target.id()));
      cy.on("tap", "edge", (event) => callback.current("relation", event.target.data("relationId")));
      cy.on("mouseover", "edge", (event) => event.target.addClass("hover"));
      cy.on("mouseout", "edge", (event) => event.target.removeClass("hover"));
      observer = new ResizeObserver(() => { if (!disposed) cy.resize(); });
      observer.observe(element);
    }).catch(() => { if (!disposed) setFailure("图谱组件加载失败，请刷新页面重试"); });
    return () => { disposed = true; observer?.disconnect(); instance.current?.destroy(); instance.current = null; };
  }, [graphData, mode, focusId, storageKey]);
  if (!graph.entities.length) return <Empty icon={Network} text="当前范围没有图谱节点" />;
  return <div className="tk-graph-wrap"><div className="tk-graph-canvas" ref={container} role="img" aria-label={`知识图谱：${graph.entities.length} 个实体，${graph.relations.length} 条关系。可在数据视图查看全部实体。`} />{failure && <p className="tk-error" role="alert">{failure}</p>}<span className="tk-graph-hint">拖拽节点调整布局 · 点击查看关联</span><div className="tk-graph-controls"><button aria-label="放大图谱" onClick={() => { const cy = instance.current; if (cy) cy.zoom({ level: cy.zoom() * 1.25, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } }); }}><Plus size={16} /></button><button aria-label="缩小图谱" onClick={() => { const cy = instance.current; if (cy) cy.zoom({ level: cy.zoom() / 1.25, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } }); }}><Minus size={16} /></button><button aria-label="图谱适应画布" onClick={() => instance.current?.fit(undefined, 55)}><Maximize2 size={16} /></button></div></div>;
}
