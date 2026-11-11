"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, Check, ChevronDown, Download, Filter, Focus, GitBranch, Layers3, Loader2, Maximize2, Minimize2, Minus, MousePointer2, Network, Palette, Play, Plus, RotateCcw, Search, Table2, X } from "lucide-react";
import { fieldValue } from "@/lib/researchDatabase";
import { readAssetGraph, type AssetGraph, type AssetNode, type AssetPane, type Evidence, type GraphMode } from "@/lib/researchAssets";
import { downloadGraph, filterGraph, graphGexf, nodeColor, type GraphColor, type GraphLayout, type GraphSize } from "@/lib/assetGraphView";
import GephiGraphCanvas, { type GraphActions } from "./GephiGraphCanvas";

const message = (e: unknown) => e instanceof Error ? e.message : String(e);
const layoutNames = { "force-atlas2": "ForceAtlas2", "antv-dagre": "分层谱系", circular: "环形布局" };

export default function AssetGraphWorkbench({ slug, asset, version, onOpen, onEvidence }: {
  slug: string; asset: AssetNode; version: number; onOpen: (n: AssetNode, pane?: AssetPane) => void;
  onEvidence: (e: Evidence) => Promise<void>;
}) {
  const [mode, setMode] = useState<GraphMode>("overview"), [scope, setScope] = useState<"asset" | "topic">("asset");
  const [depth, setDepth] = useState(1), [limit, setLimit] = useState(250);
  const [graph, setGraph] = useState<AssetGraph | null>(null), [error, setError] = useState("");
  const [layout, setLayout] = useState<GraphLayout>("force-atlas2"), [size, setSize] = useState<GraphSize>("neighbor_count");
  const [color, setColor] = useState<GraphColor>("kind"), [scale, setScale] = useState(1), [spacing, setSpacing] = useState(1);
  const [labels, setLabels] = useState(true), [edgeLabels, setEdgeLabels] = useState(false), [revision, setRevision] = useState(0);
  const [tab, setTab] = useState<"appearance" | "layout" | "filters">("appearance");
  const [hiddenKinds, setHiddenKinds] = useState<string[]>([]), [hiddenRelations, setHiddenRelations] = useState<string[]>([]), [minimum, setMinimum] = useState(0);
  const [nodeId, setNode] = useState<string | null>(null), [edgeId, setEdge] = useState<string | null>(null), [query, setQuery] = useState("");
  const [table, setTable] = useState(false), [fullscreen, setFullscreen] = useState(false), [exporting, setExporting] = useState(false);
  const actions = useRef<GraphActions | null>(null), root = useRef<HTMLElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    void readAssetGraph(slug, asset.id, mode, depth, controller.signal, { scope, limit }).then((data) => {
      if (!controller.signal.aborted) { setGraph(data); setError(""); setNode(null); setEdge(null); }
    }).catch((e) => { if (!controller.signal.aborted) setError(message(e)); });
    return () => controller.abort();
  }, [slug, asset.id, mode, depth, scope, limit, version]);
  useEffect(() => {
    if (!fullscreen) return;
    const close = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !document.querySelector(".ka-source-drawer")) setFullscreen(false);
    };
    const previous = document.body.style.overflow; document.body.style.overflow = "hidden";
    window.addEventListener("keydown", close);
    return () => { document.body.style.overflow = previous; window.removeEventListener("keydown", close); };
  }, [fullscreen]);
  const kinds = useMemo(() => {
    const values = new Map<string, { kind: string; label: string; count: number }>();
    graph?.nodes.forEach((n) => { const item = values.get(n.kind) ?? { kind: n.kind, label: n.kind_label, count: 0 }; item.count++; values.set(n.kind, item); });
    return [...values.values()].sort((a, b) => b.count - a.count || a.kind.localeCompare(b.kind));
  }, [graph]);
  const relations = useMemo(() => {
    const values = new Map<string, { kind: string; label: string; count: number }>();
    graph?.edges.forEach((e) => { const item = values.get(e.predicate) ?? { kind: e.predicate, label: e.label, count: 0 }; item.count++; values.set(e.predicate, item); });
    return [...values.values()].sort((a, b) => b.count - a.count);
  }, [graph]);
  const legend = useMemo(() => {
    const values = new Map<string, { label: string; color: string; count: number }>();
    graph?.nodes.forEach((n) => {
      const label = color === "kind" ? n.kind_label : color === "namespace" ? n.namespace :
        n.distance === null ? "专题中的其他对象" : n.distance === 0 ? "中心对象" : `关联 ${n.distance} 层`;
      const row = values.get(label) ?? { label, color: nodeColor(n, graph, color), count: 0 };
      row.count++; values.set(label, row);
    });
    return [...values.values()];
  }, [graph, color]);
  const visible = useMemo(() => graph ? filterGraph(graph, kinds.filter((k) => !hiddenKinds.includes(k.kind)).map((k) => k.kind),
    relations.filter((k) => !hiddenRelations.includes(k.kind)).map((k) => k.kind), minimum) : null,
  [graph, kinds, relations, hiddenKinds, hiddenRelations, minimum]);
  const node = visible?.nodes.find((n) => n.id === nodeId), edge = visible?.edges.find((e) => e.id === edgeId);
  const found = useMemo(() => visible?.nodes.filter((n) => n.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())) ?? [], [visible, query]);
  const modes: [GraphMode, string][] = [["overview", "关系网络"], ["lineage", asset.kind === "product" ? "产品谱系" : "技术层级"], ["technology", "技术组成"], ["applications", "产品—技术"], ["capabilities", "能力指标"]];
  const clear = () => { setNode(null); setEdge(null); };
  const chooseNode = (id: string) => { setNode(id); setEdge(null); actions.current?.focus(id); };
  const toggle = (value: string, values: string[], setter: (v: string[]) => void) => setter(values.includes(value) ? values.filter((v) => v !== value) : [...values, value]);
  const exportData = (format: "json" | "gexf") => {
    if (!visible) return;
    downloadGraph(`${asset.name}-${scope}.${format}`, format === "json" ? JSON.stringify(visible, null, 2) : graphGexf(visible), format === "json" ? "application/json" : "application/xml");
    setExporting(false);
  };
  return <section ref={root} className={`gx-workbench${fullscreen ? " gx-fullscreen" : ""}`} aria-label="图谱探索工作台">
    <header className="gx-header"><div className="gx-title"><Network size={17} /><strong>图谱探索</strong><span>{asset.name}</span></div>
      <div className="gx-header-actions"><div className="gx-view-switch"><button aria-pressed={!table} onClick={() => setTable(false)}><Network size={14} />图谱</button><button aria-pressed={table} onClick={() => setTable(true)}><Table2 size={14} />数据表</button></div>
        <div className="gx-export"><button onClick={() => setExporting(!exporting)} aria-expanded={exporting}><Download size={14} />导出<ChevronDown size={12} /></button>{exporting && <div role="menu" aria-label="导出图谱"><button role="menuitem" onClick={() => { actions.current?.exportPng(); setExporting(false); }} disabled={table}>图谱图片 PNG</button><button role="menuitem" onClick={() => exportData("gexf")}>Gephi 文件 GEXF</button><button role="menuitem" onClick={() => exportData("json")}>数据与证据 JSON</button></div>}</div>
        <button aria-label={fullscreen ? "退出图谱全屏" : "全屏查看图谱"} onClick={() => setFullscreen(!fullscreen)}>{fullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}</button></div></header>
    <nav className="gx-modes" aria-label="图谱关系范围">{modes.map(([value, label]) => <button key={value} aria-pressed={mode === value} onClick={() => { if (mode !== value) { setMode(value); setLayout(value === "lineage" ? "antv-dagre" : "force-atlas2"); setEdgeLabels(value === "lineage"); setGraph(null); } }}>{value === "lineage" ? <GitBranch size={14} /> : null}{label}</button>)}<span>已采纳关系 · 原文可追溯</span></nav>
    <div className="gx-body"><aside className="gx-controls" aria-label="图谱显示设置">
      <nav className="gx-control-tabs" aria-label="图谱设置分类">{([["appearance", Palette, "外观"], ["layout", Layers3, "布局"], ["filters", Filter, "筛选"]] as const).map(([value, Icon, text]) => <button key={value} aria-pressed={tab === value} onClick={() => setTab(value)}><Icon size={15} />{text}</button>)}</nav>
      <div className="gx-control-scroll">
        {tab === "appearance" ? <>
          <div className="gx-control-group"><h3>节点大小</h3><label className="gx-setting">映射属性<select aria-label="节点大小依据" value={size} onChange={(e) => setSize(e.target.value as GraphSize)}><option value="neighbor_count">关联对象数</option><option value="source_count">来源数量</option><option value="uniform">统一大小</option></select></label>
            <label className="gx-range">尺寸比例 <output>{scale.toFixed(1)}×</output><input aria-label="节点尺寸比例" type="range" min=".6" max="1.8" step=".1" value={scale} onChange={(e) => setScale(Number(e.target.value))} /></label><p>按完整范围的去重关联数计算，筛选后保持尺寸可比。</p></div>
          <div className="gx-control-group"><h3>节点颜色</h3><label className="gx-setting">分类属性<select aria-label="节点颜色依据" value={color} onChange={(e) => setColor(e.target.value as GraphColor)}><option value="kind">对象类型</option><option value="namespace">所属主体</option><option value="distance">关联层数</option></select></label>
            {legend.map((item) => <div className="gx-legend-row" key={item.label}><i style={{ background: item.color }} /><span>{item.label}</span><small>{item.count}</small></div>)}</div>
          <div className="gx-control-group"><h3>标签与连线</h3><label className="gx-check"><input type="checkbox" checked={labels} onChange={(e) => setLabels(e.target.checked)} />显示对象名称</label><label className="gx-check"><input type="checkbox" checked={edgeLabels} onChange={(e) => setEdgeLabels(e.target.checked)} />显示关系名称</label><p>悬停可突出相邻关系；点击连线查看原文证据。</p></div>
        </> : tab === "layout" ? <>
          <div className="gx-control-group"><h3>布局算法</h3><label className="gx-setting">排列方式<select aria-label="图谱布局算法" value={layout} onChange={(e) => setLayout(e.target.value as GraphLayout)}>{Object.entries(layoutNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><p>{layout === "force-atlas2" ? "ForceAtlas2 展开关系网络，让关联紧密的对象自然聚集。" : layout === "antv-dagre" ? "Dagre 按有向关系排列层级，使用折线和连接点展示谱系。" : "沿圆周排列，便于比较节点与关联。"}</p>
            <label className="gx-range">布局间距<output>{spacing.toFixed(1)}×</output><input aria-label="布局间距" type="range" min=".5" max="2.5" step=".1" value={spacing} onChange={(e) => setSpacing(Number(e.target.value))} /></label><button className="gx-primary" onClick={() => setRevision((v) => v + 1)}><Play size={13} />重新排列</button></div>
          <div className="gx-control-group"><h3>视图操作</h3><button className="gx-wide-button" onClick={() => actions.current?.fit()}><Focus size={14} />适应窗口</button><p>滚轮缩放，拖动画布平移，拖动节点调整位置。</p></div>
          <div className="gx-control-group gx-reference"><h3>开源实现</h3><a href="https://lite.gephi.org/" target="_blank" rel="noreferrer">Gephi Lite · 操作方式 ↗</a><a href="https://github.com/antvis/G6" target="_blank" rel="noreferrer">AntV G6 · 绘制与布局 ↗</a></div>
        </> : <>
          <div className="gx-control-group"><h3>数据范围</h3><label className="gx-setting">查看范围<select aria-label="图谱数据范围" value={scope} onChange={(e) => { setScope(e.target.value as "asset" | "topic"); setGraph(null); }}><option value="asset">当前对象周边</option><option value="topic">整个专题网络</option></select></label>
            {scope === "asset" && <label className="gx-setting">展开层数<select aria-label="图谱展开层数" value={depth} onChange={(e) => { setDepth(Number(e.target.value)); setGraph(null); }}>{[1, 2, 3].map((v) => <option key={v} value={v}>{v} 层</option>)}</select></label>}
            <label className="gx-setting">对象上限<select aria-label="图谱对象上限" value={limit} onChange={(e) => { setLimit(Number(e.target.value)); setGraph(null); }}>{[100, 250, 500].map((v) => <option key={v} value={v}>{v} 个</option>)}</select></label></div>
          <div className="gx-control-group"><h3>对象类型</h3>{kinds.map((kind) => <label key={kind.kind} className="gx-check"><input type="checkbox" aria-label={`显示${kind.label}节点`} checked={!hiddenKinds.includes(kind.kind)} onChange={() => toggle(kind.kind, hiddenKinds, setHiddenKinds)} /><span>{kind.label}</span><small>{kind.count}</small></label>)}<p>中心对象保留。隐藏类型时同时隐藏相连的边。</p></div>
          <div className="gx-control-group"><h3>关系类型</h3>{relations.map((r) => <label key={r.kind} className="gx-check"><input type="checkbox" aria-label={`显示${r.label}关系`} checked={!hiddenRelations.includes(r.kind)} onChange={() => toggle(r.kind, hiddenRelations, setHiddenRelations)} /><span>{r.label}</span><small>{r.count}</small></label>)}</div>
          <div className="gx-control-group"><label className="gx-range">最少关联对象<output>{minimum}</output><input aria-label="最少关联对象" type="range" min="0" max={Math.max(1, ...graph?.nodes.map((n) => n.neighbor_count) ?? [1])} value={minimum} onChange={(e) => setMinimum(Number(e.target.value))} /></label><button className="gx-wide-button" onClick={() => { setHiddenKinds([]); setHiddenRelations([]); setMinimum(0); }}><RotateCcw size={13} />清除筛选</button></div>
        </>}
      </div><footer className="gx-control-foot"><Check size={12} />{layoutNames[layout]} · 真实数据库</footer>
    </aside>
    <main className="gx-stage"><div className="gx-stage-top"><label className="gx-search"><Search size={15} /><input aria-label="搜索图谱对象" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索并定位对象" />{query && <button aria-label="清空图谱搜索" onClick={() => setQuery("")}><X size={12} /></button>}</label><span>{visible?.nodes.length ?? 0} 个节点 <i />{visible?.edges.length ?? 0} 条边</span></div>
      {query && !table && <div className="gx-search-results" role="listbox" aria-label="图谱搜索结果">{found.slice(0, 12).map((n) => <button role="option" aria-selected={nodeId === n.id} key={n.id} onClick={() => { chooseNode(n.id); setQuery(""); }}><span>{n.name}</span><small>{n.kind_label}</small></button>)}{!found.length && <p>当前可见范围没有匹配对象</p>}</div>}
      {error ? <div className="gx-canvas-message gx-error" role="alert">{error}</div> : !graph || !visible ? <div className="gx-canvas-message"><Loader2 size={20} className="animate-spin" />读取数据库中的节点与关系…</div> : table ?
        <div className="gx-data-table"><table><thead><tr><th>对象名称</th><th>类型</th><th>所属主体</th><th>关联对象</th><th>来源</th></tr></thead><tbody>{found.map((n) => <tr key={n.id} aria-selected={nodeId === n.id}><td><button onClick={() => chooseNode(n.id)}>{n.name}</button></td><td>{n.kind_label}</td><td>{n.namespace}</td><td>{n.neighbor_count}</td><td>{n.source_count ?? 0}</td></tr>)}</tbody></table></div> : <GephiGraphCanvas graph={visible} reference={graph} layout={layout} size={size} color={color} scale={scale} labels={labels} edgeLabels={edgeLabels} spacing={spacing} revision={revision} selectedNode={nodeId} selectedEdge={edgeId} onNode={chooseNode} onEdge={(id) => { setEdge(id); setNode(null); }} onClear={clear} actionsRef={actions} />}
      {!table && <div className="gx-canvas-tools"><button aria-label="放大图谱" onClick={() => actions.current?.zoom(1.25)}><Plus size={16} /></button><button aria-label="缩小图谱" onClick={() => actions.current?.zoom(.8)}><Minus size={16} /></button><button aria-label="图谱适应窗口" onClick={() => actions.current?.fit()}><Focus size={16} /></button></div>}
      {!node && !edge && !table && <div className="gx-selection-hint"><MousePointer2 size={13} />点击对象或连线，查看属性与来源</div>}
      {(node || edge) && <aside className="gx-inspector" aria-label="图谱对象与关系详情"><header><span>{edge ? "关系属性" : "对象属性"}</span><button aria-label="关闭图谱详情" onClick={clear}><X size={15} /></button></header>
        {node ? <><div className="gx-object-heading"><i style={{ background: nodeColor(node, graph!, color) }} /><span>{node.kind_label}</span><h3>{node.name}</h3></div><dl><dt>所属主体</dt><dd>{node.namespace}</dd><dt>关联对象</dt><dd>{node.neighbor_count}</dd><dt>原文来源</dt><dd>{node.source_count ?? 0}</dd><dt>图谱位置</dt><dd>{node.id === asset.id ? "当前中心" : node.distance === null ? "专题网络" : `关联 ${node.distance} 层`}</dd></dl>
          {node.can_open && <div className="gx-inspector-actions"><button className="gx-primary" onClick={() => onOpen(node)}>查看七类资料<ArrowRight size={13} /></button>{node.id !== asset.id && <button className="gx-wide-button" onClick={() => onOpen(node, "graph")}>以此为中心查看</button>}</div>}
          <h4>已显示的相邻关系</h4><div className="gx-neighbor-list">{visible?.edges.filter((e) => e.source === node.id || e.target === node.id).map((e) => <button key={e.id} onClick={() => { setNode(null); setEdge(e.id); }}><span>{e.label}</span><strong>{visible.nodes.find((n) => n.id === (e.source === node.id ? e.target : e.source))?.name}</strong><ArrowRight size={12} /></button>)}</div>
        </> : edge && <><div className="gx-object-heading"><span>有向关系</span><h3>{edge.label}</h3></div><p className="gx-edge-endpoints">{visible?.nodes.find((n) => n.id === edge.source)?.name}<ArrowRight size={14} />{visible?.nodes.find((n) => n.id === edge.target)?.name}</p>
          <h4>{edge.claims.length} 条来源陈述</h4>{edge.claims.map((claim) => <article className="gx-evidence" key={claim.id}>{claim.detail && <strong>{fieldValue(claim.detail)}</strong>}{claim.evidence.map((e, i) => <div key={i}><blockquote>{e.quote}</blockquote><small>{e.title}</small><button className="gx-wide-button" onClick={() => void onEvidence(e)}>查看归档原文<ArrowRight size={12} /></button></div>)}</article>)}</>}
      </aside>}
    </main></div>
    <footer className="gx-statusbar"><span><i />{scope === "topic" ? "专题网络" : `${depth} 层关联`} · 显示 {visible?.nodes.length ?? 0} / {graph?.nodes.length ?? 0} 个对象</span><span>{graph?.truncated ? `已达 ${limit} 个对象上限，可在筛选中调整` : "箭头表示真实关系方向"}</span><span>拖动平移 · 滚轮缩放</span></footer>
  </section>;
}
