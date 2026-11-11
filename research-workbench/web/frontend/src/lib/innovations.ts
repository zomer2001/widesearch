import type { Turn } from "./conversation";
import type { ResearchModuleConfig, ResearchModuleId, SearchState } from "./types";

export const RESEARCH_MODULES: { id: ResearchModuleId; label: string; description: string }[] = [
  { id: "evidence_revision", label: "证据适用性与修订", description: "记录证据的适用范围、时间与修订影响。" },
  { id: "context_memory", label: "目标与采集经验", description: "保留研究目标，记录采集经验与长程上下文整理。" },
  { id: "adaptive_collection", label: "自适应采集", description: "根据目标缺口、资料关联和新增信息调整采集顺序。" },
];
export const moduleConfigForTurn = (turn?: Turn | null): ResearchModuleConfig | undefined => turn?.searchState?.module_config ?? turn?.moduleConfig;
export const createModuleConfig = (enabled: ResearchModuleId[] = []): ResearchModuleConfig => ({
  schema_version: 1, enabled: RESEARCH_MODULES.map((module) => module.id).filter((id) => enabled.includes(id)), implementation_version: "1",
});

/** An unrecorded legacy continuation leaves selection to the saved backend session. */
export function modulesForSubmission(previous: Turn | undefined, selected: ResearchModuleId[], continuing: boolean): ResearchModuleId[] | undefined {
  if (continuing) return moduleConfigForTurn(previous)?.enabled;
  return createModuleConfig(selected).enabled;
}

export function innovationActivity(state: SearchState | null, config?: ResearchModuleConfig) {
  const actual = state?.module_config ?? config;
  const annotations = Object.values(state?.evidence_graph?.claim_annotations ?? {});
  const comparisons = Object.values(state?.coverage_map?.evidence_comparisons ?? {}).flatMap((items) => Object.values(items)).filter((item) => item.method === "p1_rule");
  const revisions = Object.values(state?.p1?.events ?? {}).filter((item) => item.event.confirmed);
  const experiences = Object.values(state?.p2?.experiences ?? {});
  const metrics = state?.p2?.compaction_metrics ?? [];
  const goal = state?.p2?.goal;
  const published = metrics.filter((item) => item.outcome === "published");
  const compressed = published.filter((item) => (item.summary_model_calls ?? 0) > 0);
  const rebuilt = published.filter((item) => item.rebuilt);
  const rejected = metrics.filter((item) => item.outcome === "rejected");
  const activity = {
    adaptive_collection: (state?.collection_policy?.decisions.length ?? 0) > 0 || Object.keys(state?.collection_policy?.batches ?? {}).length > 0,
    evidence_revision: annotations.length + comparisons.length + revisions.length > 0,
    context_memory: !!goal?.original_goal || (goal?.user_messages.length ?? 0) > 0 || experiences.length > 0 || metrics.length > 0,
  };
  const status = (id: ResearchModuleId): "unknown" | "disabled" | "enabled" | "triggered" => !actual ? "unknown"
    : !actual.enabled.includes(id) ? "disabled" : activity[id] ? "triggered" : "enabled";
  return { config: actual, status, annotations, comparisons, revisions, experiences, metrics, goal, compressed, rebuilt, rejected };
}

export function innovationSourceHref(source: string): string | undefined {
  return /^(https?:\/\/|local:\/\/)/i.test(source) ? source : undefined;
}
