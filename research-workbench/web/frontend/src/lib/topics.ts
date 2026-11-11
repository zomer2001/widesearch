import { API_BASE } from "./api";
import type { EngineId } from "./workbench";
import type { KnowledgeKind, KnowledgeSnapshot } from "./knowledgeDatabase";

export interface Topic {
  id: string; title: string; description: string; namespace: string; aliases: string[];
  created_at: string; updated_at: string;
  counts: { entities: number; sources: number; runs: number };
  profile?: TopicProfile; clusters?: TopicCluster[]; coverage?: TopicCoverage; quality?: TopicQuality;
  recent_run?: Pick<TopicRun, "id" | "query" | "status" | "created_at" | "updated_at" | "message"> | null;
}
export interface TopicSource {
  id: string; document_id: string; library_id: string; title: string; url: string;
  ref_id: string; source_revision: string; available: boolean; session_ids: string[];
}
export interface TopicChange {
  id: string; topic_id: string; run_id: string; created_at: string;
  entities_added: number; entities_updated: number; entities_removed: number;
  relations_added: number; relations_removed: number; sources_added: number; summary: string;
}
export interface TopicRun {
  modules?: string[];
  id: string; topic_id: string; query: string; engine: string; model: string; status: string;
  message?: string; created_at: string; updated_at: string; max_tasks: number; max_seconds: number;
  local_only?: boolean; stop_requested?: boolean; local_result?: string; error?: string;
  plan?: { id: string; title: string; missing?: unknown }[]; remaining?: string[];
}
export interface TopicTask {
  adaptive_collection?: { batches: number; new: number; corroboration: number; groups: number; last_error: string };
  id: string; run_id: string; topic_id: string; status: string; title: string; query?: string;
  message?: string; session_id?: string; created_at: string; updated_at: string; kind?: string; facet?: string;
}
export interface TopicProfile { id: "anduril" | "palantir" | "huawei" | "generic"; label: string; schema_version: number }
export interface TopicCluster { id: string; label: string; parent_id: string | null; entity_ids: string[]; count: number; origin: "classification_rule"; keywords?: string[] }
export type TopicFieldStatus = "unknown" | "known" | "not_public" | "not_applicable" | "conflicting";
export interface TopicFieldObservation { value: unknown; source_key: string; evidence_ids: string[]; manual: boolean; status: TopicFieldStatus; active?: boolean; context?: Record<string, unknown> }
export interface TopicField { key: string; label: string; type: string; group?: string; aliases?: string[]; unit?: string }
export interface TopicEntityField extends TopicField { group: string; value: unknown; status: TopicFieldStatus; evidence_ids: string[]; source_keys: string[]; manual: boolean; observations: TopicFieldObservation[] }
export interface TopicCoverage { filled: number; total: number; missing: number }
export interface TopicQuality { known: number; unknown: number; not_public: number; not_applicable: number; conflicting: number; total: number; with_field_evidence: number; entities_without_field_evidence: number }
export interface TopicMerge { canonical_id: string; alias_id: string; canonical_name: string; alias_name: string; created_at: string; reason: string }
export interface TopicDuplicateCandidate { canonical_id: string; alias_id: string; canonical_name: string; alias_name: string; kind: KnowledgeKind; reason: string; confidence: "high" | "review"; shared_neighbors: number }
export interface TopicGap { entity_id: string; entity_name: string; kind: KnowledgeKind; field: string; label: string }
export interface TopicSnapshot {
  topic: Topic; knowledge: KnowledgeSnapshot; sources: TopicSource[]; runs: TopicRun[];
  tasks: TopicTask[]; changes: TopicChange[]; templates: Partial<Record<KnowledgeKind, TopicField[]>>;
  coverage: TopicCoverage; gaps: TopicGap[];
  profile?: TopicProfile; clusters?: TopicCluster[]; entity_clusters?: Record<string, string[]>;
  entity_fields?: Record<string, TopicEntityField[]>; quality?: TopicQuality;
  merges?: TopicMerge[]; duplicate_candidates?: TopicDuplicateCandidate[];
  classification_overrides?: Record<string, string[]>; entity_classification_origin?: Record<string, "manual" | "classification_rule">;
}
export interface TopicList { topics: Topic[]; default_topic_id: string | null }
export interface ResearchInput { query: string; topic_id?: string; engine?: EngineId; max_tasks: number; max_seconds: number; local_only?: boolean; modules?: "adaptive_collection"[] }

export function retainedTopicSelection(current: string | null, topics: Topic[]): string | null {
  return current && topics.some((topic) => topic.id === current) ? current : null;
}

/** Detail payloads may omit list counts; derive navigation counts from the scoped snapshot. */
export function topicFromSnapshot(snapshot: TopicSnapshot, previous: Topic): Topic {
  return {
    ...previous,
    ...snapshot.topic,
    counts: {
      entities: snapshot.knowledge.counts.entities,
      sources: snapshot.sources.length,
      runs: snapshot.runs.length,
    },
    ...(snapshot.profile ? { profile: snapshot.profile } : {}),
    ...(snapshot.clusters ? { clusters: snapshot.clusters } : {}),
    ...(snapshot.coverage ? { coverage: snapshot.coverage } : {}),
    ...(snapshot.quality ? { quality: snapshot.quality } : {}),
    recent_run: snapshot.runs[0] ?? null,
  };
}

async function send<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const timeout = AbortSignal.timeout(30000);
  const response = await fetch(`${API_BASE}/api/topics${path}`, {
    method, cache: "no-store", signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    let message = `操作未完成（${response.status}）`;
    try { const error = await response.json(); if (typeof error.detail === "string") message = error.detail; } catch { /* Keep the HTTP error. */ }
    throw new Error(message);
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}
export const listTopics = (signal?: AbortSignal) => send<TopicList>("", "GET", undefined, signal);
export const getTopic = (id: string, signal?: AbortSignal) => send<TopicSnapshot>(`/${encodeURIComponent(id)}`, "GET", undefined, signal);
export const createTopic = (title: string, signal?: AbortSignal) => send<Topic>("", "POST", { title }, signal);
export const researchTopic = (input: ResearchInput, signal?: AbortSignal) => send<{ topic_id: string; run_id: string }>("/research", "POST", input, signal);
export const stopTopicRun = (id: string, signal?: AbortSignal) => send<unknown>(`/runs/${encodeURIComponent(id)}/stop`, "POST", {}, signal);
export const detachTopicEntity = (topicId: string, entityId: string, signal?: AbortSignal) => send<void>(`/${encodeURIComponent(topicId)}/entities/${encodeURIComponent(entityId)}`, "DELETE", undefined, signal);
export const mergeTopicEntities = (topicId: string, canonicalId: string, aliasId: string, signal?: AbortSignal) => send<TopicSnapshot>(`/${encodeURIComponent(topicId)}/merges`, "POST", { canonical_id: canonicalId, alias_id: aliasId }, signal);
export const setTopicClassification = (topicId: string, entityId: string, clusterIds: string[] | null, signal?: AbortSignal) => send<TopicSnapshot>(`/${encodeURIComponent(topicId)}/entities/${encodeURIComponent(entityId)}/classification`, "PATCH", { cluster_ids: clusterIds }, signal);
export const undoTopicMerge = (topicId: string, aliasId: string, signal?: AbortSignal) => send<TopicSnapshot>(`/${encodeURIComponent(topicId)}/merges/${encodeURIComponent(aliasId)}`, "DELETE", undefined, signal);

export function activeTopicRun(run: Pick<TopicRun, "status">): boolean {
  return ["queued", "planning", "running", "researching", "ingesting", "stopping"].includes(run.status);
}
export function topicStatus(status: string): string {
  return ({ queued: "等待执行", planning: "规划中", running: "执行中", researching: "采集中", ingesting: "入库中", stopping: "正在停止", completed: "已完成", partial: "部分完成", failed: "失败", stopped: "已停止", cancelled: "已取消", skipped: "已跳过", interrupted: "已中断", pending: "待执行" } as Record<string, string>)[status] ?? "处理中";
}
export function topicDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
/** Serial observation. A view never launches research, including after errors. */
export function watchTopic(options: {
  id: string; read?: typeof getTopic; onData: (value: TopicSnapshot) => void; onError: (error: string | null) => void;
  schedule?: (callback: () => void, ms: number) => ReturnType<typeof setTimeout>;
  unschedule?: (id: ReturnType<typeof setTimeout>) => void;
}) {
  const schedule = options.schedule ?? setTimeout, unschedule = options.unschedule ?? clearTimeout;
  const read = options.read ?? getTopic;
  let timer: ReturnType<typeof setTimeout> | undefined, controller: AbortController | undefined;
  let generation = 0, stopped = false, failures = 0;
  const poll = async () => {
    const current = ++generation;
    controller?.abort(); controller = new AbortController();
    if (timer !== undefined) { unschedule(timer); timer = undefined; }
    let delay = 5000;
    try {
      const value = await read(options.id, controller.signal);
      if (stopped || current !== generation) return;
      failures = 0; options.onData(value); options.onError(null);
      delay = value.runs.some(activeTopicRun) ? 5000 : 15000;
    } catch (error) {
      if (stopped || current !== generation) return;
      failures++;
      options.onError(`${error instanceof Error ? error.message : "读取专题失败"}${failures >= 3 ? "。自动刷新已暂停，请点击刷新重试。" : ""}`);
    }
    if (!stopped && current === generation && failures < 3) timer = schedule(() => { void poll(); }, delay);
  };
  void poll();
  return { refresh: () => { failures = 0; void poll(); }, stop: () => { stopped = true; generation++; controller?.abort(); if (timer !== undefined) unschedule(timer); } };
}
