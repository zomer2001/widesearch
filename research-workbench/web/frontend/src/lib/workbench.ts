import { API_BASE } from "./api";

export const ENGINE_IDS = ["deepseek", "openai", "ollama", "cli"] as const;
export type EngineId = typeof ENGINE_IDS[number];
export const ENGINE_LABELS: Record<EngineId, string> = {
  deepseek: "DeepSeek API", openai: "OpenAI API", ollama: "Ollama", cli: "CLI · 6-luna",
};
export interface WorkbenchSettings {
  engine: EngineId;
  deepseek: { model: string; api_base: string; api_key_configured: boolean };
  openai: { model: string; api_base: string; api_key_configured: boolean };
  ollama: { model: string; api_base: string };
  cli: { model: string; available: boolean };
  search: { provider: string; web_search_model: string; web_search_allowed_domains: string[]; openai_key_configured: boolean; serper_key_configured: boolean; tavily_key_configured: boolean; browser_backend: string };
  engines: { id: EngineId; label: string; available: boolean; message: string }[];
  ollama_models: { id: string; label: string }[];
}
export interface WorkbenchPatch {
  engine?: EngineId;
  deepseek?: { model?: string; api_base?: string; api_key?: string };
  openai?: { model?: string; api_base?: string; api_key?: string };
  ollama?: { model?: string; api_base?: string };
  search?: { provider?: string; web_search_model?: string; web_search_allowed_domains?: string[]; browser_backend?: string; serper_api_key?: string; tavily_api_key?: string };
}
export function readableError(error: unknown): string {
  return (error instanceof Error ? error.message : String(error)).replace(/SearchOS/gi, "资料研究台");
}
async function request<T>(path: string, body?: unknown, method = "GET"): Promise<T> {
  const response = await fetch(`${API_BASE}/api/workbench/${path}`, {
    method, cache: "no-store", signal: AbortSignal.timeout(path === "probe" ? 120000 : path === "crawl" ? 70000 : path === "search-probe" ? 75000 : 20000),
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(typeof data?.detail === "string" ? data.detail : `服务请求失败（${response.status}）`);
  }
  return response.json();
}
export const getWorkbenchSettings = () => request<WorkbenchSettings>("settings");
export const putWorkbenchSettings = (patch: WorkbenchPatch) => request<WorkbenchSettings>("settings", patch, "PUT");
export const probeEngine = (engine: EngineId) => request<{ ok: boolean; message: string }>("probe", { engine }, "POST");
export interface CrawlPreview {
  ok: boolean; title: string; url: string; status: number; error: string;
  characters: number; elapsed_s: number; preview: string;
  metadata: { fetch_backend?: string; fetched_at?: string; cache_hit?: boolean; fallback_reason?: string; browser_error?: string };
}
export const crawlPage = (url: string, backend: string) => request<CrawlPreview>("crawl", { url, backend }, "POST");
export interface SearchPreview {
  ok: boolean; provider: string; message: string; elapsed_s: number;
  results: { title: string; url: string; snippet: string }[];
}
export const probeSearch = (provider: string, api_key: string, query: string, web_search_model?: string, web_search_allowed_domains?: string[]) =>
  request<SearchPreview>("search-probe", { provider, api_key, query, ...(web_search_model === undefined ? {} : { web_search_model }), ...(web_search_allowed_domains === undefined ? {} : { web_search_allowed_domains }) }, "POST");
