"use client";

import { useEffect, useState } from "react";
import dynamic from "next/dynamic";
import { ArrowDownToLine, ArrowRight, BookOpen, Check, ChevronRight, Circle, Clock3, Database, FileSearch, FileText, FolderOpen, Layers3, Loader2, Plus, Search, Network, Settings2, Sparkles, Square, Terminal, Trash2, WifiOff } from "lucide-react";
import Answer, { cleanAnswer } from "@/components/conversation/Answer";
import AnswerActions from "@/components/conversation/AnswerActions";
import OrchestrationCard from "@/components/conversation/OrchestrationCard";
import CoverageTable from "@/components/coverage/CoverageTable";
import ResultExportMenu from "@/components/coverage/ResultExportMenu";
import AgentWall from "./AgentWall";
import TraceDrawer from "./TraceDrawer";
import InnovationControls from "./InnovationControls";
import InnovationPanel from "./InnovationPanel";
import ResourcePanel from "@/components/drawer/ResourcePanel";
import FrontierPanel from "@/components/drawer/FrontierPanel";
import LocalSourceControl from "@/components/local/LocalSourceControl";
import type { SubmitOpts } from "@/components/shell/Composer";
import type { LocalLibraryController } from "@/hooks/useLocalLibrary";
import type { useWorkbench } from "@/hooks/useWorkbench";
import type { HistoryItem } from "@/lib/api";
import { getResearchMaterials } from "@/lib/api";
import { turnElapsedLabel, turnModelLabel, workbenchModelSelection, type Turn } from "@/lib/conversation";
import { ENGINE_IDS, ENGINE_LABELS, readableError } from "@/lib/workbench";
import { localCitationLabels } from "@/lib/localCitations";
import { moduleConfigForTurn } from "@/lib/innovations";
import type { ResearchModuleId } from "@/lib/types";
import { declaredCoverageTables } from "@/lib/coverageTables";
import { useSettings } from "@/components/settings/SettingsProvider";

const ResearchGraphCanvas = dynamic(() => import("@/components/visualization/ResearchGraphCanvas"), {
  loading: () => <div className="wb-empty" role="status">正在打开研究图谱…</div>,
});

const DEMO_QUERY = "请根据示例资料，整理三个项目的负责人、当前阶段、截止日期和主要风险，用表格比较，并列出最需要跟进的事项。每项结论请标注资料来源。";
const EXAMPLES = [
  { label: "资料整理", query: "请从我上传的资料中提取关键事实、时间和数字，整理成表格，并标注来源。" },
  { label: "专题比较", query: "请比较三种主流数据标注工具的功能、部署方式和适用场景，优先引用官方资料。" },
  { label: "信息核实", query: "请核实我提供资料中的关键主张，区分已确认的信息和仍缺少证据的问题。" },
];
type Tab = "result" | "sources" | "process" | "graph" | "innovation";
interface Props {
  turns: Turn[]; history: HistoryItem[]; activeId: string | null; loadingId: string | null;
  historyStatus: "loading" | "ready" | "error"; historyQuery: string; historySearching: boolean;
  onSearch: (query: string) => void; onRetryHistory: () => void; onNew: () => void;
  onSelect: (id: string) => void; onDelete: (id: string) => void;
  running: boolean; reconnecting: boolean; stopping: boolean; error: string | null;
  onSubmit: (query: string, opts: SubmitOpts, freshRun?: boolean) => void;
  onStop: () => void; onRerun: (turn: Turn) => void;
  localLibrary: LocalLibraryController; onOpenKnowledgeBase: () => void; onOpenKnowledgeDatabase: (view?: "ingestion") => void; onOpenLocalSource: (ref: string) => void;
  workbench: ReturnType<typeof useWorkbench>; onOpenSettings: () => void;
  modules: ResearchModuleId[]; modulesLocked: boolean; onModulesChange: (modules: ResearchModuleId[]) => void;
}

export default function WorkbenchView(props: Props) {
  const { turns, history, running, workbench, localLibrary } = props;
  const [query, setQuery] = useState("");
  const [sourceOpen, setSourceOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("result");
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  const [traceAgent, setTraceAgent] = useState<string | null>(null);
  const [demoBusy, setDemoBusy] = useState(false);
  const [materialsBusy, setMaterialsBusy] = useState(false);
  const [materialsError, setMaterialsError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [mobilePanel, setMobilePanel] = useState(false);
  const [clock, setClock] = useState(0);
  const { overrides, setOverrides } = useSettings();
  const turn = turns.find((item) => item.id === selectedTurnId) ?? turns.at(-1) ?? null;
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  useEffect(() => {
    if (!mobilePanel) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setMobilePanel(false);
      document.getElementById("wb-mobile-toggle")?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mobilePanel]);
  const engineInfo = workbench.settings?.engines.find((item) => item.id === workbench.engine);
  const displayedModel = workbenchModelSelection(turns, running, { engine: workbench.engine, model: workbench.settings?.[workbench.engine]?.model });
  const model = displayedModel.model;
  const evidence = turn?.searchState?.evidence_graph?.nodes ?? [];
  const elapsedLabel = turn ? turnElapsedLabel(turn, clock || Date.now()) : "";
  const answer = cleanAnswer(turn?.answer ?? "");
  const localLabels = turn ? localCitationLabels(turn.events, evidence) : {};
  const localReadEntries = Object.entries(localLabels).filter(([ref]) => ref.includes("?rev="));
  const sourceCount = new Set([
    ...evidence.map((item) => item.source),
    ...localReadEntries.map(([ref]) => ref),
  ].filter(Boolean).map((source) => source.startsWith("local://") ? source.split("?")[0] : source)).size;
  const localSourceRefs = Array.from(new Set([
    ...evidence.map((node) => node.source).filter((source) => source.startsWith("local://")),
    ...localReadEntries.map(([ref]) => ref),
  ]));
  const readyDocuments = localLibrary.documents.filter((document) => document.searchable).length;

  const submit = () => {
    if (!query.trim() || running) return;
    if (!workbench.settings) { setFormError("后端尚未连接，请确认本机服务已启动。"); return; }
    if (!engineInfo?.available) { setFormError(engineInfo?.message || "当前模型尚未配置，请先打开设置完成连接。"); return; }
    if (localLibrary.mode !== "web" && !localLibrary.selection) { setFormError("请先添加或选择本地资料，等待资料可以检索后再开始。"); return; }
    setFormError(null); setSelectedTurnId(null); setTab("result"); setMobilePanel(false);
    props.onSubmit(query.trim(), { sources: localLibrary.selection ?? { mode: localLibrary.mode } });
    setQuery("");
  };
  const loadDemo = async () => {
    setDemoBusy(true); setFormError(null);
    try {
      const response = await fetch("/demo/project-brief.md");
      if (!response.ok) throw new Error("示例资料读取失败，请重试。");
      const text = await response.text();
      localLibrary.setMode("local");
      localLibrary.selectedDocumentIds.forEach((id) => localLibrary.setDocumentSelected(id, false));
      setSourceOpen(true); setQuery(DEMO_QUERY);
      await localLibrary.uploadFiles([new File([text], "示例资料_项目简报.md", { type: "text/markdown" })]);
    } catch (error) { setFormError(readableError(error)); }
    finally { setDemoBusy(false); }
  };
  const openResearchMaterials = async () => {
    setMaterialsBusy(true); setMaterialsError(null);
    try {
      const materials = await getResearchMaterials();
      if (materials.library_deleted) throw new Error("研究网页资料库已删除。");
      if (!materials.library_id) throw new Error("尚无已保存的任务网页资料，读取网页后会自动归档。");
      await localLibrary.openLibrary(materials.library_id);
      props.onOpenKnowledgeBase();
    } catch (error) { setMaterialsError(readableError(error)); }
    finally { setMaterialsBusy(false); }
  };

  return <div className="wb-app">
    <header className="wb-topbar">
      <button className="wb-brand" onClick={() => { if (!running) { props.onNew(); setQuery(""); setSelectedTurnId(null); } }} aria-label="资料研究台首页"><span className="wb-brand-icon"><Layers3 size={23} strokeWidth={1.6} /></span><span><strong>资料研究台</strong><small>PERSONAL RESEARCH WORKSPACE</small></span></button>
      <div className="wb-engine-switch" role="group" aria-label="选择研究模型">
        {ENGINE_IDS.map((id) => {
          const info = workbench.settings?.engines.find((item) => item.id === id);
          return <button key={id} className={displayedModel.engine === id ? "selected" : ""} aria-pressed={displayedModel.engine === id} disabled={running || workbench.switching || workbench.loading || !workbench.settings} title={running ? "当前任务运行中，结束后可切换模型" : info?.message || "正在检查配置"} onClick={() => { void workbench.setEngine(id); setFormError(null); }}><span className={`wb-engine-dot ${info?.available ? "available" : ""}`} />{ENGINE_LABELS[id]}{displayedModel.engine === id && <Check size={12} />}</button>;
        })}
      </div>
      <div className="wb-top-actions"><button className="wb-top-knowledge" title="打开产品与技术知识数据库" onClick={() => props.onOpenKnowledgeDatabase()}><Database size={16} /><span>知识数据库</span></button><button className="wb-icon-button" aria-label="打开工作台设置" title={running ? "任务运行中，结束后可修改设置" : "工作台设置"} disabled={running || !workbench.settings} onClick={props.onOpenSettings}><Settings2 size={20} /></button></div>
    </header>
    <button id="wb-mobile-toggle" aria-expanded={mobilePanel} aria-controls="wb-sidebar" className="wb-mobile-toggle" onClick={() => setMobilePanel(!mobilePanel)}><FileSearch size={16} />{mobilePanel ? "收起任务面板" : "新建任务与历史"}<ChevronRight size={15} /></button>
    <div className="wb-columns">
      <aside id="wb-sidebar" className={`wb-sidebar ${mobilePanel ? "mobile-open" : ""}`}>
        <div className="wb-sidebar-heading"><span className="wb-eyebrow">研究任务</span><button onClick={() => { props.onNew(); setQuery(""); setSelectedTurnId(null); setFormError(null); }} disabled={running} className="wb-text-button"><Plus size={14} />新任务</button></div>
        <div className="wb-task-form">
          <label htmlFor="wb-question" className="wb-question-label">这次想弄清什么？</label>
          <textarea id="wb-question" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") submit(); }} disabled={running} placeholder="描述你的研究问题、想比较的内容，或需要整理的资料…" />
          <div className="wb-source-heading"><span>资料来源</span><button className="wb-text-button" onClick={props.onOpenKnowledgeBase}><FolderOpen size={13} />资料库</button></div>
          <fieldset disabled={running} className="wb-source-control"><LocalSourceControl controller={localLibrary} open={sourceOpen} onOpenChange={setSourceOpen} /></fieldset>
          <div className="wb-effort"><label htmlFor="wb-effort">研究深度</label><select id="wb-effort" disabled={running} value={overrides.effort ?? "low"} onChange={(event) => setOverrides({ ...overrides, effort: event.target.value as "low" | "medium" | "high" })}><option value="low">快速 · 事实与简短资料</option><option value="medium">标准 · 比较与综合分析</option><option value="high">深入 · 多步专题研究</option></select></div>
          <InnovationControls modules={props.modules} locked={props.modulesLocked} running={running} onChange={props.onModulesChange} onNew={() => { props.onNew(); setQuery(""); setSelectedTurnId(null); setFormError(null); }} />
          {running ? <button className="wb-stop" onClick={props.onStop} disabled={props.stopping}><Square size={14} fill="currentColor" />{props.stopping ? "正在停止…" : "停止研究"}</button> : <button className="wb-start" disabled={!query.trim() || workbench.loading || workbench.switching || demoBusy || localLibrary.uploading} onClick={submit}>{turns.length ? "继续研究" : "开始研究"}<ArrowRight size={17} /></button>}
          <div className="wb-model-caption">{running ? "本轮模型已固定" : turns.length ? "下次研究模型" : "当前模型"}<span title={model}>{model || (running ? "模型未记录" : "等待配置")}</span></div>
          {(formError || workbench.error || props.error) && <div role="alert" className="wb-inline-error">{readableError(formError || workbench.error || props.error)}{workbench.error && <button onClick={() => void workbench.refresh()}>重试连接</button>}{!workbench.error && !running && <button onClick={props.onOpenSettings}>检查配置</button>}</div>}
        </div>
        <section className="wb-materials"><div className="wb-materials-actions"><button className="wb-secondary" disabled={materialsBusy} onClick={() => void openResearchMaterials()}>{materialsBusy ? <Loader2 size={14} className="animate-spin" /> : <FolderOpen size={14} />}任务资料库</button><button className="wb-secondary wb-knowledge-entry" title="打开产品与技术知识数据库" onClick={() => props.onOpenKnowledgeDatabase()}><Database size={14} />知识数据库<ArrowRight size={13} /></button></div><p>网页原文归档后自动抽取入库；产品、技术及其关系可在知识数据库中查看。</p>{materialsError && <div role="alert" className="wb-inline-error">{materialsError}</div>}</section>
        <section className="wb-demo"><div><BookOpen size={15} /><strong>从一份资料开始</strong></div><p>导入项目简报，体验整理、对比与引用。</p><button disabled={running || demoBusy || localLibrary.uploading} onClick={() => void loadDemo()}>{demoBusy ? <Loader2 size={13} className="animate-spin" /> : <Plus size={13} />}载入示例资料</button></section>
        <section className="wb-history"><div className="wb-sidebar-heading"><span className="wb-eyebrow">最近研究</span><span className="wb-count">{history.length}</span></div><div className="wb-history-search"><Search size={13} /><input value={props.historyQuery} placeholder="搜索历史研究" onChange={(event) => props.onSearch(event.target.value)} aria-label="搜索历史研究" />{props.historySearching && <Loader2 size={12} className="animate-spin" />}</div>
          {props.historyStatus === "error" ? <button className="wb-history-empty" onClick={props.onRetryHistory}>历史读取失败，点击重试</button> : props.historyStatus === "loading" ? <p className="wb-history-empty">正在读取历史…</p> : history.length === 0 ? <p className="wb-history-empty">{props.historyQuery ? "没有找到匹配的研究" : "完成的研究会保存在这里"}</p> : <div className="wb-history-items">{history.filter((item) => !item.archived).slice(0, 40).map((item) => <div key={item.session_id} className={`wb-history-item ${props.activeId === item.session_id ? "active" : ""}`}><button disabled={running || props.loadingId !== null} onClick={() => { props.onSelect(item.session_id); setSelectedTurnId(null); setTab("result"); setMobilePanel(false); }}>{props.loadingId === item.session_id ? <Loader2 size={14} className="animate-spin" /> : <FileText size={14} />}<span><strong>{item.title || "未命名研究"}</strong><small>{new Date(item.updated_at * 1000).toLocaleDateString("zh-CN", { month: "short", day: "numeric" })} · {item.status === "completed" ? "已完成" : item.status === "running" ? "运行中" : "未完成"}</small></span></button><button disabled={running} className="wb-history-delete" title="删除研究" aria-label={`删除研究：${item.title}`} onClick={() => props.onDelete(item.session_id)}><Trash2 size={13} /></button></div>)}</div>}
        </section>
        <footer className="wb-sidebar-footer"><Database size={13} />{readyDocuments ? `${readyDocuments} 份资料可检索` : "你的研究记录保存在本机"}</footer>
      </aside>
      <main className="wb-main">
        {!turn ? <div className="wb-welcome"><div className="wb-welcome-top"><span className="wb-eyebrow">从问题到有据可查的答案</span><span className="wb-edition">个人研究 · 基础版</span></div><div className="wb-welcome-copy"><span className="wb-welcome-symbol"><FileSearch size={39} strokeWidth={1.3} /></span><h1>把零散资料，<br /><span>变成清晰的判断。</span></h1><p>提出问题，选择模型与资料范围。<br />在一个工作台里完成检索、阅读、比较和整理。</p></div>
          <div className="wb-method">{[{ no: "01", title: "规划任务", detail: "拆解问题，明确研究范围" }, { no: "02", title: "检索与阅读", detail: "查找来源，提取相关事实" }, { no: "03", title: "整理结果", detail: "形成回答，保留引用出处" }].map((step) => <div key={step.no}><span>{step.no}</span><strong>{step.title}</strong><p>{step.detail}</p></div>)}</div>
          <div className="wb-examples-head"><h2>选个问题开始</h2><span>点击填入，按你的需要修改</span></div><div className="wb-examples">{EXAMPLES.map((example) => <button key={example.label} onClick={() => { setQuery(example.query); setMobilePanel(true); document.getElementById("wb-question")?.focus(); }}><span>{example.label}<ArrowRight size={15} /></span><p>{example.query}</p></button>)}</div><div className="wb-welcome-foot"><span><Circle size={8} fill="currentColor" />实时执行 · 来源可查 · 结果可导出</span><a href="/demo/project-brief.md" download><ArrowDownToLine size={13} />下载示例资料</a></div>
        </div> : <div className="wb-research">
          <header className="wb-research-head"><div className="wb-result-eyebrow"><span className={`wb-run-status ${turn.status}`}>{turn.status === "running" ? <Loader2 size={13} className="animate-spin" /> : turn.status === "completed" ? <Check size={13} /> : <Circle size={12} />}{turn.status === "running" ? "正在研究" : turn.status === "completed" ? "研究完成" : "研究未完成"}</span><span>{turn.sourceMode === "local" ? "本地资料" : turn.sourceMode === "hybrid" ? "混合研究" : "联网检索"}</span></div><h1>{turn.query}</h1><div className="wb-result-meta"><span><Clock3 size={13} />{elapsedLabel}</span><span><FileText size={13} />{sourceCount} 个来源</span><span><Layers3 size={13} />{evidence.length || localReadEntries.length} {evidence.length ? "条证据" : "个阅读片段"}</span><span title="本轮实际使用的模型">本轮模型：{turnModelLabel(turn.meta)}</span></div></header>
          {turns.length > 1 && <div className="wb-turns" aria-label="研究轮次">{turns.map((item, index) => <button key={item.id} className={item.id === turn.id ? "active" : ""} onClick={() => setSelectedTurnId(item.id)} title={item.query}>第 {index + 1} 轮</button>)}</div>}
          {turn.status !== "running" && turn.sourceMode !== "local" && <div className="wb-auto-ingestion-note"><Database size={15} /><span>网页原文归档后会自动抽取实体与关系，入库可能仍在进行。</span><button onClick={() => props.onOpenKnowledgeDatabase("ingestion")}>查看入库进度<ArrowRight size={13} /></button></div>}
          <div className="wb-results-tabs" role="tablist" aria-label="研究视图">{([{ id: "result", label: "研究结果", icon: FileText }, { id: "sources", label: "来源与证据", icon: BookOpen }, { id: "process", label: "执行过程", icon: Terminal }, { id: "graph", label: "研究图谱", icon: Network }, { id: "innovation", label: "增强记录", icon: Sparkles }] as const).map((item) => <button key={item.id} role="tab" id={`wb-tab-${item.id}`} aria-controls="wb-result-panel" aria-selected={tab === item.id} className={tab === item.id ? "active" : ""} onClick={() => setTab(item.id)}><item.icon size={15} />{item.label}{item.id === "sources" && evidence.length > 0 && <span>{evidence.length}</span>}</button>)}<div className="wb-export">{turn.searchState && turn.status === "completed" && <ResultExportMenu turn={turn} answer={answer} />}</div></div>
          {props.reconnecting && <div className="wb-reconnect" role="status"><WifiOff size={14} />连接暂时中断，正在重新连接。任务仍在后端运行。</div>}
          <div className="wb-result-content" id="wb-result-panel" role="tabpanel" aria-labelledby={`wb-tab-${tab}`}>
            {turn.error && <div role="alert" className="wb-inline-error">{readableError(turn.error)}<button disabled={running} onClick={() => props.onRerun(turn)}>重新运行</button></div>}
            {tab === "result" && <>
              {answer ? <div className="wb-answer"><Answer markdown={answer} localSources={localSourceRefs} localSourceLabels={localLabels} evidenceSources={Object.fromEntries(evidence.filter((node) => !node.status || node.status === "active").map((node) => [node.id, node.source]))} onOpenLocalSource={props.onOpenLocalSource} />{turn.status === "completed" && <AnswerActions query={turn.query} markdown={answer} collapsible={false} collapsed={false} onToggleCollapse={() => {}} onRerun={() => props.onRerun(turn)} runDisabled={running} />}</div> : turn.status === "running" ? <div className="wb-working"><span className="wb-working-icon"><Loader2 size={24} className="animate-spin" /></span><h2>研究正在进行</h2><p>真实执行记录会持续更新，你可以切换到“执行过程”查看。</p><button className="wb-text-button" onClick={() => setTab("process")}>查看执行过程<ArrowRight size={14} /></button></div> : <div className="wb-empty">本轮没有生成最终回答。请查看执行过程和错误信息。</div>}
              {declaredCoverageTables(turn.searchState?.coverage_map).length > 0 && <section className="wb-table"><h2>结构化结果</h2><CoverageTable coverageMap={turn.searchState?.coverage_map ?? null} evidence={evidence} animationScope={turn.id} onOpenLocalSource={props.onOpenLocalSource} /></section>}
              {turn.status === "completed" && <div className="wb-followup"><label htmlFor="wb-followup">继续完善这份研究</label><div><input id="wb-followup" value={query} placeholder="追加一个问题，继续当前研究…" disabled={running} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.nativeEvent.isComposing) submit(); }} /><button className="wb-primary" disabled={running || !query.trim()} onClick={submit}><ArrowRight size={17} /></button></div></div>}
            </>}
            {tab === "sources" && (evidence.length === 0 && localReadEntries.length === 0 ? <div className="wb-empty"><BookOpen size={26} /><p>还没有可展示的证据。读取资料后，来源会出现在这里。</p></div> : <div className="wb-sources">{evidence.length === 0 && localReadEntries.map(([ref, label], index) => <article key={ref} className="wb-source-card"><div className="wb-source-number">{String(index + 1).padStart(2, "0")}</div><div><h3>{label}</h3><p className="wb-help">本轮已读取的资料片段</p><div className="wb-source-footer"><span>本地资料</span><button onClick={() => props.onOpenLocalSource(ref)}><FileText size={12} />查看原文<ArrowRight size={12} /></button></div></div></article>)}{evidence.map((node, index) => <article key={node.id} className="wb-source-card"><div className="wb-source-number">{String(index + 1).padStart(2, "0")}</div><div><h3>{node.claim || node.value || node.attribute || "来源证据"}</h3>{node.source_excerpt && <blockquote>{node.source_excerpt}</blockquote>}<div className="wb-source-footer"><span>{node.entity || "资料来源"}</span>{node.source.startsWith("local://") ? <button onClick={() => props.onOpenLocalSource(node.source)}><FileText size={12} />{localLabels[node.source] || "查看原文"}<ArrowRight size={12} /></button> : /^https?:\/\//i.test(node.source) ? <a href={node.source} target="_blank" rel="noreferrer">{sourceLabel(node.source)}<ArrowRight size={12} /></a> : <span>{node.source}</span>}</div></div></article>)}</div>)}
            {tab === "innovation" && <InnovationPanel state={turn.searchState} config={moduleConfigForTurn(turn)} onOpenLocalSource={props.onOpenLocalSource} />}
            {tab === "graph" && <ResearchGraphCanvas key={turn.id} state={turn.searchState} events={turn.events} answer={turn.answer} status={turn.status} onOpenLocalSource={props.onOpenLocalSource} />}
            {tab === "process" && <div className="wb-process"><OrchestrationCard events={turn.events} searchState={turn.searchState} status={turn.status} workers={turn.workers} sourceMode={turn.sourceMode} onOpen={() => setTab("sources")} /><section><h2>执行中的研究任务</h2>{turn.workers.length ? <AgentWall workers={turn.workers} onSelect={setTraceAgent} /> : <p className="wb-help">等待任务分配；执行状态由后端实时更新。</p>}</section>{(turn.searchState?.frontier.questions.length ?? 0) > 0 && <FrontierPanel tasks={turn.searchState!.frontier.questions} />}<section><h2>本轮用量</h2><ResourcePanel turn={turn} /></section></div>}
          </div><TraceDrawer worker={turn.workers.find((worker) => worker.name === traceAgent) ?? null} onClose={() => setTraceAgent(null)} />
        </div>}
      </main>
    </div>
  </div>;
}

function sourceLabel(source: string) { try { return new URL(source).hostname.replace(/^www\./, ""); } catch { return "查看来源"; } }
