"use client";

import { useEffect, useState } from "react";
import { ArrowLeft, ArrowRight, BookOpen, ExternalLink, FileText, Loader2, RefreshCw, Search } from "lucide-react";
import HandoffSourceReader from "./HandoffSourceReader";
import { readMaterials, type AssetNode, type Material, type MaterialIndex } from "@/lib/researchAssets";

export default function RawMaterialsView({ slug, title, onOpenAsset }: {
  slug: string; title: string; onOpenAsset: (node: AssetNode) => void;
}) {
  const [data, setData] = useState<MaterialIndex | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Material | null>(null);
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void readMaterials(slug, controller.signal).then((value) => {
      if (!controller.signal.aborted) { setData(value); setError(""); }
    }).catch((e) => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : String(e)); });
    return () => controller.abort();
  }, [slug, version]);
  const q = query.trim().toLocaleLowerCase();
  const documents = (data?.documents ?? []).filter((d) => (!q || [d.name, d.url, ...d.assets.map((a) => a.name)].some((value) => value.toLocaleLowerCase().includes(q)))
    && (filter === "all" || (filter === "mapped" ? d.field_count > 0 : d.field_count === 0)));
  const open = (d: Material) => { setSelected(d); setError(""); };
  return <section className="ka-view" aria-label={`${title}原文资料`}>
    {selected ? <>
      <button className="ka-back" onClick={() => { setSelected(null); setError(""); }}><ArrowLeft size={14} />返回原文资料</button>
      <header className="ka-material-header"><small>{title} · 归档正文</small><h1>{selected.name}</h1><div><span className="ka-status">{selected.parse_status === "ready" ? "已解析" : selected.parse_status === "failed" ? "解析失败" : "等待解析"}</span><span>{selected.field_count ? `${selected.field_count} 条字段与关系 · ${selected.assets.length} 个关联资产` : "尚无字段入库"}</span>{/^https?:\/\//i.test(selected.url) && <a href={selected.url} target="_blank" rel="noreferrer"><ExternalLink size={13} />访问来源网页</a>}</div></header>
      {error && <p className="ka-error" role="alert">{error}</p>}
      <div className="ka-material-layout"><HandoffSourceReader key={selected.document_id} slug={slug} documentId={selected.document_id} />
        <aside className="ka-material-assets"><h2>关联知识资产</h2><p>同一来源已支撑的产品与技术，含已有历史版本。</p>{selected.assets.map((node) => <button key={node.id} onClick={() => onOpenAsset(node)}><span>{node.kind === "product" ? "产品" : "技术"}</span><strong>{node.name}</strong><ArrowRight size={14} /></button>)}{!selected.assets.length && <div className="ka-empty"><BookOpen size={23} /><p>尚无关联资产。正文经字段映射入库后，这里会自动关联。</p></div>}<details><summary>归档与版本</summary><p>版本：{selected.source_revision.slice(0, 12)}</p><p>已入库来源版本：{selected.revision_count}</p></details></aside>
      </div>
    </> : <>
      <header className="ka-header"><div><small>{title} · 资料库</small><h1>原文资料</h1><p>保存抓取正文与版本，查看每份资料支撑了哪些产品和技术。</p></div><button className="tw-button" aria-label="刷新原文资料" onClick={() => setVersion((v) => v + 1)}><RefreshCw size={15} />刷新</button></header>
      <div className="ka-material-stats"><span><FileText size={17} /><b>{data?.total ?? "—"}</b> 份归档资料</span><span><BookOpen size={17} /><b>{data?.mapped ?? "—"}</b> 份已形成字段记录</span></div>
      <div className="ka-index-tools"><label className="ka-search"><Search size={17} /><input aria-label="搜索原文资料" placeholder="搜索标题、来源网址或关联对象" value={query} onChange={(e) => { setQuery(e.target.value); setPage(0); }} /></label><select className="ka-filter-select" aria-label="原文入库状态" value={filter} onChange={(e) => { setFilter(e.target.value); setPage(0); }}><option value="all">全部资料</option><option value="mapped">已有字段入库</option><option value="pending">尚无字段入库</option></select></div>
      {error && <p className="ka-error" role="alert">{error}</p>}
      {!data ? <div className="ka-loading"><Loader2 className="animate-spin" />读取归档资料…</div> : documents.length === 0 ? <div className="ka-empty"><FileText size={28} /><h3>{query || filter !== "all" ? "没有匹配的资料" : "尚无归档资料"}</h3><p>当前专题抓取并保存的正文会显示在这里。</p></div> : <>
        <div className="ka-material-table-wrap"><table className="ka-material-table"><thead><tr><th>归档资料</th><th>解析与入库</th><th>关联资产</th><th /></tr></thead><tbody>{documents.slice(page * 20, page * 20 + 20).map((d) => <tr key={d.document_id}>
          <td><button className="ka-document-name" onClick={() => open(d)}><FileText size={17} /><span>{d.name}</span></button><small>{d.url ? (() => { try { return new URL(d.url).hostname; } catch { return d.url; } })() : "归档来源"}</small></td>
          <td><span className={`ka-status ${d.parse_status === "failed" ? "is-failed" : ""}`}>{d.parse_status === "ready" ? "已解析" : d.parse_status === "failed" ? "解析失败" : "等待解析"}</span><small>{d.field_count ? `${d.field_count} 条字段与关系` : "尚无字段入库"}</small></td>
          <td><div className="ka-material-linked">{d.assets.slice(0, 2).map((node) => <button key={node.id} onClick={() => onOpenAsset(node)}>{node.name}</button>)}{d.assets.length > 2 && <span>+{d.assets.length - 2}</span>}{!d.assets.length && <span>尚无关联</span>}</div></td>
          <td><button className="tw-icon" aria-label={`查看原文：${d.name}`} onClick={() => open(d)}><ArrowRight size={17} /></button></td>
        </tr>)}</tbody></table></div>
        {documents.length > 20 && <div className="ka-pagination"><span>{page * 20 + 1}–{Math.min((page + 1) * 20, documents.length)} / {documents.length}</span><button className="tw-button" disabled={page === 0} onClick={() => setPage((v) => v - 1)}>上一页</button><button className="tw-button" disabled={(page + 1) * 20 >= documents.length} onClick={() => setPage((v) => v + 1)}>下一页</button></div>}
      </>}
    </>}
  </section>;
}
