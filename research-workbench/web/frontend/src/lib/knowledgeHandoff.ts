import { request } from "./researchDatabase";

export interface HandoffResult {
  schema_version: "knowledge-handoff-v2";
  document_id: string;
  source_revision: string;
  outputs: Record<string, unknown>[];
  summary?: { text: string; kind: "extractive" | "abstractive"; quotes: string[] } | null;
  status: "completed" | "partial" | "no_relevant_content";
  notes?: string[];
}

export interface HandoffPlan {
  plan_id: string;
  document_id?: string;
  title?: string;
  status: string;
  created_at?: string;
  source_revision?: string;
  counts: Record<string, number>;
  records?: Record<string, unknown>[];
  entities?: Record<string, unknown>[];
  unmapped?: unknown[];
  conflicts?: unknown[];
  issues?: unknown[];
  result?: HandoffResult;
  outputs?: Record<string, unknown>[];
  entity_choices?: Record<string, string>;
  can_commit?: boolean;
  requires_partial_confirmation?: boolean;
  validation_note?: string;
  sql_statements?: { sql: string; params: unknown[] }[];
}

export interface HandoffSummary {
  id: string;
  text: string;
  kind: "extractive" | "abstractive";
  source_revision: string;
  current: boolean;
  created_at?: string;
  quotes?: string[];
  evidence?: { quote: string }[];
}

export interface HandoffDocument {
  document_id: string;
  title: string;
  url: string;
  source_revision: string;
  text: string;
  summaries: HandoffSummary[];
}

export interface HandoffStatus {
  plans: HandoffPlan[];
  batches: { id?: string; batch_id?: string; title?: string; status?: string; counts?: Record<string, number> }[];
  summaries?: HandoffSummary[];
}

export const readHandoffStatus = (slug: string, signal?: AbortSignal) =>
  request<HandoffStatus>(`/handoff/status?${new URLSearchParams({ slug })}`, undefined, signal);
export const readHandoffPlan = (slug: string, id: string) =>
  request<HandoffPlan>(`/handoff/plans/${encodeURIComponent(id)}?${new URLSearchParams({ slug })}`);
export const previewHandoff = (slug: string, result: HandoffResult, entityChoices: Record<string, string> = {}) =>
  request<HandoffPlan>("/handoff/preview", { slug, result, entity_choices: entityChoices });
export const commitHandoff = (slug: string, planId: string, allowPartial: boolean) =>
  request<{ status: string; counts?: Record<string, number>; message?: string; summary_error?: string }>("/handoff/commit", { slug, plan_id: planId, allow_partial: allowPartial });
export const saveHandoffSummary = (slug: string, result: HandoffResult) =>
  request<{ id?: string; message?: string }>("/handoff/summaries", { slug, result });
export const readHandoffDocument = (slug: string, documentId: string, signal?: AbortSignal) =>
  request<HandoffDocument>(`/handoff/documents/${encodeURIComponent(documentId)}?${new URLSearchParams({ slug })}`, undefined, signal);

export function handoffStatusLabel(status: string): string {
  return ({ ready: "待确认", prepared: "待确认", preview: "待确认", candidate: "候选", duplicate: "已有记录", partial: "部分可入库", blocked: "需要处理", needs_review: "待核对", committed: "已入库", completed: "已完成", stale: "需要重新预览", invalid: "未通过校验", failed: "处理失败" } as Record<string, string>)[status] ?? status;
}
