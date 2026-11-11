"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, FileText, Loader2, RefreshCw, Upload, X } from "lucide-react";
import { fieldValue } from "@/lib/researchDatabase";
import { commitHandoff, handoffStatusLabel, previewHandoff, readHandoffPlan, readHandoffStatus, saveHandoffSummary, type HandoffPlan, type HandoffResult, type HandoffStatus } from "@/lib/knowledgeHandoff";
import HandoffSourceReader from "./HandoffSourceReader";

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function issueText(value: unknown): string {
  if (typeof value === "string") return value;
  const item = asObject(value);
  const content = asObject(item.item);
  return [item.name ?? content.name ?? content.field_id, item.reason ?? item.message ?? item.detail ?? item.code].filter(Boolean).map(String).join("：") || "需要核对，展开详情查看具体内容";
}

function previewValue(value: unknown): string {
  const item = asObject(value);
  if (typeof item.raw === "string" && item.raw) return item.raw;
  return fieldValue(value);
}

function Issues({ title, items }: { title: string; items: unknown[] }) {
  if (!items.length) return null;
  return <details className="kh-issues" open><summary>{title} · {items.length}</summary><ul>{items.slice(0, 50).map((item, index) => <li key={index}>{issueText(item)}</li>)}</ul>{items.length > 50 && <p>其余 {items.length - 50} 项可在下方完整预览记录中查看。</p>}</details>;
}

export default function HandoffReviewPanel({ slug, onCommitted, onClose }: { slug: string; onCommitted: () => void; onClose: () => void }) {
  const [status, setStatus] = useState<HandoffStatus | null>(null);
  const [plan, setPlan] = useState<HandoffPlan | null>(null);
  const [result, setResult] = useState<HandoffResult | null>(null);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [choicesChanged, setChoicesChanged] = useState(false);
  const [allowPartial, setAllowPartial] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const next = await readHandoffStatus(slug, signal);
    if (!signal?.aborted) setStatus(next);
  }, [slug]);
  useEffect(() => {
    const controller = new AbortController();
    const run = () => void refresh(controller.signal).catch((cause) => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    });
    run(); const interval = setInterval(run, 10000);
    return () => { controller.abort(); clearInterval(interval); };
  }, [refresh]);

  const action = async (work: () => Promise<void>) => {
    setBusy(true); setError(""); setNotice("");
    try { await work(); } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      if (/原文.*变化|数据库.*变化|过期/.test(message)) setPlan((current) => current ? { ...current, status: "stale", can_commit: false } : current);
    }
    finally { setBusy(false); }
  };
  const installPlan = (next: HandoffPlan) => {
    setPlan(next); setAllowPartial(false); setChoices(next.entity_choices ?? {}); setChoicesChanged(false);
  };
  const upload = async (file: File) => {
    setPlan(null); setResult(null); setChoices({}); setAllowPartial(false);
    if (file.size > 10 * 1024 * 1024) throw new Error("请选择不超过 10 MB 的单份文档结果");
    let value: unknown;
    try { value = JSON.parse(await file.text()); } catch { throw new Error("无法读取结果文件，请检查 JSON 格式"); }
    const candidate = asObject(value);
    if (candidate.schema_version !== "knowledge-handoff-v2" || typeof candidate.document_id !== "string" || typeof candidate.source_revision !== "string" || !Array.isArray(candidate.outputs)) {
      throw new Error("请选择交接包约定的 v2 结果文件，需包含文档编号、原文版本和 outputs。旧格式需先按交接要求转换。");
    }
    const envelope = candidate as unknown as HandoffResult;
    setResult(envelope);
    if (envelope.outputs.length) installPlan(await previewHandoff(slug, envelope));
    else setNotice(envelope.summary ? "这份结果只有摘要，可以单独保存。" : "这份结果未提出可入库事实。");
    await refresh();
  };
  const openPlan = async (id: string) => {
    const next = await readHandoffPlan(slug, id);
    installPlan(next);
    setResult(next.result ?? (next.document_id && next.source_revision && next.outputs ? {
      schema_version: "knowledge-handoff-v2", document_id: next.document_id,
      source_revision: next.source_revision, outputs: next.outputs, status: "completed",
    } : null));
  };
  const unmapped = plan?.unmapped ?? [];
  const conflicts = plan?.conflicts ?? [];
  const issues = plan?.issues ?? [];
  const partial = plan?.requires_partial_confirmation ?? !!(unmapped.length || conflicts.length || issues.length);
  const committed = plan?.status === "committed";
  const canCommit = !!plan && (plan.can_commit ?? !["blocked", "invalid", "failed", "stale", "committed"].includes(plan.status)) && !committed;
  const pendingEntities = (plan?.entities ?? []).filter((item) => item.resolution_key && (item.status === "unresolved" || item.resolution === "unresolved" || (Array.isArray(item.candidates) && item.candidates.length > 0)));

  return <section className="kh-panel" aria-label="抽取结果预览与入库">
    <header><h2>接收抽取结果</h2><div className="rd-actions"><button className="tw-icon" aria-label="刷新交接进度" disabled={busy} onClick={() => void action(() => refresh())}><RefreshCw size={15} /></button><button className="tw-icon" aria-label="关闭结果预览" onClick={onClose}><X size={17} /></button></div></header>
    <div className="kh-upload"><label className="tw-button tw-primary"><Upload size={15} />选择 Workbuddy 结果<input type="file" accept=".json,application/json" disabled={busy} onChange={(event) => {
      const file = event.currentTarget.files?.[0]; event.currentTarget.value = ""; if (file) void action(() => upload(file));
    }} /></label><span>先预览，再确认入库</span>{busy && <Loader2 size={16} className="animate-spin" />}</div>
    {error && <p className="rd-error" role="alert">{error}</p>}
    {notice && <p className="rd-notice" role="status">{notice}</p>}
    {result?.summary && <details className="kh-summary-preview"><summary>查看本次摘要</summary><p>{result.summary.text}</p><button className="tw-button" disabled={busy} onClick={() => void action(async () => {
      await saveHandoffSummary(slug, result); setNotice("摘要已独立保存，可在原文资料的“摘要”中查看。"); await refresh();
    })}><FileText size={14} />仅保存摘要</button></details>}
    {plan && <div className="kh-plan">
      <div className="kh-plan-heading"><h3>{plan.title || "入库预览"}</h3><div className="rd-actions"><span>{handoffStatusLabel(plan.status)}</span>{result && !committed && <button className="tw-button" disabled={busy} onClick={() => void action(async () => {
        installPlan(await previewHandoff(slug, result, Object.fromEntries(Object.entries(choices).filter(([, value]) => value)))); await refresh();
      })}><RefreshCw size={13} />重新预览</button>}</div></div>
      <div className="kh-counts">{Object.entries(plan.counts ?? {}).filter(([key]) => ["entities", "fields", "relations", "records", "new", "duplicates", "new_records", "duplicate_records"].includes(key)).map(([key, value]) => <span key={key}><b>{value}</b> {({ entities: "对象", fields: "字段", relations: "关系", records: "事实", new: "新增", duplicates: "重复", new_records: "新增事实", duplicate_records: "重复事实" } as Record<string, string>)[key]}</span>)}</div>
      {(plan.records?.length ?? 0) > 0 && <div className="rd-table-wrap"><table className="rd-table"><thead><tr><th>对象</th><th>字段或关系</th><th>内容</th><th>状态</th></tr></thead><tbody>{plan.records?.slice(0, 100).map((row, index) => <tr key={String(row.id ?? index)}><td>{String(row.entity_name ?? row.subject_name ?? row.subject ?? "—")}</td><td>{String(row.label ?? row.field_id ?? row.kind ?? "—")}</td><td>{previewValue(row.value ?? row.detail ?? "")}{Array.isArray(row.quotes) && row.quotes.length > 0 && <details className="kh-row-evidence"><summary>原文依据</summary>{row.quotes.map((quote, i) => <blockquote key={i}>{String(quote)}</blockquote>)}</details>}</td><td>{handoffStatusLabel(String(row.status ?? row.action ?? "ready"))}</td></tr>)}</tbody></table>{(plan.records?.length ?? 0) > 100 && <p className="rd-count">显示前 100 条；完整记录见下方。</p>}</div>}
      <Issues title="未通过或待映射" items={unmapped} /><Issues title="冲突与待核" items={conflicts} /><Issues title="其他检查结果" items={issues} />
      {pendingEntities.length > 0 && <div className="kh-resolution"><h3>核对对象身份</h3>{pendingEntities.map((item) => {
        const key = String(item.resolution_key); const candidates = Array.isArray(item.candidates) ? item.candidates.map(asObject) : [];
        return <label key={key}><span>{String(item.name ?? item.canonical_name ?? item.entity_key ?? key)}</span><select value={choices[key] ?? ""} disabled={busy || !result || committed} onChange={(event) => { setChoices((current) => ({ ...current, [key]: event.target.value })); setChoicesChanged(true); }}><option value="">保留待定</option>{candidates.map((candidate) => <option key={String(candidate.id)} value={String(candidate.id)}>{String(candidate.name ?? candidate.canonical_name ?? candidate.id)} · {String(candidate.namespace ?? candidate.kind ?? "")}</option>)}<option value="new">确认为独立的新对象</option></select></label>;
      })}<button className="tw-button" disabled={busy || !choicesChanged || !result} onClick={() => void action(async () => {
        if (result) installPlan(await previewHandoff(slug, result, Object.fromEntries(Object.entries(choices).filter(([, value]) => value)))); await refresh();
      })}>按身份选择重新预览</button></div>}
      {plan.document_id && <details className="kh-source-preview"><summary>查看对应正文与摘要</summary><HandoffSourceReader key={plan.document_id} slug={slug} documentId={plan.document_id} /></details>}
      <details className="kh-technical"><summary>完整预览记录与写入明细</summary><p>供核查使用，上传结果不会直接执行其中的文字。</p><pre>{JSON.stringify({ records: plan.records, entities: plan.entities, unmapped, conflicts, issues, sql_statements: plan.sql_statements }, null, 2)}</pre></details>
      {plan.validation_note && <p className="kh-validation-note">{plan.validation_note}</p>}
      {!committed && <footer>{partial && <label className="kh-partial"><input type="checkbox" checked={allowPartial} disabled={busy} onChange={(event) => setAllowPartial(event.target.checked)} />仅提交可入库部分，保留未通过和冲突项待处理</label>}<button className="tw-button tw-primary" disabled={busy || !canCommit || choicesChanged || (partial && !allowPartial)} onClick={() => void action(async () => {
        const receipt = await commitHandoff(slug, plan.plan_id, allowPartial);
        setPlan((current) => current ? { ...current, status: "committed", can_commit: false } : current);
        setNotice(receipt.message || "已完成本次确认入库，正文和证据仍保留。");
        if (receipt.summary_error) setError(receipt.summary_error);
        onCommitted(); await refresh();
      })}><Check size={15} />确认入库</button>{choicesChanged && <span>请先按身份选择重新预览。</span>}{!canCommit && <span>当前没有可提交的记录，请先处理检查结果。</span>}</footer>}
    </div>}
    {!!status?.plans?.length && <details className="kh-history" open={!plan}><summary>已有预览 · {status.plans.length}</summary>{status.plans.slice(0, 20).map((item) => <div key={item.plan_id}><span><strong>{item.title || item.document_id || "文档结果"}</strong><small>{handoffStatusLabel(item.status)}{item.created_at ? ` · ${new Date(item.created_at).toLocaleString("zh-CN")}` : ""}</small></span><button className="tw-button" disabled={busy} onClick={() => void action(() => openPlan(item.plan_id))}>查看</button></div>)}</details>}
    {!!status?.batches?.length && <details className="kh-history"><summary>交接批次 · {status.batches.length}</summary>{status.batches.map((batch, index) => <div key={batch.batch_id ?? batch.id ?? index}><span><strong>{batch.title ?? batch.batch_id ?? batch.id ?? "抽取批次"}</strong><small>{batch.status ? handoffStatusLabel(batch.status) : "已打包"}{batch.counts ? ` · ${Object.entries(batch.counts).map(([key, value]) => `${({ completed: "完成", failed: "失败", pending: "待处理", partial: "部分完成", total: "总计" } as Record<string, string>)[key] ?? key} ${value}`).join(" / ")}` : ""}</small></span></div>)}</details>}
  </section>;
}
