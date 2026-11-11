"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Box, ChevronRight, Cpu, FileText, Loader2, Network, RefreshCw, Search } from "lucide-react";
import { fieldValue, readFieldSource } from "@/lib/researchDatabase";
import { assetDate, groupAssetRecords, readAsset, readAssets, type AssetDetail, type AssetIndex, type AssetKind, type AssetNode, type AssetPane, type Evidence } from "@/lib/researchAssets";
import AssetSourceDrawer, { type OpenedSource } from "./AssetSourceDrawer";
import AssetGraphWorkbench from "./AssetGraphWorkbench";

const labels = { product: "产品", technology: "技术" };
const message = (e: unknown) => e instanceof Error ? e.message : String(e);

export default function KnowledgeAssetsView({ slug, title, initialAsset }: {
  slug: string; title: string; initialAsset?: AssetNode | null;
}) {
  const [kind, setKind] = useState<AssetKind>(initialAsset?.kind === "technology" ? "technology" : "product");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [index, setIndex] = useState<AssetIndex | null>(null);
  const [trail, setTrail] = useState<AssetNode[]>(initialAsset ? [initialAsset] : []);
  const [detail, setDetail] = useState<AssetDetail | null>(null);
  const [pane, setPane] = useState<AssetPane>("fields");
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  const [source, setSource] = useState<OpenedSource | null>(null);
  const sourceSequence = useRef(0);
  const selected = trail.at(-1);
  const selectedId = selected?.id;
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void readAssets(slug, kind, query, offset, controller.signal).then((data) => {
        if (!controller.signal.aborted) { setIndex(data); setError(""); }
      }).catch((e) => { if (!controller.signal.aborted) setError(message(e)); });
    }, 180);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [slug, kind, query, offset, version]);
  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    void readAsset(slug, selectedId, controller.signal).then((data) => {
      if (!controller.signal.aborted) { setDetail(data); setError(""); }
    }).catch((e) => { if (!controller.signal.aborted) setError(message(e)); });
    return () => controller.abort();
  }, [slug, selectedId, version]);
  useEffect(() => () => { sourceSequence.current += 1; }, []);
  const open = (node: AssetNode, nextPane: AssetPane = "fields") => {
    if (!node.can_open) return;
    if (selectedId === node.id) { setPane(nextPane); return; }
    setTrail((old) => old.at(-1)?.id === node.id ? old : [...old, node]);
    setKind(node.kind === "technology" ? "technology" : "product");
    setDetail(null); setPane(nextPane); setError(""); setOffset(0);
  };
  const openEvidence = async (evidence: Evidence) => {
    const sequence = ++sourceSequence.current;
    try {
      const result = await readFieldSource(evidence.revision_id);
      if (sequence === sourceSequence.current) setSource({ ...result, quote: evidence.quote });
    } catch (e) { if (sequence === sourceSequence.current) setError(message(e)); }
  };
  const back = () => { setTrail((old) => old.slice(0, -1)); setDetail(null); setError(""); };

  return <section className="ka-view" aria-label={`${title}知识资产`}>
    {selected ? <>
      <div className="ka-breadcrumb"><button onClick={() => { setTrail([]); setDetail(null); }}>知识资产</button><ChevronRight size={13} /><span>{labels[selected.kind as AssetKind] ?? "对象"}</span>{trail.slice(-3).map((node, i, items) => <span key={`${node.id}-${i}`}><ChevronRight size={13} />{i === items.length - 1 ? node.name : <button onClick={() => { setTrail((v) => v.slice(0, v.length - items.length + i + 1)); setDetail(null); }}>{node.name}</button>}</span>)}</div>
      <header className="ka-detail-header"><div><button className="ka-back" onClick={back}><ArrowLeft size={14} />返回{trail.length > 1 ? "上一个对象" : `${labels[kind]}列表`}</button><h1>{selected.name}<span className={`ka-kind ka-kind-${selected.kind}`}>{labels[selected.kind as AssetKind]}</span></h1>
        <p>{detail ? `${detail.records.filter((r) => r.kind !== "identity").length} 条字段与关系 · ${detail.sources.length} 份来源 · ${detail.related.length} 个关联对象` : "正在读取对象资料…"}</p></div>
        <button className="tw-button" onClick={() => setVersion((v) => v + 1)}><RefreshCw size={14} />刷新资料</button></header>
      <nav className="ka-detail-tabs" aria-label="对象资料视图"><button aria-pressed={pane === "fields"} onClick={() => setPane("fields")}>七类要素</button><button aria-pressed={pane === "graph"} onClick={() => setPane("graph")}><Network size={15} />对象图谱</button><button aria-pressed={pane === "sources"} onClick={() => setPane("sources")}>来源资料 <small>{detail?.sources.length ?? 0}</small></button></nav>
      {error && <p className="ka-error" role="alert">{error}</p>}
      {!detail ? <div className="ka-loading"><Loader2 className="animate-spin" size={24} />{error ? "资料暂时无法读取" : "读取数据库中的对象与字段…"}</div> : pane === "graph" ?
        <AssetGraphPanel key={detail.asset.id} slug={slug} asset={detail.asset} version={version} onOpen={open} onEvidence={openEvidence} /> : pane === "sources" ?
        <div className="ka-source-cards">{detail.sources.map((item) => <article key={item.revision_id}><FileText size={20} /><h3>{item.title}</h3><p>{item.quotes.length} 条原文引句</p><blockquote>{item.quotes[0]}</blockquote><button className="tw-button" onClick={() => void openEvidence(item)}>查看归档全文<ArrowRight size={13} /></button></article>)}{!detail.sources.length && <div className="ka-empty">这个对象尚无已收录的原文依据。</div>}</div> :
        <AssetFields key={detail.asset.id} detail={detail} onOpen={open} onEvidence={openEvidence} />}
    </> : <>
      <header className="ka-header"><div><small>{title} · 知识库</small><h1>知识资产</h1><p>从产品与技术进入，查看字段、来源和它们之间的联系。</p></div><button className="tw-button" aria-label="刷新知识资产" onClick={() => setVersion((v) => v + 1)}><RefreshCw size={15} />刷新</button></header>
      <div className="ka-index-tools"><div className="ka-type-tabs" aria-label="资产分类">{(["product", "technology"] as const).map((value) => <button key={value} aria-pressed={kind === value} onClick={() => { if (kind !== value) { setKind(value); setOffset(0); setIndex(null); } }}>{value === "product" ? <Box size={18} /> : <Cpu size={18} />}<span>{labels[value]}</span><b>{index?.counts[value] ?? "—"}</b></button>)}</div>
        <label className="ka-search"><Search size={17} /><input aria-label="搜索知识资产" placeholder={`搜索${labels[kind]}名称、字段或关联对象`} value={query} onChange={(e) => { setQuery(e.target.value); setOffset(0); }} /></label></div>
      {error && <p className="ka-error" role="alert">{error}</p>}
      {!index ? <div className="ka-loading"><Loader2 className="animate-spin" size={25} />读取{labels[kind]}资产…</div> : <>
        <div className="ka-list-caption"><span>{query ? `找到 ${index.total} 个匹配${labels[kind]}` : `共 ${index.total} 个${labels[kind]}`}</span><small>名称与字段来自数据库，图谱使用已收录关系</small></div>
        <div className="ka-asset-grid">{index.items.map((asset) => <article className={`ka-asset-card ka-card-${kind}`} key={asset.id}>
          <div className="ka-asset-card-top"><span>{kind === "product" ? <Box size={19} /> : <Cpu size={19} />}</span><small>{labels[kind]}</small></div>
          <button className="ka-asset-name" onClick={() => open(asset)}>{asset.name}<ChevronRight size={17} /></button><p>{asset.summary || "进入资料，按七类要素查看已有字段与原文依据。"}</p>
          <div className="ka-card-metrics"><span><b>{asset.field_count}</b> 条字段与关系</span><span><b>{asset.source_count}</b> 份来源</span></div>
          <footer><span>收录更新 {assetDate(asset.updated_at)}</span><button onClick={() => open(asset, "graph")}><Network size={14} />查看图谱</button></footer>
        </article>)}</div>
        {index.total === 0 && <div className="ka-empty"><Box size={27} /><h3>{query ? "没有匹配的资产" : `尚无${labels[kind]}资产`}</h3><p>{query ? "试试其他名称或字段内容。" : "原文中的对象与字段入库后，会在这里按分类显示。"}</p></div>}
        {index.total > index.limit && <div className="ka-pagination"><span>{offset + 1}–{Math.min(offset + index.limit, index.total)} / {index.total}</span><button className="tw-button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - index.limit))}>上一页</button><button className="tw-button" disabled={offset + index.limit >= index.total} onClick={() => setOffset(offset + index.limit)}>下一页</button></div>}
      </>}
    </>}
    {source && <AssetSourceDrawer source={source} onClose={() => { sourceSequence.current += 1; setSource(null); }} />}
  </section>;
}

function AssetFields({ detail, onOpen, onEvidence }: {
  detail: AssetDetail; onOpen: (node: AssetNode, pane?: AssetPane) => void; onEvidence: (e: Evidence) => Promise<void>;
}) {
  const [dimension, setDimension] = useState(detail.asset.kind === "product" ? 7 : 1);
  const [empty, setEmpty] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const active = detail.dimensions.find((d) => d.id === dimension)!;
  const grouped = groupAssetRecords(detail.records);
  const records = grouped.filter((r) => r.dimension_id === dimension);
  const populated = new Set(detail.records.map((r) => r.field_id));
  const unfilled = empty ? detail.fields.filter((f) => f.dimension_id === dimension && !populated.has(f.id)) : [];
  return <div className="ka-fields-layout"><nav className="ka-dimension-nav" aria-label="对象七类要素">{detail.dimensions.map((d) => <button key={d.id} aria-pressed={d.id === dimension} onClick={() => { setDimension(d.id); setExpanded(null); }}><span>{d.id}</span><strong>{d.name}</strong><small>{grouped.filter((r) => r.dimension_id === d.id).length}</small></button>)}</nav>
    <div className="ka-fields-main"><header><div><h2>{active.name}</h2><p>{active.description}</p></div><label className="ka-empty-toggle"><input type="checkbox" checked={empty} onChange={(e) => setEmpty(e.target.checked)} />包含未填写字段</label></header>
      {active.elements.map((element) => {
        const values = records.filter((r) => r.element_id === element.id), blanks = unfilled.filter((f) => f.element_id === element.id);
        if (!values.length && !blanks.length) return null;
        return <section className="ka-field-group" key={element.id}><h3>{element.name}<small>{values.length} 条记录</small></h3>
          {values.map((record) => <div className="ka-field-row" key={record.id}><div className="ka-field-label">{record.label}<small>来源陈述</small></div><div className="ka-field-value"><p>{fieldValue(record.value)}</p>
            {record.linked_entities.filter((n) => n.can_open).map((node) => <button className="ka-linked-asset" key={node.id} onClick={() => onOpen(node)}>{node.kind === "product" ? <Box size={12} /> : <Cpu size={12} />}{node.name}<ChevronRight size={12} /></button>)}
            {Object.entries(record.qualifiers).filter(([k, v]) => ["version", "condition", "date", "scope", "test_conditions"].includes(k) && v).map(([key, value]) => <small className="ka-qualifier" key={key}>{({ version: "版本", condition: "条件", date: "日期", scope: "范围", test_conditions: "测试条件" } as Record<string, string>)[key]}：{fieldValue(value)}</small>)}
            {expanded === record.id && <div className="ka-inline-evidence">{record.evidence.map((e, i) => <article key={i}><strong>{e.title}</strong>{typeof e.source_context?.assertion_type === "string" && <small className="ka-qualifier">{e.source_context.assertion_type}</small>}<blockquote>{e.quote}</blockquote><button className="tw-button" onClick={() => void onEvidence(e)}>查看原文定位</button></article>)}</div>}</div>
            <button className="ka-evidence-count" aria-expanded={expanded === record.id} onClick={() => setExpanded(expanded === record.id ? null : record.id)}><FileText size={13} />{record.evidence.length} 条依据</button></div>)}
          {blanks.map((f) => <div className="ka-field-row ka-unfilled" key={f.id}><div className="ka-field-label">{f.label}</div><div className="ka-field-value">尚未收录</div></div>)}
        </section>;
      })}
      {!records.length && !unfilled.length && <div className="ka-empty"><FileText size={25} /><h3>这一类尚无已收录内容</h3><p>框架包含：{active.elements.map((e) => e.name).join("、")}。</p></div>}
      {detail.related.length > 0 && <section className="ka-related-strip"><h3>关联对象</h3>{detail.related.filter((n) => n.can_open).map((node) => <button key={node.id} className="ka-linked-asset" onClick={() => onOpen(node)}>{node.name}<span>{node.relations.join(" / ")}</span><ChevronRight size={12} /></button>)}</section>}
    </div>
  </div>;
}

function AssetGraphPanel(props: {
  slug: string; asset: AssetNode; version: number; onOpen: (n: AssetNode, pane?: AssetPane) => void;
  onEvidence: (e: Evidence) => Promise<void>;
}) {
  return <AssetGraphWorkbench {...props} />;
}
