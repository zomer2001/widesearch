import { attributeLabel, formatAttributeValue, type KnowledgeEntity, type KnowledgeEvidence, type KnowledgeSnapshot } from "./knowledgeDatabase";
import type { TopicChange, TopicSnapshot, TopicSource } from "./topics";

export interface FieldDefinition { key: string; label: string; type: string; group?: string }
export interface FieldValue { text: string; evidenceIds: string[]; contexts?: Record<string, unknown>[] }
export interface FieldCell { values: FieldValue[]; evidenceIds?: string[]; status: "missing" | "recorded" | "multiple" | "manual" | "contextual" | "not_public" | "not_applicable" }
const aliases: Record<string, string[]> = {
  manufacturer: ["manufacturer", "vendor", "厂商", "制造商", "生产方"],
  family: ["family", "series", "产品家族", "系列"],
  purpose: ["purpose", "用途", "应用场景", "description"],
  mechanism: ["mechanism", "原理", "技术原理", "实现机制"],
  parameters: ["parameters", "技术参数", "参数"],
  maturity: ["maturity", "成熟度", "stage", "阶段"],
  limitations: ["limitations", "局限", "限制", "信息缺口"],
  problem: ["problem", "解决问题", "解决的问题"],
  inputs: ["inputs", "输入"], outputs: ["outputs", "输出"],
  dependencies: ["dependencies", "依赖", "依赖条件"],
  steps: ["steps", "步骤", "实现步骤", "复现步骤"],
  conditions: ["conditions", "条件", "适用条件"],
  alternatives: ["alternatives", "替代方案"],
  repo_url: ["repo_url", "仓库地址", "仓库"],
  license: ["license", "许可证", "许可"],
  execution_status: ["execution_status", "复现状态", "执行状态"],
};

function record(value: unknown): Record<string, unknown> { return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function meaningful(value: unknown): boolean { return value !== undefined && value !== null && value !== "" && (!Array.isArray(value) || value.length > 0); }
function fieldIds(attributes: Record<string, unknown>, key: string): string[] {
  const value = record(attributes.field_evidence)[key];
  if (Array.isArray(value)) return value.filter((item): item is string => typeof item === "string");
  const ids = record(value).evidence_ids;
  return Array.isArray(ids) ? ids.filter((item): item is string => typeof item === "string") : [];
}

/** Keep independent source values visible; never silently choose a newer claim. */
export function entityField(entity: KnowledgeEntity, field: FieldDefinition): FieldCell {
  const enriched = entity as KnowledgeEntity & { field_overrides?: Record<string, { value: unknown }>; field_evidence?: Record<string, string[]> };
  const override = enriched.field_overrides?.[field.key];
  if (override && meaningful(override.value)) return { values: [{ text: formatAttributeValue(override.value), evidenceIds: [] }], status: "manual" };
  const keys = [...new Set([field.key, field.label, ...(aliases[field.key] ?? [])])];
  const observations = Array.isArray(entity.attributes.document_observations) ? entity.attributes.document_observations : [];
  const sources: Record<string, unknown>[] = [{ ...entity.attributes, field_evidence: enriched.field_evidence ?? entity.attributes.field_evidence }, ...observations.map((item) => {
    const observation = record(item);
    return { ...record(observation.attributes), field_evidence: observation.field_evidence ?? record(observation.attributes).field_evidence };
  })];
  const values = new Map<string, Set<string>>();
  for (const source of sources) for (const key of keys) {
    if (!meaningful(source[key])) continue;
    let text = formatAttributeValue(source[key]);
    if (key === "execution_status") text = ({ not_run: "未运行", passed: "已通过", failed: "未通过" } as Record<string, string>)[text] ?? text;
    const ids = values.get(text) ?? new Set<string>();
    fieldIds(source, key).forEach((id) => ids.add(id));
    values.set(text, ids);
  }
  const items = [...values].map(([text, ids]) => ({ text, evidenceIds: [...ids] }));
  return { values: items, status: !items.length ? "missing" : items.length > 1 ? "multiple" : entity.origin === "manual" ? "manual" : "recorded" };
}

export function additionalFields(entity: KnowledgeEntity, templates: FieldDefinition[]): FieldDefinition[] {
  const excluded = new Set(["field_evidence", "document_observations", "namespace", "source_ids", "evidence_ids", "name", "summary", "category", "id", "kind"]);
  templates.forEach((field) => [field.key, field.label, ...(aliases[field.key] ?? [])].forEach((key) => excluded.add(key)));
  const observations = Array.isArray(entity.attributes.document_observations) ? entity.attributes.document_observations : [];
  const keys = [...new Set([Object.keys(entity.attributes), ...observations.map((item) => Object.keys(record(record(item).attributes)))].flat())];
  return keys.filter((key) => !excluded.has(key)).map((key) => ({ key, label: attributeLabel(key), type: "text" }));
}

export function fieldEvidence(cell: FieldCell, evidence: KnowledgeEvidence[]): KnowledgeEvidence[] {
  const ids = new Set([...(cell.evidenceIds ?? []), ...cell.values.flatMap((value) => value.evidenceIds)]);
  return evidence.filter((item) => ids.has(item.id));
}

export function scopedGraph(data: KnowledgeSnapshot, mode: "network" | "lineage", focusId: string | null, query: string) {
  const available = new Set(data.entities.map((item) => item.id));
  const edges = data.relations.filter((item) => available.has(item.source_id) && available.has(item.target_id) && (mode === "network" || ["variant_of", "part_of", "replaces", "evolves_from"].includes(item.predicate)));
  let ids = mode === "lineage" ? new Set(data.entities.filter((item) => item.kind === "product").map((item) => item.id)) : available;
  const matches = query.trim() ? data.entities.filter((item) => [item.name, item.summary, item.category].join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())).map((item) => item.id) : focusId && ids.has(focusId) ? [focusId] : [];
  if (query.trim() || matches.length) {
    const centers = new Set(matches);
    ids = new Set(matches.filter((id) => ids.has(id)));
    edges.forEach((edge) => { if (centers.has(edge.source_id) || centers.has(edge.target_id)) { ids.add(edge.source_id); ids.add(edge.target_id); } });
  }
  return { entities: data.entities.filter((item) => ids.has(item.id)), relations: edges.filter((edge) => ids.has(edge.source_id) && ids.has(edge.target_id)) };
}

export function defaultTopicGraphFocus(data: KnowledgeSnapshot, perspective: "product" | "technology"): string | null {
  const degrees = new Map<string, number>();
  data.relations.forEach((edge) => [edge.source_id, edge.target_id].forEach((id) => degrees.set(id, (degrees.get(id) ?? 0) + 1)));
  const roots = data.entities.filter((item) => item.kind === perspective && degrees.has(item.id));
  return roots.sort((a, b) => (degrees.get(b.id) ?? 0) - (degrees.get(a.id) ?? 0) || a.name.localeCompare(b.name))[0]?.id ?? null;
}

/** Perspectives share facts. Compact mode changes only the visible subgraph. */
export function topicGraphRecords(data: KnowledgeSnapshot, options: { perspective: "product" | "technology"; density: "compact" | "full"; mode: "network" | "lineage"; focusId: string | null; query: string; limit?: number }) {
  let base = scopedGraph(data, options.mode, options.focusId, options.query);
  if (!options.focusId && !options.query.trim() && options.mode === "network") {
    const relevant = new Set(base.entities.filter((item) => item.kind === options.perspective).map((item) => item.id));
    let previousSize = -1;
    while (previousSize !== relevant.size) {
      previousSize = relevant.size;
      const previous = new Set(relevant);
      base.relations.forEach((edge) => { if (previous.has(edge.source_id) || previous.has(edge.target_id)) { relevant.add(edge.source_id); relevant.add(edge.target_id); } });
    }
    base = { entities: base.entities.filter((item) => relevant.has(item.id)), relations: base.relations.filter((edge) => relevant.has(edge.source_id) && relevant.has(edge.target_id)) };
  }
  const total = base.entities.length;
  if (options.density === "full") return { ...base, total, hidden: 0 };
  const edges = base.relations.filter((edge) => edge.status === "source_backed" && edge.evidence_ids.length > 0);
  const degrees = new Map<string, number>();
  edges.forEach((edge) => [edge.source_id, edge.target_id].forEach((id) => degrees.set(id, (degrees.get(id) ?? 0) + 1)));
  const sorted = [...base.entities].sort((a, b) => Number(b.id === options.focusId) - Number(a.id === options.focusId) || (degrees.get(b.id) ?? 0) - (degrees.get(a.id) ?? 0) || Number(b.kind === options.perspective) - Number(a.kind === options.perspective) || a.name.localeCompare(b.name));
  const limit = Math.max(2, Math.min(18, options.limit ?? 16));
  const chosen = sorted.slice(0, limit);
  const ids = new Set(chosen.map((item) => item.id));
  return { entities: chosen, relations: edges.filter((edge) => ids.has(edge.source_id) && ids.has(edge.target_id)), total, hidden: total - chosen.length };
}

export function changeText(change: TopicChange): string {
  const pieces = [
    change.entities_added > 0 ? `新增 ${change.entities_added} 个实体` : "",
    change.entities_updated > 0 ? `更新 ${change.entities_updated} 个实体` : "",
    change.entities_removed > 0 ? `移除 ${change.entities_removed} 个实体` : "",
    change.relations_added > 0 ? `新增 ${change.relations_added} 条关系` : "",
    change.relations_removed > 0 ? `移除 ${change.relations_removed} 条关系` : "",
    change.sources_added > 0 ? `收录 ${change.sources_added} 份资料` : "",
  ].filter(Boolean);
  return pieces.join("，") || change.summary || "本轮未记录知识变化";
}

/** A deterministic view of saved changes, not an additional model-generated report. */
export function topicUpdateText(snapshot: TopicSnapshot): string {
  const lines = [`${snapshot.topic.title} · 知识库更新记录`, "", `当前实体 ${snapshot.knowledge.entities.length} 个，关系 ${snapshot.knowledge.relations.length} 条，资料 ${snapshot.sources.length} 份。`, ""];
  if (!snapshot.changes.length) lines.push("尚无已记录的增量更新。");
  for (const change of snapshot.changes) lines.push(`${change.created_at}  ${changeText(change)}`);
  if (snapshot.gaps.length) {
    lines.push("", "待补字段：");
    snapshot.gaps.forEach((gap) => lines.push(`- ${gap.entity_name}：${gap.label}`));
  }
  lines.push("", "资料来源：");
  snapshot.sources.forEach((source) => lines.push(`- ${source.title}${source.url ? ` — ${source.url}` : ""}${source.available ? "" : "（本地原文不可用）"}`));
  return lines.join("\n") + "\n";
}


export interface ProjectedField extends FieldDefinition {
  value: unknown; status: string; evidence_ids: string[]; unit?: string; origin?: string; manual?: boolean; source_keys?: string[];
  observations?: { value: unknown; evidence_ids: string[]; manual: boolean; status: string; active?: boolean; context?: Record<string, unknown> }[];
  values?: { value: unknown; evidence_ids: string[]; context?: Record<string, unknown> }[];
}

export const fieldStatusLabels: Record<FieldCell["status"], string> = {
  missing: "未采集", recorded: "已记录", multiple: "来源存在差异", manual: "手工维护", contextual: "不同版本或条件",
  not_public: "未公开", not_applicable: "不适用",
};

/** Projection states are source-backed classifications, never guesses from absent values. */
export function projectedFieldCell(entity: KnowledgeEntity, field: FieldDefinition, projection?: ProjectedField[]): FieldCell {
  const found = projection?.find((item) => item.key === field.key);
  if (!found) return entityField(entity, field);
  if (found.status === "not_public" || found.status === "not_applicable") return { values: [], status: found.status, evidenceIds: found.evidence_ids ?? [] };
  const rawValues = found.values?.length ? found.values : found.observations?.length ? found.observations.filter((item) => item.active !== false && (item.status === "known" || item.status === "conflicting")) : meaningful(found.value) ? [{ value: found.value, evidence_ids: found.evidence_ids }] : [];
  const combined = new Map<string, { ids: Set<string>; contexts: Map<string, Record<string, unknown>> }>();
  for (const item of rawValues) if (meaningful(item.value)) {
    const text = formatAttributeValue(item.value), entry = combined.get(text) ?? { ids: new Set<string>(), contexts: new Map<string, Record<string, unknown>>() };
    (item.evidence_ids ?? []).forEach((id) => entry.ids.add(id));
    const context = "context" in item ? item.context : undefined;
    if (context && Object.keys(context).length > 0) entry.contexts.set(JSON.stringify(context), context);
    combined.set(text, entry);
  }
  const values = [...combined].map(([text, entry]) => ({ text, evidenceIds: [...entry.ids], ...(entry.contexts.size ? { contexts: [...entry.contexts.values()] } : {}) }));
  const manual = found.manual || found.origin === "manual" || !!entity.field_overrides?.[field.key];
  return { values, status: !values.length ? "missing" : manual ? "manual" : found.status === "conflicting" ? "multiple" : values.length > 1 ? "contextual" : "recorded" };

}

/** Branches are views over one entity store. They never create graph edges. */
export function branchKnowledge(data: KnowledgeSnapshot, ids?: string[]): KnowledgeSnapshot {
  if (!ids) return data;
  const selected = new Set(ids);
  return { ...data, entities: data.entities.filter((item) => selected.has(item.id)), relations: data.relations.filter((edge) => selected.has(edge.source_id) && selected.has(edge.target_id)) };
}

export function graphDegrees(data: Pick<KnowledgeSnapshot, "entities" | "relations">): Map<string, number> {
  const available = new Set(data.entities.map((item) => item.id));
  const neighbors = new Map<string, Set<string>>();
  for (const edge of data.relations) if (available.has(edge.source_id) && available.has(edge.target_id) && edge.source_id !== edge.target_id) {
    for (const [id, other] of [[edge.source_id, edge.target_id], [edge.target_id, edge.source_id]]) {
      const adjacent = neighbors.get(id) ?? new Set<string>(); adjacent.add(other); neighbors.set(id, adjacent);
    }
  }
  return new Map(data.entities.map((entity) => [entity.id, neighbors.get(entity.id)?.size ?? 0]));
}

export function expandGraph(data: KnowledgeSnapshot, base: ReturnType<typeof scopedGraph>, expanded: string[], hidden: string[]) {
  const ids = new Set(base.entities.map((item) => item.id)), centers = new Set(expanded), excluded = new Set(hidden);
  const available = new Set(data.entities.map((item) => item.id));
  const edges = data.relations.filter((edge) => available.has(edge.source_id) && available.has(edge.target_id));
  for (const edge of edges) if (centers.has(edge.source_id) || centers.has(edge.target_id)) { ids.add(edge.source_id); ids.add(edge.target_id); }
  return { entities: data.entities.filter((item) => ids.has(item.id) && !excluded.has(item.id)), relations: edges.filter((edge) => ids.has(edge.source_id) && ids.has(edge.target_id) && !excluded.has(edge.source_id) && !excluded.has(edge.target_id)) };
}

export function graphLeafIds(data: Pick<KnowledgeSnapshot, "entities" | "relations">, center: string): string[] {
  const degrees = graphDegrees(data), adjacent = new Set<string>();
  data.relations.forEach((edge) => { if (edge.source_id === center) adjacent.add(edge.target_id); if (edge.target_id === center) adjacent.add(edge.source_id); });
  return [...adjacent].filter((id) => id !== center && degrees.get(id) === 1);
}


export function observationContextText(context: Record<string, unknown>): string {
  const labels: Record<string, string> = { version: "版本", model: "型号", variant: "型号", date: "日期", effective_at: "适用时间", operating_conditions: "工作条件", valid_from: "生效时间", valid_to: "截止时间", observed_at: "观察时间", event_date: "事件日期", published_at: "发布时间", source_date: "来源日期", test_conditions: "测试条件", conditions: "条件", unit: "单位", scope: "范围", deployment: "部署环境" };
  return Object.entries(context).filter(([, value]) => meaningful(value)).map(([key, value]) => `${labels[key] ?? key}：${formatAttributeValue(value)}`).join(" · ");
}


/** Citation locators differ from full-document refs; match the archived document, not its excerpt. */
export function sourcesForBranch(sources: TopicSource[], data: KnowledgeSnapshot): TopicSource[] {
  const evidenceIds = new Set([...data.entities.flatMap((item) => item.evidence_ids), ...data.relations.flatMap((item) => item.evidence_ids)]);
  const evidence = data.evidence.filter((item) => evidenceIds.has(item.id));
  const documentKey = (item: { library_id?: string; document_id?: string; ref_id?: string }): string | null => {
    if (item.library_id && item.document_id) return `${item.library_id}/${item.document_id}`;
    if (!item.ref_id?.startsWith("local://")) return null;
    try { const ref = new URL(item.ref_id); if (ref.searchParams.has("rev") && ref.hostname && ref.pathname.length > 1) return `${decodeURIComponent(ref.hostname)}/${decodeURIComponent(ref.pathname.slice(1))}`; } catch { /* Legacy refs can fall back to exact identity. */ }
    return null;
  };
  const documents = new Set(evidence.map(documentKey).filter((value): value is string => !!value));
  const refs = new Set(evidence.map((item) => item.ref_id).filter((value): value is string => !!value));
  const ids = new Set(evidence.map((item) => item.id));
  return sources.filter((source) => { const key = documentKey(source); return !!key && documents.has(key) || !!source.ref_id && refs.has(source.ref_id) || !key && ids.has(source.id); });
}

/** Restore only overrides owned by this entity; inherited/manual-origin values are not removable overlays. */
export function restorableFieldKeys(entity: KnowledgeEntity, field: FieldDefinition, projection?: ProjectedField[]): string[] {
  const projected = projection?.find((item) => item.key === field.key);
  const keys = new Set([field.key, ...(projected?.source_keys ?? [])]);
  return [...keys].filter((key) => Object.prototype.hasOwnProperty.call(entity.field_overrides ?? {}, key));
}
