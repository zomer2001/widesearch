"use client";

import { useState } from "react";
import { ArrowRight, Check, CircleAlert, FileText, Loader2, RefreshCw, Workflow } from "lucide-react";
import { INGESTION_LABELS, activeIngestion, matchesIngestion, type IngestionFilter, type KnowledgeIngestionSnapshot } from "@/lib/knowledgeDatabase";

interface Props {
  data: KnowledgeIngestionSnapshot | null;
  error: string | null;
  actionError: string | null;
  action: string | null;
  notice: string | null;
  query: string;
  onRefresh: () => void;
  onQueueLatest: () => void;
  onRetry: (id: string) => void;
}
function date(value: string): string { const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? "时间未记录" : parsed.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }); }

export default function KnowledgeIngestionView({ data, error, actionError, action, notice, query, onRefresh, onQueueLatest, onRetry }: Props) {
  const [filter, setFilter] = useState<IngestionFilter>("all");
  const jobs = data?.jobs.filter((job) => matchesIngestion(job, query, filter)) ?? [];
  return <section className="kd-ingestion-view" aria-label="自动入库进度">
    <header className="kd-ingestion-heading"><div><h2><Workflow size={20} />从原文到知识图谱</h2><p>新的网页与混合研究结束后，已归档原文会自动排队抽取。研究结束后，入库处理可能仍在继续。</p></div><button className="kd-button" onClick={onRefresh}><RefreshCw size={14} />刷新进度</button></header>
    <div className="kd-ingestion-flow"><span><FileText size={16} />原文归档</span><ArrowRight size={14} /><span>抽取实体与关系</span><ArrowRight size={14} /><span>核对引句</span><ArrowRight size={14} /><span>入库并更新图谱</span></div>
    {error && <div className="kd-message kd-error" role="alert"><CircleAlert size={16} /><span>{error}</span><button onClick={onRefresh}>重新读取进度</button></div>}
    {actionError && <div className="kd-message kd-error" role="alert"><CircleAlert size={16} /><span>{actionError}</span></div>}
    {notice && <div className="kd-message kd-success" role="status"><Check size={15} /><span>{notice}</span></div>}
    {data ? <>
      <div className="kd-ingestion-stats" aria-label="入库任务统计">{(["queued", "running", "completed", "partial", "failed", "skipped"] as const).map((status) => <span key={status} className={`kd-ingestion-count kd-ingestion-${status}`}><strong>{data.counts[status]}</strong>{INGESTION_LABELS[status]}</span>)}</div>
      {activeIngestion(data) > 0 && <p className="kd-ingestion-live" role="status"><Loader2 size={14} className="animate-spin" />正在处理 {activeIngestion(data)} 份资料。此页每 5 秒更新，图谱会同步读取已入库内容。</p>}
      <div className="kd-ingestion-list-heading"><h3>资料处理记录 <span>{jobs.length}</span></h3><select aria-label="筛选入库状态" value={filter} onChange={(event) => setFilter(event.target.value as IngestionFilter)}><option value="all">全部状态</option><option value="active">进行中</option><option value="completed">已入库</option><option value="attention">需关注</option></select></div>
      {jobs.length ? <div className="kd-ingestion-list">{jobs.map((job) => <article className={`kd-ingestion-job kd-ingestion-${job.status}`} key={job.id}><div className="kd-ingestion-job-heading"><FileText size={17} /><h3>{job.title || "归档网页"}</h3><span className="kd-ingestion-state">{job.status === "running" && <Loader2 size={13} className="animate-spin" />}{INGESTION_LABELS[job.status] || "状态待确认"}</span></div><div className="kd-ingestion-job-body"><p>{job.message || (job.status === "queued" ? "已登记，等待前面的资料处理完成。" : job.status === "running" ? "正在从原文提取可追溯的实体和关系。" : job.status === "completed" ? "已通过原文校验并写入数据库。" : "查看详情了解处理范围。")}</p><div className="kd-ingestion-job-counts"><span><strong>{job.entities}</strong> 个实体</span><span><strong>{job.relations}</strong> 条关系</span><time>{date(job.updated_at)}</time></div>{job.status === "partial" && <p className="kd-ingestion-caveat">仅已核对的内容入库，剩余范围或未通过校验的内容仍待补充。</p>}{job.error && <p className="kd-ingestion-job-error">{job.error}</p>}<details><summary>查看任务与处理信息</summary><dl><div><dt>所属研究任务</dt><dd>{job.session_id || "未记录"}</dd></div><div><dt>抽取模型</dt><dd>{job.model || job.engine || "未记录"}</dd></div><div><dt>登记时间</dt><dd>{date(job.created_at)}</dd></div>{typeof job.model_calls === "number" && <div><dt>本次模型请求</dt><dd>{job.model_calls} 次</dd></div>}</dl></details>{["failed", "partial"].includes(job.status) && <button className="kd-button" disabled={!!action || !data.enabled} title="显式重新抽取这份原文；可能再次调用模型并产生 API 费用" onClick={() => onRetry(job.id)}>{action === job.id ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}重新抽取</button>}</div></article>)}</div> : <div className="kd-empty kd-ingestion-empty"><Workflow size={25} /><p>{query || filter !== "all" ? "当前条件下没有处理记录。" : "还没有自动入库任务。"}</p><span>{query || filter !== "all" ? "更换状态或清除搜索查看其他资料。" : "发起网页研究后，系统会从实际保存的原文中自动抽取。"}</span></div>}
    </> : !error && <div className="kd-empty"><Loader2 size={22} className="animate-spin" /><p>正在读取自动入库进度…</p></div>}
    <section className="kd-ingestion-backfill"><div><h3>补抽取最近任务</h3><p>仅处理最近一次研究已归档的原文，不重新搜索。会使用当前研究模型；API 模型可能产生费用。已处理的相同原文版本会复用记录。</p><p>不会在打开页面时自动补抽取历史资料。本地资料问答不会自动上传文件进行抽取。</p></div><button className="kd-button kd-button-primary" disabled={!!action || !data?.enabled} onClick={onQueueLatest}>{action === "latest" ? <Loader2 size={14} className="animate-spin" /> : <Workflow size={14} />}补抽取最近任务</button></section>
  </section>;
}
