import { API_BASE } from "./api";

export type KnowledgeKind = "product" | "technology" | "route" | "reference";
export type KnowledgeView = "products" | "technologies";
export interface KnowledgeEntity {
  id: string;
  kind: KnowledgeKind;
  name: string;
  summary: string;
  category: string;
  attributes: Record<string, unknown>;
  field_overrides?: Record<string, { value: string | null; updated_at: string }>;
  field_evidence?: Record<string, string[]>;
  evidence_ids: string[];
  origin: "archive" | "manual";
  status: "source_backed" | "manual" | "source_unavailable";
}
export interface KnowledgeRelation {
  id: string;
  source_id: string;
  target_id: string;
  predicate: string;
  label: string;
  evidence_ids: string[];
  origin: "archive" | "manual";
  status: "source_backed" | "manual" | "source_unavailable";
}
export interface KnowledgeEvidence {
  id: string;
  library_id?: string;
  document_id?: string;
  title: string;
  url: string;
  ref_id?: string;
  quote?: string;
  location?: string;
  available: boolean;
}
export interface KnowledgeSnapshot {
  title: string;
  updated_at: string;
  counts: { entities: number; relations: number; products: number; technologies: number; routes: number; references: number };
  entities: KnowledgeEntity[];
  relations: KnowledgeRelation[];
  evidence: KnowledgeEvidence[];
  gaps: string[];
}
export interface EntityInput {
  kind: KnowledgeKind;
  name: string;
  summary?: string;
  category?: string;
  attributes?: Record<string, unknown>;
}
export type IngestionStatus = "queued" | "running" | "completed" | "partial" | "failed" | "skipped";
export interface KnowledgeIngestionJob {
  id: string;
  session_id: string;
  document_id: string;
  title: string;
  status: IngestionStatus;
  message: string;
  engine: string;
  model: string;
  created_at: string;
  updated_at: string;
  entities: number;
  relations: number;
  error?: string;
  model_calls?: number;
  attempt?: number;
}
export interface KnowledgeIngestionSnapshot {
  enabled: boolean;
  counts: Record<IngestionStatus, number>;
  jobs: KnowledgeIngestionJob[];
  scope_hint?: string;
  scheduled?: number;
  reused?: number;
  session_id?: string;
}
export const INGESTION_LABELS: Record<IngestionStatus, string> = {
  queued: "等待抽取", running: "正在抽取", completed: "已入库", partial: "部分入库", failed: "抽取失败", skipped: "已跳过",
};
export type IngestionFilter = "all" | "active" | "completed" | "attention";
export function activeIngestion(data: KnowledgeIngestionSnapshot): number { return data.counts.queued + data.counts.running; }
export function matchesIngestion(job: KnowledgeIngestionJob, query: string, filter: IngestionFilter = "all"): boolean {
  const matches = !query.trim() || [job.title, job.session_id, job.message, job.model].join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
  return matches && (filter === "all" || filter === "active" && ["queued", "running"].includes(job.status) || filter === "completed" && job.status === "completed" || filter === "attention" && ["partial", "failed", "skipped"].includes(job.status));
}
export const KIND_LABELS: Record<KnowledgeKind, string> = { product: "产品 / 平台", technology: "技术机制", route: "技术路线", reference: "开源参考" };
export const PREDICATE_LABELS: Record<string, string> = {
  variant_of: "型号属于", part_of: "组成部分", replaces: "替代", uses_technology: "涉及技术", has_route: "关联路线", reference_for: "独立参考",
  carries: "可搭载", deploys: "可部署", integrates: "集成", evolves_from: "演进自",
};
export function relationKinds(predicate: string): { source: KnowledgeKind[]; target: KnowledgeKind[] } {
  if (predicate === "uses_technology") return { source: ["product"], target: ["technology"] };
  if (predicate === "has_route") return { source: ["product", "technology"], target: ["route"] };
  if (predicate === "reference_for") return { source: ["reference"], target: ["technology", "route"] };
  return { source: ["product"], target: ["product"] };
}
export function evidenceStatus(status: string): string {
  return status === "source_backed" ? "有原文依据" : status === "source_unavailable" ? "原文已不可用" : "手工录入 · 待补证据";
}
export function safeEvidenceUrl(value: string | undefined): string | null {
  if (!value) return null;
  try { const parsed = new URL(value); return ["https:", "http:"].includes(parsed.protocol) && !parsed.username && !parsed.password ? value : null; } catch { return null; }
}
async function send<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const timeout = AbortSignal.timeout(30000);
  const response = await fetch(`${API_BASE}/api/knowledge${path}`, {
    method, cache: "no-store", signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    let message = `操作未完成（${response.status}）`;
    try { const error = await response.json(); if (typeof error.detail === "string") message = error.detail; } catch { /* Keep the status when the response has no message. */ }
    throw new Error(message);
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}
export const getKnowledgeDatabase = (signal?: AbortSignal) => send<KnowledgeSnapshot>("", "GET", undefined, signal);
export const syncKnowledgeDatabase = () => send<KnowledgeSnapshot>("/sync", "POST");
export const createKnowledgeEntity = (input: EntityInput) => send<KnowledgeEntity>("/entities", "POST", input);
export const updateKnowledgeEntity = (id: string, input: Omit<EntityInput, "kind">) => send<KnowledgeEntity>(`/entities/${encodeURIComponent(id)}`, "PATCH", input);
export const deleteKnowledgeEntity = (id: string) => send<void>(`/entities/${encodeURIComponent(id)}`, "DELETE");
export const createKnowledgeRelation = (input: { source_id: string; target_id: string; predicate: string }) => send<KnowledgeRelation>("/relations", "POST", input);
export const deleteKnowledgeRelation = (id: string) => send<void>(`/relations/${encodeURIComponent(id)}`, "DELETE");
export const knowledgeExportUrl = (format: "json" | "csv") => `${API_BASE}/api/knowledge/export?format=${format}`;
export const getKnowledgeIngestion = (signal?: AbortSignal, sessionId?: string) => send<KnowledgeIngestionSnapshot>(`/ingestion${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ""}`, "GET", undefined, signal);
export const startKnowledgeIngestion = (sessionId?: string, signal?: AbortSignal) => send<KnowledgeIngestionSnapshot>("/ingestion", "POST", sessionId ? { session_id: sessionId } : {}, signal);
export const retryKnowledgeIngestion = (id: string, signal?: AbortSignal) => send<KnowledgeIngestionSnapshot>(`/ingestion/${encodeURIComponent(id)}/retry`, "POST", undefined, signal);

export function matchesEntity(entity: KnowledgeEntity, query: string, kind: KnowledgeKind | "all" = "all"): boolean {
  return (kind === "all" || entity.kind === kind) && (!query.trim() || [entity.name, entity.category, entity.summary].join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
}
export function adjacentIds(id: string, relations: KnowledgeRelation[]): Set<string> {
  const ids = new Set([id]);
  for (const relation of relations) {
    if (relation.source_id === id) ids.add(relation.target_id);
    if (relation.target_id === id) ids.add(relation.source_id);
  }
  return ids;
}
export function graphRecords(data: KnowledgeSnapshot, query: string, focusId: string | null, neighborsOnly: boolean) {
  const existing = new Set(data.entities.map((entity) => entity.id));
  const validRelations = data.relations.filter((relation) => existing.has(relation.source_id) && existing.has(relation.target_id));
  let ids = new Set(existing);
  if (neighborsOnly && focusId && existing.has(focusId)) ids = adjacentIds(focusId, validRelations);
  if (query.trim()) {
    const matching = data.entities.filter((entity) => matchesEntity(entity, query));
    const expanded = new Set(matching.flatMap((entity) => [...adjacentIds(entity.id, validRelations)]));
    ids = expanded;
  }
  return { entities: data.entities.filter((entity) => ids.has(entity.id)), relations: validRelations.filter((relation) => ids.has(relation.source_id) && ids.has(relation.target_id)) };
}
export interface PositionedKnowledgeEntity extends KnowledgeEntity { x: number; y: number; width: number; height: number }
export interface KnowledgeGraphLayout { nodes: PositionedKnowledgeEntity[]; edges: KnowledgeRelation[]; width: number; height: number; columns: { kind: KnowledgeKind; x: number; count: number }[] }
/** Use only persisted relationships; column placement never creates graph edges. */
export function layoutEntityGraph(entities: KnowledgeEntity[], relations: KnowledgeRelation[], view: KnowledgeView): KnowledgeGraphLayout {
  const order: KnowledgeKind[] = view === "products" ? ["product", "technology", "route", "reference"] : ["route", "technology", "product", "reference"];
  const columns = order.filter((kind) => entities.some((entity) => entity.kind === kind));
  const nodeWidth = 250, nodeHeight = 96, rowGap = 40, columnGap = 145, top = 82;
  const nodes: PositionedKnowledgeEntity[] = [];
  const maxRows = Math.max(1, ...columns.map((kind) => entities.filter((entity) => entity.kind === kind).length));
  const height = Math.max(410, top + maxRows * (nodeHeight + rowGap) + 30);
  columns.forEach((kind, columnIndex) => {
    const group = entities.filter((entity) => entity.kind === kind).sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
    const groupHeight = group.length * nodeHeight + (group.length - 1) * rowGap;
    const start = top + (height - top - groupHeight - 30) / 2;
    group.forEach((entity, index) => nodes.push({ ...entity, x: 36 + columnIndex * (nodeWidth + columnGap), y: start + index * (nodeHeight + rowGap), width: nodeWidth, height: nodeHeight }));
  });
  const ids = new Set(nodes.map((node) => node.id));
  return { nodes, edges: relations.filter((edge) => ids.has(edge.source_id) && ids.has(edge.target_id)), width: Math.max(400, columns.length * nodeWidth + Math.max(0, columns.length - 1) * columnGap + 72), height, columns: columns.map((kind, index) => ({ kind, x: 36 + index * (nodeWidth + columnGap), count: entities.filter((entity) => entity.kind === kind).length })) };
}
export function formatAttributeValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "未记录";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.map(formatAttributeValue).join("；");
  if (typeof value === "object") return Object.entries(value as Record<string, unknown>).filter(([key]) => !["source_ids", "evidence_ids"].includes(key)).map(([key, item]) => `${attributeLabel(key)}：${formatAttributeValue(item)}`).join(" / ");
  return String(value);
}
const ATTRIBUTE_LABELS: Record<string, string> = {
  document_observations: "来源记录", summary: "说明", namespace: "所属组织", aliases: "别名",
  family: "产品家族", category: "类别", overview: "概述", technical_details: "技术属性", label: "字段", text: "内容", maturity: "成熟度", stage: "阶段", basis: "依据", limitations: "信息缺口", mechanism: "实现机制", problem: "解决问题", tradeoffs: "取舍与约束", description: "用途说明", repo_url: "仓库地址", commit: "固定版本", license: "许可证", inputs: "输入", outputs: "输出", steps: "复现步骤", acceptance_criteria: "验收条件", execution_status: "执行状态", status: "状态", uncertainties: "未知项", parameters: "技术参数", interfaces: "公开接口", subsystems: "子系统", architecture: "系统组成", name: "名称", kind: "类别", date: "日期", as_of: "截至日期", source_stage: "原文阶段", assessment_type: "判断类型", conditions: "适用条件", adoption_status: "采用关系", relationship: "关联性质", code_paths: "代码路径", dependencies: "依赖", hardware: "运行条件",
};
export function attributeLabel(key: string): string { return ATTRIBUTE_LABELS[key] || key; }
