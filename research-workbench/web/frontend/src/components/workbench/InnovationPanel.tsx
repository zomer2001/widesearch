"use client";

import { ArrowUpRight, BookOpen, BrainCircuit, Check, Circle, FileCheck2, History, ShieldCheck, Sparkles } from "lucide-react";
import { innovationActivity, innovationSourceHref, RESEARCH_MODULES } from "@/lib/innovations";
import type { EvidenceNode, ResearchModuleConfig, SearchState } from "@/lib/types";

const STATUS = {
  unknown: "设置未记录", disabled: "未启用", enabled: "已启用 · 尚无触发记录", triggered: "已启用 · 已触发",
};
const EXPERIENCES = { active: "可参考", insufficient_feedback: "反馈尚不足", revalidation_required: "需要重新核验" };
const VERDICTS = { compatible: "可以并存", contradictory: "存在冲突", uncertain: "尚不确定" };

export default function InnovationPanel({ state, config, onOpenLocalSource }: {
  state: SearchState | null; config?: ResearchModuleConfig; onOpenLocalSource: (ref: string) => void;
}) {
  const data = innovationActivity(state, config);
  const evidence = state?.evidence_graph?.nodes ?? [];
  const byId = new Map(evidence.map((node) => [node.id, node]));
  const source = (id: string) => <EvidenceSource node={byId.get(id)} onOpen={onOpenLocalSource} />;
  const p1Status = data.status("evidence_revision");
  const p2Status = data.status("context_memory");
  const adaptiveStatus = data.status("adaptive_collection");
  const collection = state?.collection_policy;
  const batches = Object.values(collection?.batches ?? {});
  const goalUpdates = (data.goal?.user_messages ?? []).filter((message) => message !== data.goal?.original_goal);

  return <div className="wb-innovation">
    <div className="wb-innovation-intro"><span><Sparkles size={17} />研究增强记录 · 试验功能</span><small><History size={12} />当前研究累计</small><p>已接入，完整多步流程仍在验证；默认关闭。这里展示保存到研究记录中的实际操作，有对应记录才会显示为已触发。</p></div>
    <section className="wb-enhancement-card">
      <header><span className="wb-enhancement-icon"><Sparkles size={21} /></span><div><h2>自适应采集</h2><p>目标缺口与本轮收获</p></div><StatusBadge status={adaptiveStatus} /></header>
      {adaptiveStatus === "disabled" || adaptiveStatus === "unknown" ? <p className="wb-enhancement-empty">新建任务时可开启自适应采集。</p> : <>
        <div className="wb-enhancement-stats"><Stat value={batches.length} label="已反馈批次" /><Stat value={batches.reduce((sum, item) => sum + item.new, 0)} label="估计新增事实" /><Stat value={batches.reduce((sum, item) => sum + item.corroboration, 0)} label="新增佐证" /><Stat value={collection?.communities.length ?? 0} label="资料分组" /></div>
        {collection?.last_error && <p className="wb-enhancement-note">部分排序已回退至原流程：{collection.last_error}</p>}
        {Object.entries(collection?.goals ?? {}).map(([id, goal]) => <article className="wb-enhancement-entry" key={id}><div className="wb-enhancement-entry-head"><h3>{goal.text}</h3><small className={goal.gap === 0 ? "good" : "pending"}>{goal.gap === null ? "待评估" : goal.gap === 0 ? "已有支持" : "仍有缺口"}</small></div><p>{goal.basis === "table" ? "依据任务目标覆盖状态" : goal.basis === "reader_assessment" ? "依据原文阅读评估" : "等待阅读反馈"}</p></article>)}
        {(collection?.communities.length ?? 0) > 0 && <details className="wb-enhancement-records"><summary>资料分组</summary><div>{collection?.communities.map((group) => <article className="wb-enhancement-entry" key={group.id}><h3>{group.terms.join(" · ")}</h3><p>{group.size} 篇 · 已读 {group.read} 篇</p></article>)}</div></details>}
        {(collection?.decisions.length ?? 0) > 0 && <details className="wb-enhancement-records"><summary>最近的排序记录</summary><div>{collection?.decisions.slice(-12).reverse().map((item, index) => <article className="wb-enhancement-entry" key={`${item.kind}-${item.id}-${index}`}><h3>{item.kind === "task" ? collection.goals[item.id]?.text || item.id : "候选资料"}</h3>{item.kind === "page" && <SourceLink source={item.id} onOpen={onOpenLocalSource} />}<p>排序分数 {item.score}{typeof item.redundancy === "number" ? ` · 词汇重合度 ${Math.round(item.redundancy * 100)}%` : ""}</p></article>)}</div></details>}
        {adaptiveStatus === "enabled" && <p className="wb-enhancement-empty">等待第一批搜索与阅读反馈。</p>}
      </>}
    </section>
    <section className="wb-enhancement-card">
      <header><span className="wb-enhancement-icon"><ShieldCheck size={21} /></span><div><h2>{RESEARCH_MODULES[0].label}</h2><p>查看证据适用范围，以及确认修订后的影响。</p></div><StatusBadge status={p1Status} /></header>
      {p1Status === "disabled" || p1Status === "unknown" ? <p className="wb-enhancement-empty">{p1Status === "disabled" ? "这次研究未启用此模块。新建任务时可以选择开启。" : "这份历史记录没有保存此模块的配置。"}</p> : <>
        <div className="wb-enhancement-stats"><Stat value={data.annotations.length} label="适用性标注" /><Stat value={data.annotations.filter((item) => item.verified).length} label="原文支持核验通过" /><Stat value={data.comparisons.length} label="规则比较" /><Stat value={data.revisions.length} label="确认修订" /></div>
        {p1Status === "enabled" && <p className="wb-enhancement-empty">模块已开启，目前尚未记录适用性标注、规则比较或确认修订。</p>}
        {data.annotations.length > 0 && <details className="wb-enhancement-records" open><summary><FileCheck2 size={14} />证据适用性<span>{data.annotations.length}</span></summary><div>{data.annotations.map((item) => <article key={item.evidence_id} className="wb-enhancement-entry">
          <div className="wb-enhancement-entry-head"><h3>{[item.entity, item.attribute].filter(Boolean).join(" · ") || "证据记录"}</h3><small className={item.verified ? "good" : "pending"}>{item.verified ? "原文支持核验通过" : "待核验"}</small></div>
          {item.value && <p className="wb-enhancement-value">{item.value}</p>}
          {item.scope_key && <p className="wb-enhancement-time">适用口径：{item.scope_key}</p>}
          {item.valid_time && <p className="wb-enhancement-time">适用时间：{item.valid_time.start || "起点未记录"} — {item.valid_time.end || "终点未记录"}</p>}
          {item.valid_time?.basis_quote && <blockquote>{item.valid_time.basis_quote}</blockquote>}
          {item.reason && <p>{item.reason}</p>}{source(item.evidence_id)}
        </article>)}</div></details>}
        {data.comparisons.length > 0 && <details className="wb-enhancement-records"><summary><FileCheck2 size={14} />适用性比较<span>{data.comparisons.length}</span></summary><div>{data.comparisons.map((item, index) => <article key={index} className="wb-enhancement-entry">
          <div className="wb-enhancement-entry-head"><h3>第 {index + 1} 组证据</h3><small className={item.verdict === "compatible" ? "good" : "pending"}>{VERDICTS[item.verdict] || "比较记录"}</small></div>
          {item.reason && <p>{item.reason}</p>}<div className="wb-enhancement-links">{source(item.left_id)}{source(item.right_id)}</div>
        </article>)}</div></details>}
        {data.revisions.length > 0 && <details className="wb-enhancement-records" open><summary><History size={14} />已确认的修订<span>{data.revisions.length}</span></summary><div>{data.revisions.map((item, index) => <article key={item.event.id} className="wb-enhancement-entry">
          <div className="wb-enhancement-entry-head"><h3>第 {index + 1} 次确认修订</h3><small className="good">已记录</small></div><p>{item.event.reason}</p>
          <div className="wb-revision-sources"><span>原证据{source(item.event.old_evidence_id)}</span><span>替代证据{source(item.event.new_evidence_id)}</span></div>
          <p className="wb-enhancement-time">影响 {item.impact.affected_artifact_ids.length} 项内容、{item.impact.affected_task_ids.length} 个任务；保留 {item.impact.unchanged_ids.length} 项独立内容。</p>
        </article>)}</div></details>}
      </>}
    </section>

    <section className="wb-enhancement-card">
      <header><span className="wb-enhancement-icon"><BrainCircuit size={21} /></span><div><h2>{RESEARCH_MODULES[1].label}</h2><p>目标保留、采集经验和上下文整理分别记录。</p></div><StatusBadge status={p2Status} /></header>
      {p2Status === "disabled" || p2Status === "unknown" ? <p className="wb-enhancement-empty">{p2Status === "disabled" ? "这次研究未启用此模块。新建任务时可以选择开启。" : "这份历史记录没有保存此模块的配置。"}</p> : <>
        <div className="wb-enhancement-stats"><Stat value={data.goal?.original_goal ? "已保存" : "待记录"} label="研究目标" /><Stat value={data.experiences.length} label="采集经验" /><Stat value={data.compressed.length} label="完成压缩" /><Stat value={data.rebuilt.length} label="上下文重建" /></div>
        {p2Status === "enabled" && <p className="wb-enhancement-empty">模块已开启，目前尚无目标、经验或上下文整理记录。</p>}
        {data.goal?.original_goal && <div className="wb-goal-record"><span><Check size={13} />已保存的研究目标</span><p>{data.goal.original_goal}</p>{goalUpdates.length > 0 && <details><summary>后续补充 · {goalUpdates.length}</summary><ol>{goalUpdates.map((message, index) => <li key={index}>{message}</li>)}</ol></details>}{data.goal.hard_constraints.length > 0 && <details><summary>明确约束 · {data.goal.hard_constraints.length}</summary><ul>{data.goal.hard_constraints.map((constraint, index) => <li key={index}>{constraint}</li>)}</ul></details>}</div>}
        {data.experiences.length > 0 && <details className="wb-enhancement-records" open><summary><BookOpen size={14} />采集经验<span>{data.experiences.length}</span></summary><p className="wb-enhancement-note">经验卡记录采集过程与已观察到的产出；是否再次采用，以后续实际记录为准。</p><div>{data.experiences.map((item) => <article className="wb-enhancement-entry" key={item.id}>
          <div className="wb-enhancement-entry-head"><h3>{item.question || "采集记录"}</h3><small className={item.status === "active" ? "good" : "pending"}>{EXPERIENCES[item.status] || "已记录"}</small></div>
          <div className="wb-experience-yield"><span>新增证据 <b>{item.feedback.new_evidence_ids === null ? "未记录" : item.feedback.new_evidence_ids.length}</b></span><span>补齐字段 <b>{item.feedback.newly_filled_cells === null ? "未记录" : item.feedback.newly_filled_cells.length}</b></span>{item.feedback.duration_s !== null && <span>用时 <b>{Math.round(item.feedback.duration_s)} 秒</b></span>}</div>
          {item.queries.length > 0 && <details><summary>使用的检索问题 · {item.queries.length}</summary><ul>{item.queries.map((query, index) => <li key={index}>{query}</li>)}</ul></details>}
          <div className="wb-enhancement-links">{Array.from(new Set([...item.sources, ...item.feedback.grounded_evidence_ids.map((id) => byId.get(id)?.source || "")])).filter(Boolean).map((href) => <SourceLink key={href} source={href} label={evidence.find((node) => node.source === href)?.local_source_metadata?.file_name} onOpen={onOpenLocalSource} />)}</div>
          {item.status === "revalidation_required" && <p className="wb-enhancement-note">相关来源、证据或适用范围发生变化，需重新核验后再参考。</p>}
        </article>)}</div></details>}
        {data.metrics.length > 0 && <details className="wb-enhancement-records" open><summary><History size={14} />上下文整理记录<span>{data.metrics.length}</span></summary><div>{data.metrics.map((item, index) => <article key={index} className="wb-enhancement-entry wb-compaction-entry">
          <div className="wb-enhancement-entry-head"><h3>{item.rebuilt ? "上下文重新构建" : "历史上下文压缩"}</h3><small className={item.outcome === "published" ? "good" : "pending"}>{item.outcome === "published" ? "已完成" : item.outcome === "rejected" ? "未发布" : "结果未记录"}</small></div>
          <div className="wb-experience-yield">{typeof item.before_tokens === "number" && <span>整理前 <b>{item.before_tokens.toLocaleString()} tokens</b></span>}{typeof item.after_tokens === "number" && <span>整理后 <b>{item.after_tokens.toLocaleString()} tokens</b></span>}{typeof item.summary_model_calls === "number" && <span>摘要调用 <b>{item.summary_model_calls} 次</b></span>}</div>
          {item.outcome === "rejected" && <p className="wb-enhancement-note">此次整理未发布新的上下文，调用成本仍保留在记录中。</p>}
        </article>)}</div></details>}
        {data.metrics.length === 0 && p2Status === "triggered" && <p className="wb-enhancement-note wb-enhancement-note-bottom">尚未触发需要记录的上下文压缩或重建。</p>}
      </>}
    </section>
  </div>;
}

function StatusBadge({ status }: { status: keyof typeof STATUS }) {
  return <span className={`wb-module-state ${status}`}><Circle size={6} fill="currentColor" />{STATUS[status]}</span>;
}
function Stat({ value, label }: { value: string | number; label: string }) {
  return <div><strong>{value}</strong><small>{label}</small></div>;
}
function EvidenceSource({ node, onOpen }: { node?: EvidenceNode; onOpen: (ref: string) => void }) {
  return node?.source ? <SourceLink source={node.source} label={node.local_source_metadata?.file_name} onOpen={onOpen} /> : <span className="wb-enhancement-note">来源链接未记录</span>;
}
function SourceLink({ source, label, onOpen }: { source: string; label?: string; onOpen: (ref: string) => void }) {
  const href = innovationSourceHref(source);
  if (!href) return <span className="wb-enhancement-note">来源位置未提供可打开链接</span>;
  let title = label || "查看原文";
  if (!label && /^https?:\/\//i.test(href)) { try { title = new URL(href).hostname.replace(/^www\./, ""); } catch { /* Retain the readable fallback. */ } }
  return href.startsWith("local://") ? <button className="wb-enhancement-source" onClick={() => onOpen(href)}><BookOpen size={12} />{title}<ArrowUpRight size={12} /></button>
    : <a className="wb-enhancement-source" href={href} target="_blank" rel="noopener noreferrer"><BookOpen size={12} />{title}<ArrowUpRight size={12} /></a>;
}
