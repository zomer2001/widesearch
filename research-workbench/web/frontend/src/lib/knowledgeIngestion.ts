import { activeIngestion, getKnowledgeDatabase, getKnowledgeIngestion, type KnowledgeIngestionSnapshot, type KnowledgeSnapshot } from "./knowledgeDatabase";

interface MonitorOptions {
  onUpdate: (data: KnowledgeIngestionSnapshot) => void;
  onDatabase: (data: KnowledgeSnapshot) => void;
  onError: (message: string | null) => void;
  fetchJobs?: typeof getKnowledgeIngestion;
  fetchDatabase?: typeof getKnowledgeDatabase;
  schedule?: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>;
  unschedule?: (timer: ReturnType<typeof setTimeout>) => void;
}

/** Opening a database only observes authorized jobs; this monitor never sends POST. */
export function monitorKnowledgeIngestion({ onUpdate, onDatabase, onError, fetchJobs = getKnowledgeIngestion, fetchDatabase = getKnowledgeDatabase, schedule = globalThis.setTimeout, unschedule = globalThis.clearTimeout }: MonitorOptions) {
  let stopped = false, generation = 0, failures = 0, previouslyActive = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;
  const poll = async (currentGeneration: number) => {
    const request = new AbortController(); controller = request;
    try {
      const data = await fetchJobs(request.signal);
      if (stopped || request.signal.aborted || currentGeneration !== generation) return;
      onUpdate(data);
      const active = activeIngestion(data) > 0;
      if (active) previouslyActive = true;
      // Refresh on the terminal transition as well, so the last completed job
      // reaches the graph without forcing the user to reopen the database.
      if (previouslyActive) {
        const database = await fetchDatabase(request.signal);
        if (stopped || request.signal.aborted || currentGeneration !== generation) return;
        onDatabase(database);
      }
      previouslyActive = active;
      failures = 0; onError(null);
    } catch {
      if (stopped || request.signal.aborted || currentGeneration !== generation) return;
      failures++;
      onError(failures >= 3 ? "读取入库进度失败，已暂停自动刷新。请点击刷新进度重试。" : "暂时无法读取入库进度，正在重试；已有数据仍保留。");
    } finally {
      if (!stopped && currentGeneration === generation && failures < 3) timer = schedule(() => { void poll(currentGeneration); }, 5000);
    }
  };
  const refresh = () => {
    generation++; failures = 0;
    if (timer !== undefined) unschedule(timer);
    controller?.abort();
    if (!stopped) void poll(generation);
  };
  refresh();
  return {
    refresh,
    stop() { stopped = true; generation++; controller?.abort(); if (timer !== undefined) unschedule(timer); },
  };
}
