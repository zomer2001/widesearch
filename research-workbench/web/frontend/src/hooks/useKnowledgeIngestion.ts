"use client";

import { useEffect, useRef, useState } from "react";
import { monitorKnowledgeIngestion } from "@/lib/knowledgeIngestion";
import { retryKnowledgeIngestion, startKnowledgeIngestion, type KnowledgeIngestionSnapshot, type KnowledgeSnapshot } from "@/lib/knowledgeDatabase";

export default function useKnowledgeIngestion(onDatabase: (data: KnowledgeSnapshot) => void) {
  const [data, setData] = useState<KnowledgeIngestionSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null), [actionError, setActionError] = useState<string | null>(null);
  const [action, setAction] = useState<string | null>(null), [notice, setNotice] = useState<string | null>(null);
  const monitor = useRef<ReturnType<typeof monitorKnowledgeIngestion> | null>(null);
  const active = useRef(true), actionLock = useRef(false), actionRequest = useRef<AbortController | null>(null);
  useEffect(() => {
    active.current = true;
    const observer = monitorKnowledgeIngestion({ onUpdate: setData, onError: setError, onDatabase });
    monitor.current = observer;
    return () => { active.current = false; observer.stop(); monitor.current = null; actionRequest.current?.abort(); };
  }, [onDatabase]);
  const perform = async (identity: string, operation: (signal: AbortSignal) => Promise<KnowledgeIngestionSnapshot>) => {
    if (actionLock.current || !active.current) return;
    actionLock.current = true;
    const controller = new AbortController(); actionRequest.current = controller;
    setAction(identity); setActionError(null); setNotice(null);
    try {
      const snapshot = await operation(controller.signal);
      if (!active.current || controller.signal.aborted) return;
      setData(snapshot);
      setNotice(identity === "latest"
        ? (snapshot.scheduled ?? 0) > 0 ? `已登记 ${snapshot.scheduled} 份原文，可在列表查看进度。`
          : (snapshot.reused ?? 0) > 0 ? `已复用 ${snapshot.reused} 份原文的处理记录，无需重复登记。`
            : "最近研究任务没有新增可抽取原文，现有知识已保留。"
        : "已登记重新抽取，可在列表查看进度。");
      monitor.current?.refresh();
    } catch (cause) {
      if (active.current && !controller.signal.aborted) setActionError(cause instanceof Error ? cause.message : "未能登记抽取任务，请重试。");
    } finally { actionLock.current = false; if (active.current) setAction(null); }
  };
  return { data, error, actionError, action, notice,
    refresh: () => { setNotice(null); monitor.current?.refresh(); },
    queueLatest: () => perform("latest", (signal) => startKnowledgeIngestion(undefined, signal)),
    retry: (id: string) => perform(id, (signal) => retryKnowledgeIngestion(id, signal)),
  };
}
