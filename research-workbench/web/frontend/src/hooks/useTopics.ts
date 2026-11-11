"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { createTopic, listTopics, researchTopic, retainedTopicSelection, stopTopicRun, topicFromSnapshot, watchTopic, type ResearchInput, type Topic, type TopicSnapshot } from "@/lib/topics";

export function useTopics() {
  const [topics, setTopics] = useState<Topic[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [snapshot, setSnapshot] = useState<TopicSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [action, setAction] = useState<"create" | "research" | "stop" | null>(null);
  const observer = useRef<ReturnType<typeof watchTopic> | null>(null);
  const listController = useRef<AbortController | null>(null);
  const actionController = useRef<AbortController | null>(null);
  const mounted = useRef(false);

  const refreshList = useCallback(async () => {
    listController.current?.abort();
    const controller = new AbortController(); listController.current = controller;
    try {
      const data = await listTopics(controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      setTopics(data.topics); setListError(null);
      setSelectedId((current) => retainedTopicSelection(current, data.topics));
    } catch (cause) {
      if (mounted.current && !controller.signal.aborted) setListError(cause instanceof Error ? cause.message : "读取专题失败");
    } finally { if (mounted.current && !controller.signal.aborted) setLoaded(true); }
  }, []);
  useEffect(() => {
    mounted.current = true; void refreshList();
    return () => { mounted.current = false; listController.current?.abort(); actionController.current?.abort(); };
  }, [refreshList]);
  useEffect(() => {
    if (!selectedId) return;
    const monitor = watchTopic({ id: selectedId, onData: (value) => {
      setSnapshot(value);
      setTopics((items) => items.map((item) => item.id === value.topic.id ? topicFromSnapshot(value, item) : item));
    }, onError: setError });
    observer.current = monitor;
    return () => { monitor.stop(); if (observer.current === monitor) observer.current = null; };
  }, [selectedId]);

  const refresh = useCallback(() => { observer.current?.refresh(); void refreshList(); }, [refreshList]);
  const perform = useCallback(async <T,>(kind: "create" | "research" | "stop", operation: (signal: AbortSignal) => Promise<T>) => {
    if (actionController.current) return null;
    const controller = new AbortController(); actionController.current = controller;
    setAction(kind); setActionError(null);
    try {
      const result = await operation(controller.signal);
      if (!mounted.current || controller.signal.aborted) return null;
      return result;
    } catch (cause) {
      if (mounted.current && !controller.signal.aborted) setActionError(cause instanceof Error ? cause.message : "操作未完成");
      return null;
    } finally {
      if (actionController.current === controller) actionController.current = null;
      if (mounted.current && !controller.signal.aborted) setAction(null);
    }
  }, []);
  const create = useCallback(async (title: string) => {
    const topic = await perform("create", (signal) => createTopic(title, signal));
    if (topic) { setTopics((items) => [topic, ...items.filter((item) => item.id !== topic.id)]); setSelectedId(topic.id); }
    return topic;
  }, [perform]);
  const research = useCallback(async (input: ResearchInput) => {
    const result = await perform("research", (signal) => researchTopic(input, signal));
    if (result) { setSelectedId(result.topic_id); observer.current?.refresh(); void refreshList(); }
    return result;
  }, [perform, refreshList]);
  const stop = useCallback(async (runId: string) => {
    await perform("stop", (signal) => stopTopicRun(runId, signal));
    observer.current?.refresh();
  }, [perform]);
  const select = useCallback((id: string | null) => { setSelectedId(id); setActionError(null); setError(null); }, []);
  return { topics, selectedId, select, loaded, snapshot: snapshot?.topic.id === selectedId ? snapshot : null, error, listError, actionError, action, create, research, stop, refresh };
}
