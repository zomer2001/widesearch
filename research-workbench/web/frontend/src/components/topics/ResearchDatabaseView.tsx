"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, FileText, Loader2, RefreshCw, Search, X } from "lucide-react";
import useDialogFocus from "@/hooks/useDialogFocus";
import { topicStatus } from "@/lib/topics";
import { fieldValue, readFieldDatabase,
  readFieldJob, readFieldJobs, readFieldSource, retryFieldJob,
  type FieldJobs, type FieldSnapshot, type JobDetail } from "@/lib/researchDatabase";

import HandoffReviewPanel from "./HandoffReviewPanel";

export default function ResearchDatabaseView({ slug, title }: {
  slug: string; title: string;
}) {
  const [snapshot, setSnapshot] = useState<FieldSnapshot | null>(null);
  const [jobs, setJobs] = useState<FieldJobs | null>(null);
  const [query, setQuery] = useState("");
  const [dimension, setDimension] = useState(0);
  const [entity, setEntity] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [source, setSource] = useState<{ title: string; url: string; text_content: string } | null>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const closeDialog = useCallback(() => { setSource(null); setDetail(null); }, []);
  useDialogFocus({ containerRef: dialog, onClose: closeDialog, active: !!source || !!detail });

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const [data, progress] = await Promise.all([
      readFieldDatabase(slug, query, dimension, entity, signal), readFieldJobs(slug, signal),
    ]);
    if (signal?.aborted) return;
    setSnapshot(data); setJobs(progress); setError("");
  }, [slug, query, dimension, entity]);

  useEffect(() => {
    const controller = new AbortController();
    const load = () => { void refresh(controller.signal).catch((cause) => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    }); };
    const initial = setTimeout(load, 200);
    const interval = setInterval(load, 4000);
    return () => { controller.abort(); clearTimeout(initial); clearInterval(interval); };
  }, [refresh]);

  const action = async (task: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await task(); await refresh(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };
  const active = (jobs?.counts.queued ?? 0) + (jobs?.counts.running ?? 0);
  const modelEnabled = jobs?.runtime?.model_enabled === true;

  return <section className="rd-view" aria-label={`${title}字段数据库`}>
    <header className="rd-header"><div><h1>字段数据库</h1><p>从归档原文提取字段，按七类要素整理并保留依据。</p></div>
      <div className="rd-actions"><button className="tw-button tw-primary" disabled={busy} aria-expanded={importOpen} onClick={() => setImportOpen((value) => !value)}><FileText size={15} />接收抽取结果</button>
      <button className="tw-icon" aria-label="刷新字段数据库" onClick={() => void action(() => refresh())}><RefreshCw size={16} /></button></div>
    </header>
    {error && <p className="rd-error" role="alert">{error}</p>}
    {importOpen && <HandoffReviewPanel slug={slug} onClose={() => setImportOpen(false)} onCommitted={() => void action(() => refresh())} />}
    <p className="rd-runtime">{modelEnabled ? "模型抽取已启用" : "自动抽取已暂停"} · 外部结果经预览确认后入库</p>
    <div className="rd-stats"><span><b>{snapshot?.counts.fields ?? "—"}</b> 条字段与关系</span>
      <span><b>{snapshot?.counts.entities ?? "—"}</b> 个对象与词条</span><span><b>{snapshot?.counts.sources ?? "—"}</b> 份来源</span>
      {active > 0 && <span><Loader2 size={14} className="animate-spin" />{active} 份处理中</span>}</div>
    <nav className="rd-dimensions" aria-label="七类要素">{snapshot?.dimensions.map((item) =>
      <button key={item.id} data-dimension={item.id} aria-pressed={dimension === item.id} onClick={() => { setDimension(dimension === item.id ? 0 : item.id); setEntity(""); }}>
        <span>{item.id}</span><strong>{item.name}</strong><small>{item.records} 条</small>
      </button>)}</nav>
    <div className="rd-tools"><label className="rd-search"><Search size={16} /><input aria-label="搜索对象或字段" value={query} placeholder="搜索对象、字段或内容" onChange={(event) => setQuery(event.target.value)} /></label>
      <select aria-label="选择对象" value={entity} onChange={(event) => setEntity(event.target.value)}><option value="">全部对象</option>
        {snapshot?.entities.map((item) => <option key={item.id} value={item.id}>{item.canonical_name} · {item.kind_label}</option>)}</select>
      <button className="tw-button" disabled={!dimension && !query && !entity} onClick={() => { setDimension(0); setQuery(""); setEntity(""); }}>查看全部</button></div>
    {!snapshot ? <div className="rd-empty"><Loader2 className="animate-spin" size={24} /><p>读取数据库…</p></div> : snapshot.records.length === 0 ?
      <div className="rd-empty"><FileText size={28} /><p>{snapshot.counts.records ? "没有匹配的字段记录" : "尚未有字段数据"}</p><span>导入与归档原文对应的字段结果后，会在这里显示。</span></div> :
      <div className="rd-table-wrap"><table className="rd-table"><thead><tr><th>对象</th><th>字段或关系</th><th>内容</th><th>依据</th></tr></thead><tbody>
        {snapshot.records.map((record) => <Fragment key={record.id}><tr><td>{record.entity_name}</td><td>{record.label}<small>{record.status === "accepted" ? "来源记录" : record.status === "withdrawn" ? "已撤回" : "待核对"}</small></td>
          <td>{fieldValue(record.value)}</td><td><button className="rd-evidence-button" aria-expanded={expanded === record.id} onClick={() => setExpanded(expanded === record.id ? null : record.id)}><FileText size={14} />{record.evidence.length} 条依据<ChevronDown size={13} /></button></td></tr>
          {expanded === record.id && <tr><td colSpan={4}><div className="rd-evidence">{record.evidence.map((evidence, index) => <article key={index}><strong>{evidence.title}</strong><blockquote>{evidence.quote}</blockquote>
            <button className="tw-button" onClick={() => void action(async () => setSource(await readFieldSource(evidence.revision_id)))}>查看归档正文</button>
            {/^https?:\/\//i.test(evidence.url) && <a href={evidence.url} target="_blank" rel="noreferrer">访问来源网页</a>}</article>)}
            {Object.keys(record.qualifiers).length > 0 && <details><summary>适用条件与说明</summary><pre>{JSON.stringify(record.qualifiers, null, 2)}</pre></details>}</div></td></tr>}
        </Fragment>)}
      </tbody></table><p className="rd-count">显示 {snapshot.records.length} / {snapshot.matching_records} 条；可按对象或关键词缩小范围。</p></div>}

    <details className="rd-jobs" open={active > 0 || undefined}><summary>文档处理记录 <span>{jobs?.jobs.length ?? 0} 份{active ? ` · ${active} 份处理中` : ""}</span></summary>
      {jobs?.jobs.slice(0, 30).map((job) => <div className="rd-job" key={job.id}><div><strong>{job.title}</strong><p>{job.mode === "existing" ? "已有结果导入" : job.mode === "structured" ? "字段结果入库 · 未调用模型" : `${job.engine} · ${job.model}`} · {topicStatus(job.status)}{job.chunks_total ? ` · ${job.chunks_done}/${job.chunks_total} 个正文分块` : ""} · {job.fields} 条字段 · {job.unmapped_count} 条待映射</p>{job.error && <p className="rd-error">{job.error}</p>}</div>
        <button className="tw-button" onClick={() => void action(async () => setDetail(await readFieldJob(job.id)))}>详情</button>
        {["failed", "partial"].includes(job.status) && <button className="tw-button" disabled={busy || (job.mode !== "existing" && !modelEnabled)} onClick={() => void action(() => retryFieldJob(job.id))}>重试</button>}</div>)}
    </details>
    {(source || detail) && <div className="rd-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) closeDialog(); }}><div ref={dialog} role="dialog" aria-modal="true" aria-label={source ? "归档正文" : "抽取处理详情"} className="rd-dialog" tabIndex={-1}>
      <header><h2>{source?.title ?? detail?.title}</h2><button className="tw-icon" aria-label="关闭详情" onClick={closeDialog}><X size={19} /></button></header>
      {source ? <pre>{source.text_content}</pre> : <div><p>{topicStatus(detail?.status ?? "")}</p>{detail?.error && <p className="rd-error">{detail.error}</p>}<h3>未映射与待核内容</h3>
        {detail?.result?.unmapped?.length ? detail.result.unmapped.map((item, i) => <pre key={i}>{JSON.stringify(item, null, 2)}</pre>) : <p>没有未映射内容。</p>}
        {detail?.result?.notes?.map((note, index) => <p key={index}>{note}</p>)}</div>}
    </div></div>}
  </section>;
}
