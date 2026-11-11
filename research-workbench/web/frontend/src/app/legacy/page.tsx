"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import dynamic from "next/dynamic";
import WorkbenchView from "@/components/workbench/WorkbenchView";
import WorkbenchSettings from "@/components/workbench/WorkbenchSettings";
import { useWorkbench } from "@/hooks/useWorkbench";

import { useSearch } from "@/hooks/useSearch";
import { useLocalLibrary } from "@/hooks/useLocalLibrary";
import { useLiveFollowUps } from "@/hooks/useLiveFollowUps";
import { listHistory, loadHistory, deleteHistory, stopSearch, type HistoryItem, type HistoryTurn } from "@/lib/api";
import { deriveAnswer, foldWorkers } from "@/lib/derive";
import type { ResearchModuleId, SearchState, SourceSelection, WSEvent } from "@/lib/types";
import { createModuleConfig, moduleConfigForTurn, modulesForSubmission } from "@/lib/innovations";
import { sourceSelectionForTurn } from "@/lib/sourceSelection";
import {
  continuationSessionId,
  historyEventSegments,
  restoredRunState,
  type Turn,
} from "@/lib/conversation";
import type { SubmitOpts } from "@/components/shell/Composer";

import KnowledgeBasePanel from "@/components/local/KnowledgeBasePanel";
import SourcePanel from "@/components/sources/SourcePanel";
import { useSettings } from "@/components/settings/SettingsProvider";

const KnowledgeDatabasePanel = dynamic(() => import("@/components/workbench/KnowledgeDatabasePanel"));

let turnSeq = 0;

export default function Home() {
  const workbench = useWorkbench();
  const { session, run, attach, reset } = useSearch();
  const localLibrary = useLocalLibrary();
  const restoreSourceSelection = localLibrary.restoreSelection;
  const { overrides, notify, refresh: refreshSettings } = useSettings();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [selectedModules, setSelectedModules] = useState<ResearchModuleId[]>([]);
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [knowledgeBaseOpen, setKnowledgeBaseOpen] = useState(false);
  const [knowledgeDatabaseOpen, setKnowledgeDatabaseOpen] = useState(false);
  const [knowledgeDatabaseView, setKnowledgeDatabaseView] = useState<"ingestion" | undefined>();
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [historyQuery, setHistoryQuery] = useState("");
  const [historySearchResults, setHistorySearchResults] = useState<HistoryItem[] | null>(null);
  const [historySearching, setHistorySearching] = useState(false);
  const [historyStatus, setHistoryStatus] = useState<"loading" | "ready" | "error">("loading");
  const [historyLoadingId, setHistoryLoadingId] = useState<string | null>(null);
  const [stopPending, setStopPending] = useState(false);
  const [localSourceRef, setLocalSourceRef] = useState<string | null>(null);
  const historyBusyRef = useRef(false);
  const stopPendingRef = useRef(false);

  const sessionActive = session.status === "running" || session.status === "reconnecting";
  const liveFollowUps = useLiveFollowUps(session.sessionId, activeTurnId, sessionActive, notify);
  const { delivered: deliveredFollowUps, hasSnapshot: hasFollowUpSnapshot } = liveFollowUps;

  useEffect(() => {
    const root = document.documentElement;
    const phase = session.status === "running" || session.status === "reconnecting"
      ? "running"
      : session.status === "completed"
        ? "completed"
        : session.status === "error"
          ? "error"
          : "idle";
    root.dataset.maxPhase = phase;
    return () => {
      delete root.dataset.maxPhase;
    };
  }, [session.status]);

  const refreshHistory = useCallback(async (announceFailure = false) => {
    try {
      setHistory(await listHistory());
      setHistoryStatus("ready");
    } catch (e) {
      setHistoryStatus("error");
      if (announceFailure) {
        notify(`读取历史失败： ${e instanceof Error ? e.message : String(e)}。请检查后端连接后重试。`);
      }
    }
  }, [notify]);
  useEffect(() => { void refreshHistory(); }, [refreshHistory]);

  useEffect(() => {
    const query = historyQuery.trim();
    if (!query) {
      setHistorySearchResults(null);
      setHistorySearching(false);
      return;
    }
    let alive = true;
    setHistorySearching(true);
    const timer = window.setTimeout(() => {
      void listHistory(query)
        .then((items) => { if (alive) setHistorySearchResults(items); })
        .catch((error) => {
          if (alive) notify(`搜索历史失败： ${error instanceof Error ? error.message : String(error)}`);
        })
        .finally(() => { if (alive) setHistorySearching(false); });
    }, 250);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [historyQuery, notify]);

  // Sync the live `session` into the active (running) turn.
  useEffect(() => {
    if (!activeTurnId) return;
    const searchState: SearchState | null = session.liveState || session.result?.search_state || null;
    const status: Turn["status"] =
      session.status === "completed" ? "completed" : session.status === "error" ? "error" : "running";

    setTurns((prev) =>
      prev.map((t) => {
        if (t.id !== activeTurnId) return t;
        const liveAnswer = session.liveAnswer;
        return {
          ...t,
          sessionId: session.sessionId,
          status,
          events: session.events,
          workers: session.workers,
          followUps: hasFollowUpSnapshot ? deliveredFollowUps.map((m) => m.text) : t.followUps,
          searchState,
          moduleConfig: searchState?.module_config ?? session.result?.module_config ?? t.moduleConfig,
          // Prefer the durable result, including an intentional empty answer.
          // Fall back to trajectory-derived text only when no result exists.
          answer: session.result?.answer ?? (status === "completed"
            ? deriveAnswer(session.events)
            : liveAnswer ?? t.answer),
          error: session.error,
          meta:
            status === "completed" || status === "error"
              ? {
                  ...t.meta,
                  engine: session.result?.engine ?? t.meta.engine,
                  engineModel: session.result?.engine_model ?? t.meta.engineModel,
                  coverageScore: session.result?.coverage_score ?? undefined,
                  evidenceCount: session.result?.evidence_count,
                  elapsed: session.result?.elapsed_s ?? (t.meta.startedAt ? session.elapsed : t.meta.elapsed),
                  verdict: session.result?.eval_verdict ?? null,
                  totalQueries: session.result?.total_queries,
                  totalSteps: session.result?.total_steps,
                  toolCounts: session.result?.tool_counts,
                  tokenUsage: session.result?.token_usage,
                  tokenPhases: session.result?.token_phases,
                  modelDistribution: session.result?.model_distribution,
                }
              : { ...t.meta, elapsed: t.meta.startedAt ? session.elapsed : t.meta.elapsed },
        };
      }),
    );
  }, [session, activeTurnId, deliveredFollowUps, hasFollowUpSnapshot]);

  // When a run finishes, the new workspace appears on disk — refresh history.
  useEffect(() => {
    if (session.status === "completed" || session.status === "error") void refreshHistory();
  }, [session.status, refreshHistory]);

  const handleSubmit = useCallback(
    (q: string, opts: SubmitOpts, freshRun = false) => {
      stopPendingRef.current = false;
      setStopPending(false);
      // Keep the conversation across modes. The server fixes each turn's
      // source scope and resets prior evidence whenever that scope changes.
      const sources: SourceSelection = structuredClone(opts.sources ?? { mode: "web" });
      const localRun = sources.mode === "local";
      const sourceMode = sources.mode;
      const followUpTo = continuationSessionId(turns, sourceMode, freshRun);
      const previous = turns.at(-1);
      const selected = opts.modules ?? (previous && !freshRun ? moduleConfigForTurn(previous)?.enabled ?? [] : selectedModules);
      const modules = modulesForSubmission(previous, selected, !!followUpTo);
      const sessionTurns = followUpTo
        ? turns
            .filter((t) => t.status === "completed" && t.sessionId === followUpTo)
        : [];
      const history = followUpTo
        ? sessionTurns
            .map((t) => ({ query: t.query, answer: t.answer }))
        : undefined;

      const id = `t${++turnSeq}`;
      const turn: Turn = {
        id, query: q, sessionId: null, status: "running",
        events: [], workers: [], searchState: null, stateSource: "live",
        sourceMode, sources, answer: "",
        moduleConfig: modules ? createModuleConfig(modules) : undefined,
        meta: { engine: workbench.engine, engineModel: workbench.settings?.[workbench.engine].model, startedAt: Date.now() },
        error: null,
      };
      setTurns((prevTurns) => freshRun ? [turn] : [...prevTurns, turn]);
      setActiveTurnId(id);
      run(
        {
          query: q,
          engine: workbench.engine,
          modules,
          entities: opts.entities,
          attrs: opts.attrs,
          table_label: opts.tableLabel,
          primary_key: opts.primaryKey,
          row_label: opts.rowLabel,
          tables: opts.tables,
          relations: opts.relations,
          effort: overrides.effort ?? "low",
          max_time: overrides.max_time,
          skills: overrides.skills,
          trusted_domains: localRun ? [] : overrides.trusted_domains,
          excluded_domains: localRun ? [] : overrides.excluded_domains,
          sources,
          follow_up_to: followUpTo,
          history,
        },
        {
          seen: followUpTo ? sessionTurns.flatMap((item) => item.events) : undefined,
          onStarted: (response) => setTurns((current) => current.map((item) => item.id !== id ? item : {
            ...item,
            moduleConfig: response.module_config ?? item.moduleConfig,
            meta: { ...item.meta, engine: response.engine ?? item.meta.engine, engineModel: response.engine_model ?? item.meta.engineModel },
          })),
        },
      );
    },
    [run, overrides, turns, workbench.engine, workbench.settings, selectedModules],
  );

  const handleRerun = useCallback((turn: Turn) => {
    const sources = sourceSelectionForTurn(turn);
    restoreSourceSelection(sources);
    handleSubmit(turn.query, { sources, modules: moduleConfigForTurn(turn)?.enabled ?? [] }, true);
  }, [handleSubmit, restoreSourceSelection]);

  // Interrupt the live run (TUI Esc parity). The backend cancels the engine
  // task; the WS then delivers the terminal event and the turn settles.
  const handleStop = useCallback(async () => {
    const sid = session.sessionId;
    if (!sid || (session.status !== "running" && session.status !== "reconnecting") || stopPendingRef.current) return;
    stopPendingRef.current = true;
    setStopPending(true);
    try {
      await stopSearch(sid);
      notify("已请求停止，正在等待当前任务结束。", "info");
    } catch (e) {
      stopPendingRef.current = false;
      setStopPending(false);
      notify(`停止失败： ${e instanceof Error ? e.message : String(e)}。请重试。`);
    }
  }, [session.sessionId, session.status, notify]);

  const handleNew = useCallback(() => {
    reset();
    setTurns([]);
    setSelectedModules([]);
    setActiveTurnId(null);
    stopPendingRef.current = false;
    setStopPending(false);
    // A live run we just walked away from keeps running server-side — make
    // sure it shows up in the rail (as running) so the user can switch back.
    void refreshHistory();
  }, [reset, refreshHistory]);


  // Currently-open session id (live turn's sessionId or a loaded session id).
  const openTurn = turns[turns.length - 1] ?? null;
  const openId = openTurn ? openTurn.sessionId ?? openTurn.id : null;

  const handleSelect = useCallback(
    async (id: string) => {
      // Already open? just scroll to it.
      const existing = turns.find((t) => (t.sessionId ?? t.id) === id);
      if (existing) {
        return;
      }
      if (historyBusyRef.current) return;
      historyBusyRef.current = true;
      setHistoryLoadingId(id);
      try {
        const data = await loadHistory(id);
        const events: WSEvent[] = data.events ?? [];
        const isRunning = data.status === "running";
        // Restore the full dialogue: one Turn per reconstructed turn. Each
        // run appends a `task_start` trajectory event, so the flat event log
        // splits into per-turn segments. Only segments that reached
        // `task_complete` align with completed dialogue turns; interrupted
        // runs must not shift another version's orchestration trace.
        const hist: HistoryTurn[] = data.turns.length ? data.turns : [{
          query: data.query,
          answer: data.answer ?? "",
          search_state: data.search_state,
          state_source: data.search_state ? "latest" : "unavailable",
          coverage_score: data.coverage_score,
          evidence_count: data.evidence_count,
        }];
        const segments = historyEventSegments(events, hist.length);
        const last = hist.length - 1;
        const restored: Turn[] = hist.map((h, i) => {
          const segEvents = segments[i] ?? [];
          const turnRunning = i === last && isRunning;
          const runState = restoredRunState(i === last ? data.status : "completed");
          return {
            id: i === last ? data.session_id : `${data.session_id}#${i}`,
            query: h.query || data.query,
            sessionId: data.session_id,
            status: runState.status,
            events: segEvents,
            workers: foldWorkers(segEvents, !turnRunning, runState.status === "error" ? "error" : "completed"),
            searchState: h.search_state,
            moduleConfig: h.search_state?.module_config ?? h.module_config ?? data.module_config,
            stateSource: turnRunning ? "live" : h.state_source,
            sourceMode: h.search_state?.source_scope?.mode ?? (segEvents.some((event) => (
              event.type === "trajectory"
              && (event.data as Record<string, unknown> | undefined)?.source_mode === "local"
            )) ? "local" : segEvents.some((event) => (
              event.type === "trajectory"
              && (event.data as Record<string, unknown> | undefined)?.source_mode === "hybrid"
            )) ? "hybrid" : "web"),
            answer: h.answer || (i === last ? data.answer : ""),
            followUps: h.steers?.length ? h.steers : undefined,
            meta: {
              // Session metadata describes only its latest turn; older turns
              // use their own recorded model distribution when available.
              engine: h.engine ?? (i === last ? data.engine : undefined),
              engineModel: h.engine_model ?? (i === last ? data.engine_model : undefined),
              coverageScore: h.coverage_score ?? undefined,
              evidenceCount: h.evidence_count ?? undefined,
              elapsed: h.elapsed_s ?? undefined,
              totalQueries: h.total_queries ?? undefined,
              totalSteps: h.total_steps ?? undefined,
              toolCounts: h.tool_counts ?? undefined,
              tokenUsage: h.token_usage ?? undefined,
              tokenPhases: h.token_phases ?? undefined,
              modelDistribution: h.model_distribution ?? undefined,
            },
            error: runState.error,
          };
        });
        const turn = restored[last];
        restoreSourceSelection(sourceSelectionForTurn(turn));
        setTurns(restored);
        // If we just walked away from a live run, it keeps running server-side
        // — refresh so the rail lists it (as running) for switching back.
        void refreshHistory();
        if (isRunning) {
          // The backend still owns this run — re-attach the live WS/steer
          // channel so events keep streaming and follow-ups can be injected.
          // Seed only the live turn's segment; dedupe against the full log.
          stopPendingRef.current = false;
          setStopPending(false);
          setActiveTurnId(turn.id);
          attach(data.session_id, { events: turn.events, searchState: turn.searchState, seen: events });
        } else {
          reset();
          setActiveTurnId(null);
        }
      } catch (e) {
        const title = history.find((item) => item.session_id === id)?.title ?? "this conversation";
        notify(`Couldn’t open ${title}: ${e instanceof Error ? e.message : String(e)}. Refresh history and try again.`);
      } finally {
        historyBusyRef.current = false;
        setHistoryLoadingId(null);
      }
    },
    [turns, reset, attach, refreshHistory, history, notify, restoreSourceSelection],
  );

  const handleDelete = useCallback(async (id: string) => {
    if (historyBusyRef.current) return;
    historyBusyRef.current = true;
    try {
      await deleteHistory(id);
      setHistory((prev) => prev.filter((h) => h.session_id !== id));
      setHistorySearchResults((prev) => prev?.filter((h) => h.session_id !== id) ?? null);
      if (openId === id) handleNew();
      notify("研究记录已删除", "success");
      void refreshHistory();
    } catch (e) {
      notify(`删除研究记录失败： ${e instanceof Error ? e.message : String(e)}。记录仍保留。`);
    } finally {
      historyBusyRef.current = false;
    }
  }, [openId, handleNew, refreshHistory, notify]);

  const handleRetryHistory = useCallback(() => {
    if (historyStatus === "loading") return;
    setHistoryStatus("loading");
    void refreshHistory(true);
  }, [historyStatus, refreshHistory]);

  return <>
    <WorkbenchView turns={turns} history={historySearchResults ?? history} activeId={openId}
      modules={openTurn ? moduleConfigForTurn(openTurn)?.enabled ?? [] : selectedModules}
      modulesLocked={turns.length > 0} onModulesChange={setSelectedModules}
      loadingId={historyLoadingId} historyStatus={historyStatus} historyQuery={historyQuery}
      historySearching={historySearching} onSearch={setHistoryQuery} onRetryHistory={handleRetryHistory}
      onNew={handleNew} onSelect={(id) => { void handleSelect(id); }} onDelete={(id) => { void handleDelete(id); }}
      running={sessionActive} reconnecting={session.status === "reconnecting"} stopping={stopPending}
      error={session.error} onSubmit={handleSubmit} onStop={() => { void handleStop(); }} onRerun={handleRerun}
      localLibrary={localLibrary} onOpenKnowledgeBase={() => setKnowledgeBaseOpen(true)} onOpenKnowledgeDatabase={(view) => { setKnowledgeDatabaseView(view); setKnowledgeDatabaseOpen(true); }}
      onOpenLocalSource={setLocalSourceRef} workbench={workbench} onOpenSettings={() => setSettingsOpen(true)} />
    {settingsOpen && workbench.settings && <WorkbenchSettings settings={workbench.settings}
      engine={workbench.engine} onClose={() => setSettingsOpen(false)}
      onSaved={(settings) => { workbench.apply(settings); refreshSettings(); }} />}
    {knowledgeBaseOpen && <KnowledgeBasePanel controller={localLibrary} onClose={() => setKnowledgeBaseOpen(false)} />}
    {knowledgeDatabaseOpen && <KnowledgeDatabasePanel initialTab={knowledgeDatabaseView} onClose={() => setKnowledgeDatabaseOpen(false)} onOpenLocalSource={setLocalSourceRef} sourceOpen={!!localSourceRef} />}
    {localSourceRef && <SourcePanel key={localSourceRef} refId={localSourceRef} onClose={() => setLocalSourceRef(null)} />}
  </>;
}
