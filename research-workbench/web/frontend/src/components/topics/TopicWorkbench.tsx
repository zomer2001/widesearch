"use client";

import Image from "next/image";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowLeft, ArrowRight, ArrowUp, BookOpen, Check, ChevronDown, Circle, FolderOpen, LayoutGrid, Loader2, Menu, Plus, RefreshCw, Search, Settings2, Square, X, Cpu, Layers3, Network } from "lucide-react";
import WorkbenchSettings from "@/components/workbench/WorkbenchSettings";
import SourcePanel from "@/components/sources/SourcePanel";
import TopicKnowledgeView from "./TopicKnowledgeView";
import ResearchDatabaseView from "./ResearchDatabaseView";
import KnowledgeAssetsView from "./KnowledgeAssetsView";
import RawMaterialsView from "./RawMaterialsView";
import { readAssetSummary, type AssetNode } from "@/lib/researchAssets";
import useDialogFocus from "@/hooks/useDialogFocus";
import { useTopics } from "@/hooks/useTopics";
import { useWorkbench } from "@/hooks/useWorkbench";
import { activeTopicRun, topicDate, topicStatus, type TopicRun, type TopicTask } from "@/lib/topics";
import { ENGINE_IDS, ENGINE_LABELS, type EngineId } from "@/lib/workbench";

export default function TopicWorkbench() {
  const topics = useTopics(), workbench = useWorkbench();
  const [query, setQuery] = useState("");
  const [homeSearch, setHomeSearch] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [sourceRef, setSourceRef] = useState<string | null>(null);
  const [newTopic, setNewTopic] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [view, setView] = useState<"research" | "knowledge" | "fields" | "assets" | "materials">("research");
  const [assetTotals, setAssetTotals] = useState<Record<string, { product: number; technology: number; sources: number }>>({});
  const [assetTarget, setAssetTarget] = useState<AssetNode | null>(null);
  const [assetNavigation, setAssetNavigation] = useState(0);
  const [maxTasks, setMaxTasks] = useState(4);
  const [maxSeconds, setMaxSeconds] = useState(1200);
  const [adaptiveCollection, setAdaptiveCollection] = useState(false);
  const snapshot = topics.snapshot;
  const currentTopic = snapshot?.topic ?? topics.topics.find((topic) => topic.id === topics.selectedId);
  const activeRun = snapshot?.runs.find(activeTopicRun);
  const busy = !!topics.action;
  const selectedEngine = workbench.settings?.engines.find((engine) => engine.id === workbench.engine);
  const configured = !!workbench.settings && !!selectedEngine?.available;
  const canStart = !!query.trim() && !busy && !activeRun && configured && !!snapshot;
  const chatEnd = useRef<HTMLDivElement>(null);
  const latestRunId = snapshot?.runs[0]?.id;
  const homeFilter = homeSearch.trim().toLocaleLowerCase();
  const visibleTopics = topics.topics.filter((topic) => !homeFilter || [topic.title, topic.description, ...(topic.aliases ?? []), ...(topic.clusters ?? []).map((cluster) => cluster.label)].join(" ").toLocaleLowerCase().includes(homeFilter));
  const recentResearch = topics.topics.filter((topic) => topic.recent_run).sort((a, b) => (b.recent_run?.created_at ?? "").localeCompare(a.recent_run?.created_at ?? "")).slice(0, 5);
  useEffect(() => { if (view === "research") chatEnd.current?.scrollIntoView({ block: "end" }); }, [latestRunId, view]);

  useEffect(() => {
    if (!topics.loaded) return;
    const controller = new AbortController();
    void readAssetSummary(controller.signal).then((data) => { if (!controller.signal.aborted) setAssetTotals(data); }).catch(() => {});
    return () => controller.abort();
  }, [topics.loaded, topics.topics.length]);

  const submit = async (localOnly: boolean) => {
    if (!query.trim() || busy || activeRun || !snapshot || (!localOnly && !configured)) return;
    const result = await topics.research({ query: query.trim(), topic_id: snapshot.topic.id, engine: workbench.engine, max_tasks: maxTasks, max_seconds: maxSeconds, local_only: localOnly, modules: adaptiveCollection ? ["adaptive_collection"] : [] });
    if (result) { setQuery(""); setView("research"); }
  };
  const create = async (title: string) => {
    const topic = await topics.create(title);
    if (topic) { setNewTopic(false); setQuery(topic.title); setView("research"); setNavOpen(false); }
  };
  const select = (id: string) => { topics.select(id); setAssetTarget(null); setAssetNavigation((v) => v + 1); setView("assets"); setQuery(""); setNavOpen(false); };
  const leave = () => { topics.select(null); setQuery(""); setSourceRef(null); setNavOpen(false); };

  return <div className="tw-app">
    {navOpen && <button className="tw-nav-scrim" aria-label="关闭专题导航" onClick={() => setNavOpen(false)} />}
    <aside className={`tw-sidebar ${navOpen ? "is-open" : ""}`} aria-label="专题导航">
      <div className="tw-brand"><Image src="/brand/vernal-institute-mark-white.png" alt="春天研究院标志" width={48} height={48} priority unoptimized /><div><strong>春天研究院</strong><span>专题研究工作台</span></div><button className="tw-mobile-close" aria-label="收起专题导航" onClick={() => setNavOpen(false)}><X size={18} /></button></div>
      <button className={`tw-home-link ${!topics.selectedId ? "is-selected" : ""}`} onClick={leave}><LayoutGrid size={17} />全部专题</button>
      {topics.selectedId && <><div className="tw-sidebar-section"><span>专题</span><button className="tw-nav-add" aria-label="新建专题" title="新建专题" onClick={() => setNewTopic(true)}><Plus size={18} /></button></div><nav className="tw-topic-list" aria-label="选择专题">{topics.topics.map((topic) => <button key={topic.id} title={topic.title} className={topics.selectedId === topic.id ? "is-selected" : ""} aria-current={topics.selectedId === topic.id ? "page" : undefined} onClick={() => select(topic.id)}><FolderOpen size={17} /><span>{topic.title}</span><span className="tw-topic-count">{assetTotals[topic.namespace] ? assetTotals[topic.namespace].product + assetTotals[topic.namespace].technology : 0}</span></button>)}</nav></>}
      <footer className="tw-sidebar-footer"><span><Circle size={7} fill="currentColor" />本地知识库</span><button onClick={() => setSettingsOpen(true)} disabled={!workbench.settings}><Settings2 size={16} />设置</button></footer>
    </aside>

    <div className="tw-shell">
      <header className="tw-topbar"><button className="tw-mobile-menu tw-icon" aria-label="打开专题导航" onClick={() => setNavOpen(true)}><Menu size={20} /></button><div className="tw-breadcrumb">{topics.selectedId ? <><button onClick={leave} aria-label="返回专题选择"><ArrowLeft size={15} /></button><strong>{currentTopic?.title || "读取专题…"}</strong></> : <strong>专题知识库</strong>}</div><div className="tw-topbar-actions">{topics.selectedId && view === "research" && <label className="tw-engine"><span className="tw-sr-only">研究模型</span><select aria-label="研究模型" value={workbench.engine} disabled={workbench.loading || workbench.switching || !!activeRun} onChange={(event) => void workbench.setEngine(event.target.value as EngineId)}>{ENGINE_IDS.map((id) => <option key={id} value={id}>{ENGINE_LABELS[id]}{workbench.settings ? ` · ${workbench.settings[id].model}` : ""}</option>)}</select></label>}<button className="tw-icon" aria-label="刷新专题" title="刷新" onClick={topics.refresh}><RefreshCw size={16} /></button><button className="tw-icon" aria-label="打开设置" title="设置" disabled={!workbench.settings} onClick={() => setSettingsOpen(true)}><Settings2 size={17} /></button></div></header>

      {!topics.selectedId ? <main className="tw-topic-home">
        <div className="tw-home-heading"><div><h1>我的专题</h1><p>产品、技术与它们之间的联系。</p></div><button className="tw-button tw-primary" onClick={() => setNewTopic(true)}><Plus size={16} />新建专题</button></div>
        <div className="tw-home-tools"><span>{topics.topics.length} 个专题</span><label className="tw-home-search"><Search size={16} /><input aria-label="搜索专题或分支" placeholder="搜索专题或分支" value={homeSearch} onChange={(event) => setHomeSearch(event.target.value)} />{homeSearch && <button aria-label="清除专题搜索" onClick={() => setHomeSearch("")}><X size={14} /></button>}</label></div>
        {topics.listError && <div className="tw-error" role="alert">{topics.listError}<button onClick={topics.refresh}>重新连接</button></div>}
        {!topics.loaded ? <div className="tw-home-loading"><Loader2 size={24} className="animate-spin" /><p>读取专题…</p></div> : <>
          <div className="tw-topic-cards">{visibleTopics.map((topic) => {
            const branches = (topic.clusters ?? []).filter((cluster) => !cluster.parent_id && cluster.id !== "unclassified").slice(0, 4);
            const totals = assetTotals[topic.namespace];
            const empty = !totals?.product && !totals?.technology;
            return <button className="tw-topic-card" data-profile={topic.profile?.id ?? "generic"} key={topic.id} onClick={() => select(topic.id)}>
              <div className="tw-topic-card-top"><span className="tw-topic-card-icon">{topic.profile?.id === "anduril" ? <Network size={25} strokeWidth={1.5} /> : topic.profile?.id === "palantir" ? <Layers3 size={25} strokeWidth={1.5} /> : topic.profile?.id === "huawei" ? <Cpu size={25} strokeWidth={1.5} /> : <FolderOpen size={25} strokeWidth={1.5} />}</span><ArrowRight size={18} /></div>
              <h2>{topic.title}</h2>{topic.description && <p>{topic.description}</p>}
              {branches.length > 0 && <div className="tw-topic-branches">{branches.map((branch) => <span key={branch.id}>{branch.label}</span>)}</div>}
              <div className="tw-topic-card-meta">{empty ? <span className="tw-topic-empty-state">待采集</span> : <><span><b>{(totals?.product ?? 0) + (totals?.technology ?? 0)}</b> 个产品与技术</span><span><b>{totals?.sources ?? 0}</b> 份证据来源</span></>}{topic.recent_run && <time dateTime={topic.recent_run.created_at}>{topicDate(topic.recent_run.created_at)}</time>}</div>
            </button>;
          })}</div>
          {visibleTopics.length === 0 && <div className="tw-home-no-results"><Search size={23} /><p>{homeFilter ? "没有匹配的专题" : "创建第一个专题"}</p><button className="tw-button" onClick={() => homeFilter ? setHomeSearch("") : setNewTopic(true)}>{homeFilter ? "查看全部" : "新建专题"}</button></div>}
          {recentResearch.length > 0 && !homeFilter && <section className="tw-home-recent"><h2>最近研究</h2><div>{recentResearch.map((topic) => { const run = topic.recent_run!; return <button key={topic.id} onClick={() => select(topic.id)}><span className="tw-recent-topic">{topic.title}</span><strong>{run.query}</strong><span className={`tw-status tw-status-${run.status}`}>{topicStatus(run.status)}</span><time dateTime={run.created_at}>{topicDate(run.created_at)}</time><ArrowRight size={15} /></button>; })}</div></section>}
        </>}
      </main> : <>
        <nav className="tw-workspace-tabs" aria-label="专题工作区"><button aria-pressed={view === "assets"} onClick={() => { setAssetTarget(null); setAssetNavigation((v) => v + 1); setView("assets"); }}>知识资产</button><button aria-pressed={view === "materials"} onClick={() => setView("materials")}>原文资料</button><button aria-pressed={view === "research"} onClick={() => setView("research")}>研究</button><details className="ka-maintenance-tools"><summary>维护工具</summary><div><button aria-pressed={view === "fields"} onClick={() => setView("fields")}>字段数据库</button><button aria-pressed={view === "knowledge"} onClick={() => setView("knowledge")}>知识库</button></div></details></nav>
        {topics.error && <div className="tw-error tw-workspace-error" role="alert">{topics.error}<button onClick={topics.refresh}>重试</button></div>}
        {view === "assets" && currentTopic ? <main className="tw-knowledge-workspace"><KnowledgeAssetsView key={`${currentTopic.namespace}:${assetTarget?.id ?? "list"}:${assetNavigation}`} slug={currentTopic.namespace} title={currentTopic.title} initialAsset={assetTarget} /></main> : view === "materials" && currentTopic ? <main className="tw-knowledge-workspace"><RawMaterialsView key={currentTopic.namespace} slug={currentTopic.namespace} title={currentTopic.title} onOpenAsset={(node) => { setAssetTarget(node); setView("assets"); }} /></main> : view === "fields" && currentTopic ? <main className="tw-knowledge-workspace"><ResearchDatabaseView key={currentTopic.namespace} slug={currentTopic.namespace} title={currentTopic.title} /></main> : view === "knowledge" ? <main className="tw-knowledge-workspace">{snapshot ? <TopicKnowledgeView key={snapshot.topic.id} snapshot={snapshot} onOpenLocalSource={setSourceRef} onRefresh={topics.refresh} onResearch={(nextQuery) => { setQuery(nextQuery); setView("research"); }} /> : <div className="tw-knowledge-empty"><Loader2 size={28} className="animate-spin" /><p>正在读取专题…</p></div>}</main> : <main className="tw-chat-workspace">
          <div className="tw-chat-scroll"><div className="tw-chat-thread">{!snapshot ? <div className="tw-chat-welcome"><Loader2 size={26} className="animate-spin" /><p>正在读取研究记录…</p></div> : snapshot.runs.length === 0 ? <div className="tw-chat-welcome"><span className="tw-chat-emblem"><BookOpen size={28} strokeWidth={1.5} /></span><h1>{currentTopic?.title}</h1><p>从一个问题开始。</p></div> : [...snapshot.runs].reverse().map((run) => <ResearchTurn key={run.id} run={run} tasks={snapshot.tasks.filter((task) => task.run_id === run.id)} onOpenKnowledge={() => setView("knowledge")} />)}<div ref={chatEnd} /></div></div>
          <div className="tw-composer-wrap"><div className="tw-composer-inner">{activeRun && <div className="tw-active-bar"><span><Loader2 size={13} className="animate-spin" />{activeRun.message || topicStatus(activeRun.status)}</span><button className="tw-stop" disabled={topics.action === "stop" || activeRun.stop_requested || activeRun.status === "stopping"} onClick={() => void topics.stop(activeRun.id)}><Square size={10} fill="currentColor" />{topics.action === "stop" || activeRun.stop_requested || activeRun.status === "stopping" ? "正在停止" : "停止"}</button></div>}
            <form className="tw-composer" onSubmit={(event) => { event.preventDefault(); void submit(false); }}><label htmlFor="tw-query" className="tw-sr-only">主题词或调查要求</label><textarea id="tw-query" placeholder={`输入 ${currentTopic?.title || "这个专题"} 的调查要求…`} value={query} maxLength={12000} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") { event.preventDefault(); void submit(false); } }} /><div className="tw-composer-controls"><details className="tw-task-options"><summary><Settings2 size={14} />研究设置<ChevronDown size={12} /></summary><div className="tw-options-popover"><p>先查本地，再补采缺项。</p><label>自适应采集<input type="checkbox" role="switch" aria-label="自适应采集" checked={adaptiveCollection} disabled={!!activeRun} onChange={(event) => setAdaptiveCollection(event.target.checked)} /></label><label>最多任务<select value={maxTasks} disabled={!!activeRun} onChange={(event) => setMaxTasks(Number(event.target.value))}>{[1, 2, 4, 6].map((value) => <option value={value} key={value}>{value} 项</option>)}</select></label><label>最长时间<select value={maxSeconds} disabled={!!activeRun} onChange={(event) => setMaxSeconds(Number(event.target.value))}><option value={600}>10 分钟</option><option value={1200}>20 分钟</option><option value={2400}>40 分钟</option></select></label></div></details><div className="tw-composer-buttons"><button type="button" className="tw-local-button" disabled={!query.trim() || busy || !!activeRun || !snapshot} onClick={() => void submit(true)}><Search size={14} />仅查本地</button><button type="submit" className="tw-send-button" disabled={!canStart} aria-label="开始研究" title="开始研究">{topics.action === "research" ? <Loader2 size={18} className="animate-spin" /> : <ArrowUp size={19} />}</button></div></div></form>
            {!configured && !workbench.loading && <p className="tw-inline-note">{workbench.error || selectedEngine?.message || "请在设置中连接研究模型。"}<button onClick={() => workbench.settings ? setSettingsOpen(true) : void workbench.refresh()}>{workbench.settings ? "设置" : "重试"}</button></p>}{topics.actionError && <p className="tw-error" role="alert">{topics.actionError}</p>}
          </div></div>
        </main>}
      </>}
    </div>
    {newTopic && <NewTopicDialog busy={topics.action === "create"} error={topics.actionError} onClose={() => setNewTopic(false)} onCreate={create} />}
    {settingsOpen && workbench.settings && <WorkbenchSettings settings={workbench.settings} engine={workbench.engine} onClose={() => setSettingsOpen(false)} onSaved={workbench.apply} />}
    {sourceRef && <SourcePanel refId={sourceRef} onClose={() => setSourceRef(null)} />}
  </div>;
}

function NewTopicDialog({ busy, error, onClose, onCreate }: { busy: boolean; error: string | null; onClose: () => void; onCreate: (title: string) => Promise<void> }) {
  const [title, setTitle] = useState("");
  const root = useRef<HTMLDivElement>(null), input = useRef<HTMLInputElement>(null);
  useDialogFocus({ containerRef: root, initialFocusRef: input, onClose: () => { if (!busy) onClose(); } });
  const submit = (event: FormEvent) => { event.preventDefault(); if (title.trim() && !busy) void onCreate(title.trim()); };
  return <div className="tw-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}><div className="tw-dialog" ref={root} role="dialog" aria-modal="true" aria-labelledby="tw-new-topic-title"><header><h2 id="tw-new-topic-title">新建专题</h2><button className="tw-icon" disabled={busy} aria-label="关闭新建专题" onClick={onClose}><X size={19} /></button></header><form onSubmit={submit}><label htmlFor="tw-new-title">专题名称</label><input ref={input} id="tw-new-title" maxLength={200} placeholder="输入公司、技术或研究主题" value={title} onChange={(event) => setTitle(event.target.value)} />{error && <p className="tw-error" role="alert">{error}</p>}<footer><button className="tw-button" type="button" disabled={busy} onClick={onClose}>取消</button><button className="tw-button tw-primary" type="submit" disabled={!title.trim() || busy}>{busy ? "创建中…" : "创建专题"}</button></footer></form></div></div>;
}

function ResearchTurn({ run, tasks, onOpenKnowledge }: { run: TopicRun; tasks: TopicTask[]; onOpenKnowledge: () => void }) {
  const active = activeTopicRun(run);
  const localAnswer = run.local_only || (run.status === "completed" && !tasks.some((task) => task.kind === "research"));
  return <article className="tw-research-turn"><div className="tw-user-message">{run.query}</div><div className="tw-assistant-message"><div className="tw-response-meta"><span className={`tw-status tw-status-${run.status}`}>{active && <Loader2 size={12} className="animate-spin" />}{topicStatus(run.status)}</span><time dateTime={run.created_at}>{topicDate(run.created_at)}</time></div><p className="tw-response-text">{run.message || (active ? "正在检查已有知识并规划任务…" : "本轮研究记录已保存。")}</p>{localAnswer && run.local_result && <p className="tw-local-result">{run.local_result}</p>}<details className="tw-actions-disclosure"><summary><span>查看行动{tasks.length > 0 && <small>{tasks.length}</small>}</span><ChevronDown size={14} /></summary><RunActivity run={run} tasks={tasks} /></details>{run.error && <p className="tw-error" role="alert">{run.error}</p>}{!active && <button className="tw-view-knowledge" onClick={onOpenKnowledge}>查看知识库<ArrowRight size={14} /></button>}</div></article>;
}

function RunActivity({ run, tasks }: { run: TopicRun; tasks: TopicTask[] }) {
  const active = activeTopicRun(run);
  const planned = active ? (run.plan ?? []).filter((item) => !tasks.some((task) => task.facet === item.id)) : [];
  return <div className="tw-run-activity">
    {tasks.length > 0 && <ol className="tw-action-list">{tasks.map((task) => <li key={task.id} className={`tw-action-item tw-action-${task.status}`}><span className="tw-action-marker">{task.status === "completed" ? <Check size={12} /> : activeTopicRun(task) ? <Loader2 size={12} className="animate-spin" /> : task.status === "failed" ? <X size={12} /> : <Circle size={7} fill="currentColor" />}</span><div><div className="tw-action-title"><strong>{task.title}</strong><span>{topicStatus(task.status)}</span></div>{task.message && <p>{task.message}</p>}{task.adaptive_collection && <p>自适应采集 · {task.adaptive_collection.batches} 批反馈 · 估计新增 {task.adaptive_collection.new} 条 · 佐证 {task.adaptive_collection.corroboration} 条 · {task.adaptive_collection.groups} 个资料组{task.adaptive_collection.last_error ? " · 部分排序已回退" : ""}</p>}</div></li>)}</ol>}
    {planned.length > 0 && <ol className="tw-action-list tw-planned-list" aria-label="后续计划">{planned.map((item) => <li key={item.id} className="tw-action-item"><span className="tw-action-marker"><Circle size={7} /></span><div className="tw-action-title"><strong>{item.title}</strong><span>待执行</span></div></li>)}</ol>}
    {!active && !!run.remaining?.length && <details className="tw-remaining"><summary>本轮未覆盖 · {run.remaining.length} 项</summary><ul>{run.remaining.map((title) => <li key={title}>{title}</li>)}</ul></details>}
    {run.local_result && !run.local_only && !tasks.some((task) => task.message === run.local_result) && <div className="tw-local-result">{run.local_result}</div>}
    {active && tasks.length === 0 && <div className="tw-action-planning"><Loader2 size={15} className="animate-spin" /><span>正在检查已有知识并规划任务…</span></div>}
  </div>;
}
