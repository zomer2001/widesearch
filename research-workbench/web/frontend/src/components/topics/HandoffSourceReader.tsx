"use client";

import { useEffect, useState } from "react";
import { FileText, Loader2, RefreshCw } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { readHandoffDocument, type HandoffDocument } from "@/lib/knowledgeHandoff";

export default function HandoffSourceReader({ slug, documentId }: { slug: string; documentId: string }) {
  const [data, setData] = useState<HandoffDocument | null>(null);
  const [tab, setTab] = useState<"text" | "summary">("text");
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void readHandoffDocument(slug, documentId, controller.signal).then((value) => {
      if (!controller.signal.aborted) { setData(value); setError(""); }
    }).catch((cause) => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause)); });
    return () => controller.abort();
  }, [slug, documentId, version]);
  const summaries = data?.summaries ?? [];
  return <div className="kh-source-reader">
    <div className="kh-reader-toolbar"><nav aria-label="资料内容"><button aria-pressed={tab === "text"} onClick={() => setTab("text")}>正文</button><button aria-pressed={tab === "summary"} onClick={() => setTab("summary")}>摘要{summaries.length ? ` · ${summaries.length}` : ""}</button></nav><button className="tw-icon" aria-label="刷新正文与摘要" onClick={() => setVersion((v) => v + 1)}><RefreshCw size={14} /></button></div>
    {error && <p className="rd-error" role="alert">{error}</p>}
    {!data ? <div className="ka-loading"><Loader2 size={22} className="animate-spin" />{error ? "资料暂不可用" : "读取资料…"}</div> : tab === "text" ?
      <article className="ka-original-content"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ children, href }) => <a href={href} target="_blank" rel="noreferrer">{children}</a> }}>{data.text}</ReactMarkdown></article> :
      <div className="kh-summaries">{summaries.length === 0 ? <div className="ka-empty"><FileText size={25} /><h3>尚未保存摘要</h3><p>摘要与正文分开保存，提交抽取结果中的摘要后可在这里查看。</p></div> : summaries.map((summary) => <article className="kh-summary" key={summary.id}>
        <header><span>{summary.kind === "extractive" ? "事实摘录" : "阅读摘要"}</span><small>{summary.current ? "对应当前正文" : "对应历史正文"}{summary.created_at ? ` · ${new Date(summary.created_at).toLocaleString("zh-CN")}` : ""}</small></header>
        <div className="ka-original-content"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ children, href }) => <a href={href} target="_blank" rel="noreferrer">{children}</a> }}>{summary.text}</ReactMarkdown></div>
        <details><summary>查看依据</summary>{(summary.evidence?.map((item) => item.quote) ?? summary.quotes ?? []).map((quote, index) => <blockquote key={index}>{quote}</blockquote>)}<p>正文版本：{summary.source_revision.slice(0, 12)}</p></details>
      </article>)}</div>}
  </div>;
}
