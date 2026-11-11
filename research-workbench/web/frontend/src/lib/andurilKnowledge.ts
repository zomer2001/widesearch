/** Reader-facing knowledge records. Collection task counts are deliberately excluded. */
export interface AndurilSource {
  id: string;
  title: string;
  url: string;
  library_id?: string;
  document_id?: string;
  ref_id?: string;
  quote?: string;
  location?: string;
}

export interface AndurilProduct {
  id: string;
  name: string;
  family: string;
  category: string;
  overview: string;
  technical_details: { label: string; text: string; source_ids: string[] }[];
  technology_ids: string[];
  maturity: { stage: string; basis: string; source_ids: string[] };
  limitations: string[];
  source_ids: string[];
}

export interface AndurilTechnology {
  id: string;
  name: string;
  mechanism: string;
  product_ids: string[];
  route_ids: string[];
  source_ids: string[];
  limitations: string[];
}

export interface AndurilRoute {
  id: string;
  name: string;
  problem: string;
  mechanism: string;
  tradeoffs: string[];
  product_ids: string[];
  source_ids: string[];
  limitations: string[];
}

export interface AndurilReproductionUnit {
  id: string;
  name: string;
  repo_url: string;
  description: string;
  related_technology_ids: string[];
  commit: string;
  license: string;
  inputs: string[];
  outputs: string[];
  steps: string[];
  acceptance_criteria: string[];
  status: string;
  limitations: string[];
  source_ids: string[];
}

export interface AndurilKnowledge {
  title: string;
  updated_at: string | number;
  status: string;
  summary: string;
  counts: { products: number; technologies: number; routes: number; maturity: number; reproduction_units: number };
  products: AndurilProduct[];
  technologies: AndurilTechnology[];
  routes: AndurilRoute[];
  reproduction_units: AndurilReproductionUnit[];
  sources: AndurilSource[];
  gaps: string[];
  audit?: Record<string, unknown>;
}

export function safeKnowledgeUrl(value: string | undefined): string | null {
  if (!value || !/^https?:\/\//i.test(value)) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? value : null;
  } catch { return null; }
}

export function knowledgeSources(ids: string[], sources: AndurilSource[]): AndurilSource[] {
  const wanted = new Set(ids);
  return sources.filter((source) => wanted.has(source.id));
}

export function maturityLabel(stage: string): string {
  const labels: Record<string, string> = {
    concept: "概念阶段", prototype: "原型阶段", testing: "测试验证", development: "研发中",
    demonstration: "演示验证", demonstrated: "已演示", production: "生产阶段", in_production: "生产阶段",
    deployed: "已部署", operational: "已投入使用", fielded: "已列装", unknown: "成熟度待补充",
  };
  return labels[stage] || stage || "成熟度待补充";
}

export function reproductionLabel(status: string): string {
  if (status === "passed" || status === "reproduced") return "本机复现通过";
  if (status === "failed") return "运行未通过";
  if (status === "running") return "复现运行中";
  if (status === "not_run") return "尚未运行";
  return "复现状态待确认";
}

export function knowledgeMatches(query: string, ...values: string[]): boolean {
  return !query.trim() || values.join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
}

export function knowledgeUpdatedLabel(value: string | number): string {
  const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "更新时间待补充" : `更新于 ${date.toLocaleDateString("zh-CN")}`;
}
