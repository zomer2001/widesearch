import { request, type FieldRecord, type SourceDocument } from "./researchDatabase";

export type AssetKind = "product" | "technology";
export type AssetPane = "fields" | "graph" | "sources";
export type GraphMode = "overview" | "lineage" | "technology" | "applications" | "capabilities";
export type Evidence = FieldRecord["evidence"][number];
export interface AssetNode {
  id: string; name: string; kind: string; kind_label: string; namespace: string; can_open: boolean;
}
export interface Asset extends AssetNode {
  summary: string; field_count: number; source_count: number; updated_at: string | null; relationship_count: number;
}
export interface AssetIndex {
  items: Asset[]; counts: Record<AssetKind, number>; total: number; offset: number; limit: number; kind: AssetKind;
}
export interface AssetField {
  id: string; label: string; value_type: string; repeat_group: string | null; element_id: string; dimension_id: number;
}
export interface AssetRecord extends FieldRecord { element_id: string; linked_entities: AssetNode[] }
export interface AssetDetail {
  asset: AssetNode; records: AssetRecord[]; fields: AssetField[];
  dimensions: { id: number; name: string; description: string; records: number;
    elements: { id: string; name: string; primary_table: string; storage_rule: string }[] }[];
  related: (AssetNode & { relations: string[] })[];
  sources: (Evidence & { quotes: string[] })[];
}
export interface GraphNode extends AssetNode { distance: number | null; layer: number; neighbor_count: number; source_count?: number; record_count?: number }
export interface GraphEdge {
  id: string; source: string; target: string; predicate: string; label: string; source_tables: string[];
  claims: { id: string; detail: string; qualifiers: Record<string, unknown>; evidence: Evidence[] }[];
}
export interface AssetGraph {
  root_id: string; mode: GraphMode; depth: number; truncated: boolean; scope?: "asset" | "topic";
  nodes: GraphNode[]; edges: GraphEdge[]; available_neighbors: number;
}
export interface Material extends SourceDocument {
  field_count: number; revision_count: number; recorded_at: string | null; assets: AssetNode[];
}
export interface MaterialIndex { documents: Material[]; total: number; mapped: number }

export const readAssets = (slug: string, kind: AssetKind, query: string, offset = 0, signal?: AbortSignal) =>
  request<AssetIndex>(`/assets?${new URLSearchParams({ slug, kind, query, offset: String(offset), limit: "18" })}`, undefined, signal);
export const readAssetSummary = (signal?: AbortSignal) =>
  request<Record<string, { product: number; technology: number; sources: number }>>("/asset-summary", undefined, signal);
export const readAsset = (slug: string, id: string, signal?: AbortSignal) =>
  request<AssetDetail>(`/assets/${encodeURIComponent(id)}?${new URLSearchParams({ slug })}`, undefined, signal);
export const readAssetGraph = (slug: string, id: string, mode: GraphMode, depth: number, signal?: AbortSignal, options?: { scope?: "asset" | "topic"; limit?: number }) =>
  request<AssetGraph>(`/assets/${encodeURIComponent(id)}/graph?${new URLSearchParams({ slug, mode, depth: String(depth), limit: String(options?.limit ?? 100), scope: options?.scope ?? "asset" })}`, undefined, signal);
export const readMaterials = (slug: string, signal?: AbortSignal) =>
  request<MaterialIndex>(`/materials?${new URLSearchParams({ slug })}`, undefined, signal);
export const readMaterial = (slug: string, id: string, signal?: AbortSignal) =>
  request<SourceDocument & { text_content: string }>(`/materials/${encodeURIComponent(id)}?${new URLSearchParams({ slug })}`, undefined, signal);

export function assetDate(value: string | null): string {
  if (!value || !Number.isFinite(Date.parse(value))) return "尚无更新记录";
  return new Date(value).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

export function groupAssetRecords(records: AssetRecord[]): AssetRecord[] {
  const canonical = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") return Object.fromEntries(Object.entries(value)
      .filter(([key]) => key !== "claim_id").sort(([a], [b]) => a.localeCompare(b))
      .map(([key, v]) => [key, canonical(v)]));
    return value;
  };
  const groups = new Map<string, AssetRecord>();
  for (const record of records) {
    const conditions = Object.fromEntries(Object.entries(record.qualifiers)
      .filter(([key]) => !["assertion_type", "source_type", "source_nature", "source_quality"].includes(key)));
    const key = JSON.stringify([record.kind, record.field_id, record.label, record.dimension_id,
      record.element_id, canonical(record.value), canonical(conditions)]);
    const evidence = record.evidence.map((e) => ({ ...e, source_context: record.qualifiers }));
    const previous = groups.get(key);
    if (!previous) { groups.set(key, { ...record, evidence, linked_entities: [...record.linked_entities] }); continue; }
    const evidenceKeys = new Set(previous.evidence.map((e) => JSON.stringify([e.revision_id, e.quote, e.stance])));
    previous.evidence.push(...evidence.filter((e) => !evidenceKeys.has(JSON.stringify([e.revision_id, e.quote, e.stance]))));
    const ids = new Set(previous.linked_entities.map((n) => n.id));
    previous.linked_entities.push(...record.linked_entities.filter((n) => !ids.has(n.id)));
  }
  return [...groups.values()];
}

export const NODE_COLORS: Record<string, string> = {
  product: "#236b57", technology: "#356da6", component: "#947335", reference: "#78659a",
  metric: "#98703f", capability: "#39817e", organization: "#69708d", event: "#99715a",
};

