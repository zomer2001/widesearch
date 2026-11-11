export type ResearchModuleId = "evidence_revision" | "context_memory" | "adaptive_collection";

export interface AdaptiveCollectionState {
  goals: Record<string, { text: string; gap: number | null; basis: string; terms: string[] }>;
  batches: Record<string, { task_id: string; source: string; new: number; corroboration: number; repeated: number; evidence_ids: string[] }>;
  communities: { id: number; terms: string[]; size: number; read: number }[];
  decisions: { id: string; kind: "task" | "page"; score: number; relevance?: number; redundancy?: number; cluster?: number }[];
  last_error: string;
}
export interface ResearchModuleConfig {
  schema_version: 1;
  enabled: ResearchModuleId[];
  implementation_version: "1";
}
export interface EvidenceClaimAnnotation {
  evidence_id: string;
  evidence_fingerprint: string;
  entity: string;
  attribute: string;
  value: string;
  scope_key: string;
  valid_time?: { start?: string | null; end?: string | null; basis_quote?: string } | null;
  verified: boolean;
  reason: string;
}
export interface EvidenceComparison {
  left_id: string;
  right_id: string;
  verdict: "compatible" | "contradictory" | "uncertain";
  reason: string;
  left_quote?: string;
  right_quote?: string;
  scope?: string;
  verified: boolean;
  method: "llm" | "p1_rule";
  rule?: string;
  temporal_relation?: string;
}
export interface RevisionArtifact {
  id: string;
  kind: "task" | "section" | "report";
  scope_key: string;
  epoch: number;
  version: number;
  status: "active" | "stale";
  content: string;
  read_evidence: Record<string, string>;
  read_artifacts: Record<string, number>;
  support_sets: string[][];
  producer_task_id: string;
}
export interface AppliedEvidenceRevision {
  payload_fingerprint: string;
  event: {
    id: string; old_evidence_id: string; new_evidence_id: string;
    scope_key: string; reason: string; confirmed: boolean;
    expected_old_version: string; expected_new_version: string;
  };
  impact: {
    revision_id: string; affected_artifact_ids: string[]; affected_task_ids: string[];
    surviving_support_sets: Record<string, string[][]>; unchanged_ids: string[];
    invalidated_execution_ids: string[]; already_applied: boolean;
  };
}
export interface EvidenceRevisionMemory {
  enabled: boolean;
  artifacts: Record<string, RevisionArtifact>;
  artifact_history: Record<string, RevisionArtifact[]>;
  executions: Record<string, {
    id: string; task_id: string; assigned_agent_id: string; scope_key: string; task_epoch: number;
    source_scope_fingerprint: string; read_evidence: Record<string, string>; read_artifacts: Record<string, number>;
    output_epochs: Record<string, number>; output_versions: Record<string, number>; invalidated: boolean; closed: boolean;
  }>;
  retired_uses: Record<string, string[]>;
  events: Record<string, AppliedEvidenceRevision>;
}
export interface CollectionExperience {
  id: string; execution_id: string; payload_fingerprint: string; task_id: string; agent_id: string;
  question: string; scope_key: string; source_scope_fingerprint: string;
  queries: string[]; routes: Record<string, unknown>[]; sources: string[]; target_cells: string[];
  evidence_versions: Record<string, string>; artifact_versions: Record<string, number>;
  feedback: {
    grounded_evidence_ids: string[]; new_evidence_ids: string[] | null; credited_evidence_ids: string[];
    newly_filled_cells: string[] | null; unique_supported_cells: string[]; repeated_evidence_count: number;
    duration_s: number | null; query_count: number | null; token_count: number | null; interpretation: string;
  };
  status: "active" | "insufficient_feedback" | "revalidation_required";
  invalidation_reasons: string[];
}
export interface ContextCompactionMetric {
  task_id?: string; mode?: string; summary_model_calls?: number;
  summary_input_tokens?: number; summary_output_tokens?: number;
  before_tokens?: number; after_tokens?: number; rebuilt?: boolean;
  outcome?: "published" | "rejected"; error_type?: string; elapsed_ms?: number;
}
export interface GoalContextMemory {
  enabled: boolean;
  goal: { version: number; original_goal: string; user_messages: string[]; hard_constraints: string[] };
  experiences: Record<string, CollectionExperience>;
  credited_evidence: Record<string, string[]>;
  compaction_metrics: ContextCompactionMetric[];
}
