import { API_BASE } from "./api";

export interface FieldRecord {
  id: string; entity_id: string; entity_name: string; entity_kind: string; kind: string;
  field_id: string | null; label: string; value: unknown; dimension_id: number; status: string;
  qualifiers: Record<string, unknown>;
  evidence: { quote: string; stance: string; title: string; url: string; revision_id: string; source_context?: Record<string, unknown> }[];
}
export interface FieldSnapshot {
  topic: { id: string; title: string; slug: string };
  dimensions: { id: number; name: string; description: string; records: number }[];
  counts: { entities: number; records: number; fields: number; sources: number };
  entities: { id: string; canonical_name: string; kind_label: string }[];
  records: FieldRecord[]; matching_records: number; limit: number;
}
export interface FieldJob {
  id: string; title: string; status: string; message?: string; engine: string; model: string;
  entities: number; fields: number; unmapped_count: number; error?: string; mode: string;
  chunks_done: number; chunks_total: number;
}
export interface ExtractionRuntime {
  model_enabled: boolean; engine: "cli"; model: "gpt-6-luna"; reasoning_effort: "low"; message: string;
}
export interface FieldJobs { counts: Record<string, number>; jobs: FieldJob[]; runtime: ExtractionRuntime }
export interface SourceDocument {
  document_id: string; name: string; searchable: boolean; parse_status: string; url: string;
  source_revision: string;
}
export interface JobDetail {
  id: string; title: string; status: string; error: string;
  result?: { counts: Record<string, number>; notes: string[]; unmapped: unknown[]; model_calls: number };
}

export async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}/api/research-database${path}`, {
    method: body === undefined ? "GET" : "POST", cache: "no-store",
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(30000)]) : AbortSignal.timeout(30000),
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    let message = `操作未完成（${response.status}）`;
    try { const error = await response.json(); if (typeof error.detail === "string") message = error.detail; } catch { /* HTTP status is retained. */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}
export const readFieldDatabase = (slug: string, query: string, dimension: number, entity: string, signal?: AbortSignal) =>
  request<FieldSnapshot>(`?${new URLSearchParams({ slug, query, dimension: String(dimension), entity_id: entity })}`, undefined, signal);
export const readFieldJobs = (slug: string, signal?: AbortSignal) => request<FieldJobs>(`/jobs?${new URLSearchParams({ slug })}`, undefined, signal);
export const listFieldDocuments = (slug: string) => request<{ library_id: string; documents: SourceDocument[] }>(`/documents?${new URLSearchParams({ slug })}`);
export const importExistingFields = () => request<{ scheduled: number; reused: number }>("/import-existing", {});
export const extractFields = (input: { slug: string; document_ids: string[] }) =>
  request<{ scheduled: number; reused: number }>("/extract", input);
export const importStructuredFields = (input: { slug: string; document_id: string; source_revision: string; outputs: unknown[] }) =>
  request<{ counts: Record<string, number>; unmapped: unknown[]; model_calls: number }>("/map", input);
export const readFieldContract = () => request<Record<string, unknown>>("/contract");
export const retryFieldJob = (id: string) => request(`/jobs/${encodeURIComponent(id)}/retry`, {});
export const readFieldJob = (id: string) => request<JobDetail>(`/jobs/${encodeURIComponent(id)}`);
export const readFieldSource = (id: string) => request<{ title: string; url: string; text_content: string }>(`/sources/${encodeURIComponent(id)}`);

export function fieldValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.map(fieldValue).join("；");
  if (typeof value === "object") {
    const row = value as Record<string, unknown>;
    if (row.raw_value !== undefined) {
      const raw = String(row.raw_value);
      const unit = row.raw_unit ? String(row.raw_unit) : "";
      return unit && !raw.includes(unit) ? `${raw} ${unit}` : raw;
    }
    for (const name of ["conclusion", "description", "level", "label", "component_name", "term_name"]) {
      if (row[name] !== undefined) return String(row[name]) + (row.occurred_at ? ` · ${row.occurred_at}` : "");
    }
    return JSON.stringify(value);
  }
  return ({ supported: "支持", unsupported: "不支持", partial: "部分支持", planned: "计划支持", claimed_unverified: "来源声称" } as Record<string, string>)[String(value)] ?? String(value);
}
