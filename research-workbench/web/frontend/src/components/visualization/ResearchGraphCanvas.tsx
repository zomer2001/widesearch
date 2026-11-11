"use client";

import { useEffect, useId, useMemo, useRef, useState, type CSSProperties } from "react";
import { ArrowUpRight, ChevronDown, Expand, Focus, GitBranch, Layers3, Maximize2, Minus, Plus, ScanLine, X } from "lucide-react";
import type { SearchState, WSEvent } from "@/lib/types";
import {
  buildResearchGraph, layoutResearchGraph, researchOperationLabel, NODE_LABELS, STATUS_LABELS,
  type PositionedResearchNode, type ResearchGraphMode, type ResearchGraphNode,
} from "@/lib/researchGraph";

export interface ResearchGraphCanvasProps {
  state: SearchState | null;
  events: WSEvent[];
  answer?: string;
  status?: string;
  mode?: ResearchGraphMode;
  onOpenLocalSource?: (refId: string) => void;
}

const TONES: Record<string, string> = {
  question: "#d2e4bc", task: "#9ddac4", agent: "#a7bfdc", operation: "#b4c7ce",
  source: "#b8c9e8", evidence: "#a5d9b4", reading: "#93c9cd", cell: "#d6c792", answer: "#d2e4bc", group: "#9fbeb6",
};
function short(value: string, length = 21): string { return value.length > length ? `${value.slice(0, length)}…` : value; }
function lines(value: string): string[] {
  const normalized = value.replace(/\s+/g, " ");
  const width = /[\u3400-\u9fff]/.test(normalized) ? 14 : 25;
  return normalized.length > width ? [normalized.slice(0, width), short(normalized.slice(width), width)] : [normalized];
}
function curve(a: PositionedResearchNode, b: PositionedResearchNode, narrow = false): string {
  if (narrow && a.column !== b.column) {
    const x1 = a.x + a.width / 2, x2 = b.x + b.width / 2;
    const y1 = a.y < b.y ? a.y + a.height : a.y;
    const y2 = a.y < b.y ? b.y : b.y + b.height;
    const middle = (y1 + y2) / 2;
    if (Math.abs(y2 - y1) > 170) {
      const side = a.column % 2 === 0 ? 20 : 340;
      const direction = y2 > y1 ? 1 : -1;
      return `M ${x1} ${y1} C ${x1} ${y1 + 20 * direction}, ${side} ${y1 + 20 * direction}, ${side} ${y1 + 40 * direction} L ${side} ${y2 - 35 * direction} C ${side} ${y2 - 15 * direction}, ${x2} ${y2 - 15 * direction}, ${x2} ${y2}`;
    }
    return `M ${x1} ${y1} C ${x1} ${middle}, ${x2} ${middle}, ${x2} ${y2}`;
  }
  const ax = a.x + a.width, ay = a.y + a.height / 2, by = b.y + b.height / 2;
  if (a.column === b.column) {
    const control = Math.max(ax, b.x + b.width) + (narrow ? 25 : 42);
    return `M ${ax} ${ay} C ${control} ${ay}, ${control} ${by}, ${b.x + b.width} ${by}`;
  }
  if (a.x > b.x) {
    const x1 = a.x, x2 = b.x + b.width, mx = (x1 + x2) / 2;
    return `M ${x1} ${ay} C ${mx} ${ay}, ${mx} ${by}, ${x2} ${by}`;
  }
  const mx = (ax + b.x) / 2;
  return `M ${ax} ${ay} C ${mx} ${ay}, ${mx} ${by}, ${b.x} ${by}`;
}

export default function ResearchGraphCanvas({ state, events, answer = "", status = "idle", mode: initialMode = "tasks", onOpenLocalSource }: ResearchGraphCanvasProps) {
  const [mode, setMode] = useState<ResearchGraphMode>(initialMode);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [fullscreen, setFullscreen] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [timelineOpen, setTimelineOpen] = useState(false);
  const [viewportWidth, setViewportWidth] = useState(0);
  const root = useRef<HTMLElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; ox: number; oy: number; width: number; height: number } | null>(null);
  const marker = useId().replace(/:/g, "");
  const graph = useMemo(() => buildResearchGraph(state, events, answer, mode, status), [state, events, answer, mode, status]);
  const narrow = viewportWidth < 640;
  const layout = useMemo(() => layoutResearchGraph(graph, expanded, { narrow }), [graph, expanded, narrow]);
  const indexed = useMemo(() => new Map(layout.nodes.map((node) => [node.id, node])), [layout.nodes]);
  const selected = layout.nodes.find((node) => node.id === selectedId) || graph.nodes.find((node) => node.id === selectedId);
  const neighborhood = useMemo(() => {
    if (!selectedId) return null;
    const ids = new Set([selectedId]);
    layout.edges.forEach((edge) => { if (edge.source === selectedId) ids.add(edge.target); if (edge.target === selectedId) ids.add(edge.source); });
    return ids;
  }, [selectedId, layout.edges]);
  const viewWidth = narrow ? layout.width : layout.width / zoom, viewHeight = narrow ? layout.height : layout.height / zoom;
  const viewX = narrow ? 0 : (layout.width - viewWidth) / 2 + pan.x;
  const viewY = narrow ? 0 : (layout.height - viewHeight) / 2 + pan.y;
  const columnNames = mode === "tasks" ? ["问题", "任务", "执行者", "操作"] : ["资料来源", "事实与阅读记录", "结果与引用"];
  const columns = [...new Set(layout.nodes.map((node) => node.column))].sort((a, b) => a - b).map((column) => columnNames[column]);
  const counts = [
    { label: mode === "tasks" ? "个拆分任务" : "来源", value: graph.nodes.filter((node) => node.kind === (mode === "tasks" ? "task" : "source")).length },
    { label: mode === "tasks" ? "操作" : "事实 / 片段", value: graph.nodes.filter((node) => mode === "tasks" ? node.kind === "operation" : ["evidence", "reading"].includes(node.kind)).length },
    { label: "关联", value: graph.edges.length },
  ];

  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const update = () => setViewportWidth(Math.round(element.getBoundingClientRect().width));
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!fullscreen) return;
    const previous = document.activeElement as HTMLElement | null;
    const before = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    root.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setFullscreen(false); return; }
      if (event.key !== "Tab") return;
      const focusable = Array.from(root.current?.querySelectorAll<HTMLElement>("button:not([disabled]), a[href], [tabindex='0']") || []);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", keydown);
    return () => { document.body.style.overflow = before; document.removeEventListener("keydown", keydown); previous?.focus(); };
  }, [fullscreen]);

  const reset = () => { setZoom(1); setPan({ x: 0, y: 0 }); viewport.current?.scrollTo({ top: 0, left: 0 }); };
  const switchMode = (next: ResearchGraphMode) => { setMode(next); setSelectedId(null); setExpanded(new Set()); reset(); };
  const select = (node: ResearchGraphNode) => {
    if (node.kind === "group") {
      setExpanded((current) => new Set([...current, node.groupKey]));
      setSelectedId(null); reset();
    } else setSelectedId((current) => current === node.id ? null : node.id);
  };
  const hasNodes = layout.nodes.length > 0;

  return <section ref={root} className={`rg-shell ${fullscreen ? "rg-fullscreen" : ""}`} role={fullscreen ? "dialog" : "region"} aria-modal={fullscreen || undefined} aria-label="研究可视化画布">
    <header className="rg-header">
      <div className="rg-title"><span className="rg-title-icon"><GitBranch size={17} /></span><div><strong>研究地图</strong><small>任务、来源与结果的真实关联</small></div></div>
      <div className="rg-mode" role="group" aria-label="图谱模式"><button className={mode === "tasks" ? "active" : ""} onClick={() => switchMode("tasks")} aria-pressed={mode === "tasks"}><GitBranch size={13} />任务状态</button><button className={mode === "evidence" ? "active" : ""} onClick={() => switchMode("evidence")} aria-pressed={mode === "evidence"}><Layers3 size={13} />来源与证据</button></div>
      <button className="rg-icon" onClick={() => setFullscreen(!fullscreen)} aria-label={fullscreen ? "退出全屏画布" : "全屏展开画布"} title={fullscreen ? "退出全屏 · Esc" : "全屏展开"}>{fullscreen ? <X size={17} /> : <Maximize2 size={16} />}</button>
    </header>
    <div className="rg-stats"><div className="rg-live"><i className={status === "running" ? "running" : ""} />{status === "running" ? "随任务更新" : hasNodes ? "已保存的研究记录" : "等待研究记录"}</div><div>{counts.map((item) => <span key={item.label}><b>{item.value}</b>{item.label}</span>)}</div></div>
    {narrow && hasNodes && <div className="rg-scroll-hint">向下滚动查看各层记录 · 点击节点展开详情</div>}
    <div className={`rg-body ${selected ? "has-detail" : ""}`}>
      <div ref={viewport} className={`rg-viewport ${narrow ? "rg-narrow" : ""}`}>
        {hasNodes ? <>
          {!narrow && <div className="rg-lanes" aria-hidden="true">{columns.map((column) => <span key={column}>{column}</span>)}</div>}
          <svg className="rg-svg" viewBox={`${viewX} ${viewY} ${viewWidth} ${viewHeight}`} aria-label={mode === "tasks" ? "研究任务与实际操作关系图" : "资料、事实与回答引用关系图"}
            style={narrow ? { width: `${zoom * 100}%`, height: `${layout.height * (viewportWidth || 360) / layout.width * zoom}px` } : undefined}
            onPointerDown={(event) => { if (narrow || (event.target as Element).closest("[data-node]")) return; const bounds = event.currentTarget.getBoundingClientRect(); drag.current = { x: event.clientX, y: event.clientY, ox: pan.x, oy: pan.y, width: bounds.width, height: bounds.height }; event.currentTarget.setPointerCapture(event.pointerId); }}
            onPointerMove={(event) => { const start = drag.current; if (!start) return; setPan({ x: start.ox - (event.clientX - start.x) * viewWidth / start.width, y: start.oy - (event.clientY - start.y) * viewHeight / start.height }); }}
            onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}>
            <defs><marker id={`${marker}-arrow`} markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7" fill="#5f8c82" /></marker><pattern id={`${marker}-dots`} width="24" height="24" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".7" fill="#99b7af" opacity=".13" /></pattern></defs>
            <rect x={viewX} y={viewY} width={viewWidth} height={viewHeight} fill={`url(#${marker}-dots)`} />
            {narrow && [...new Set(layout.nodes.map((node) => node.column))].map((column) => {
              const first = layout.nodes.find((node) => node.column === column)!;
              return <text key={`heading:${column}`} x="40" y={first.y - 15} fill="#8fac9a" fontSize="10" letterSpacing="1.4">{columnNames[column]}</text>;
            })}
            {layout.edges.map((edge) => {
              const from = indexed.get(edge.source), to = indexed.get(edge.target);
              if (!from || !to) return null;
              const active = selectedId === edge.source || selectedId === edge.target;
              return <g key={edge.id} opacity={selectedId && !active ? .15 : 1}><path d={curve(from, to, narrow)} fill="none" stroke={edge.relation === "conflict" || edge.relation === "contradicts" ? "#dfaa86" : active ? "#b9dfc6" : "#42685f"} strokeWidth={active ? 2.4 : 1.4} strokeDasharray={["dependency", "context", "rejected", "superseded"].includes(edge.relation) ? "5 5" : undefined} markerEnd={`url(#${marker}-arrow)`} /><title>{edge.label}{edge.count > 1 ? ` · ${edge.count} 条关联` : ""}</title>{active && <text x={narrow ? from.x + from.width / 2 + 18 : (from.x + from.width + to.x) / 2} y={narrow ? (from.y + from.height + to.y) / 2 : (from.y + to.y) / 2 + from.height / 2 - 7} fill="#c8e5d5" fontSize="10" textAnchor={narrow ? "start" : "middle"}>{edge.label}{edge.count > 1 ? ` ×${edge.count}` : ""}</text>}</g>;
            })}
            {layout.nodes.map((node) => {
              const tone = TONES[node.kind];
              const isRunning = status === "running" && ["running", "exploring"].includes(node.status || "");
              const chosen = selectedId === node.id;
              const dim = neighborhood && !neighborhood.has(node.id);
              const labelLines = lines(node.label);
              return <g key={node.id} data-node="true" role="button" tabIndex={0} aria-label={`${NODE_LABELS[node.kind]}：${node.label}，${STATUS_LABELS[node.status || ""] || node.subtitle}`} aria-pressed={chosen} onClick={() => select(node)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(node); } }} transform={`translate(${node.x},${node.y})`} className={`rg-node ${isRunning ? "rg-node-running" : ""}`} opacity={dim ? .3 : 1} style={{ "--node-color": tone } as CSSProperties}>
                {node.kind === "group" && <><rect x="6" y="6" width={node.width} height={node.height} rx="13" fill="#172f2d" stroke="#38574f" /><rect x="3" y="3" width={node.width} height={node.height} rx="13" fill="#1b3530" stroke="#3c6157" /></>}
                <rect className="rg-node-back" width={node.width} height={node.height} rx="13" fill={chosen ? "#284b3e" : "#19352f"} stroke={chosen ? "#d1e7c2" : isRunning ? "#8bcab2" : "#38554b"} strokeWidth={chosen ? 2 : 1} />
                <rect x="0" y="19" width="3" height="28" rx="1.5" fill={tone} />
                <text x="16" y="20" fill={tone} fontSize="9.3" fontWeight="600" letterSpacing=".6">{NODE_LABELS[node.kind]}</text>
                {node.status && <><circle cx={node.width - 71} cy="16" r="3" fill={node.status === "error" ? "#e4a987" : isRunning ? "#b9e6cc" : "#729c8b"} /><text x={node.width - 62} y="20" fill="#9cb9ad" fontSize="8.4">{short(STATUS_LABELS[node.status] || node.status, 7)}</text></>}
                <text x="16" y="43" fill="#ecf3e9" fontSize="13.2" fontWeight="500">{labelLines.map((line, i) => <tspan x="16" dy={i ? 18 : 0} key={i}>{line}</tspan>)}</text>
                <text x="16" y="80" fill="#91aba1" fontSize="9.1">{short(node.subtitle, 30)}</text>
                <title>{node.label}</title>
              </g>;
            })}
          </svg>
          <div className="rg-controls"><button onClick={() => setZoom(Math.max(.6, zoom / 1.25))} aria-label="缩小图谱" title="缩小"><Minus size={15} /></button><span>{Math.round(zoom * 100)}%</span><button onClick={() => setZoom(Math.min(3, zoom * 1.25))} aria-label="放大图谱" title="放大"><Plus size={15} /></button><i /><button onClick={reset} aria-label="图谱适应窗口" title="适应窗口"><Focus size={16} /></button>{expanded.size > 0 && <button onClick={() => { setExpanded(new Set()); setSelectedId(null); reset(); }} aria-label="收起聚合节点" title="收起节点"><Layers3 size={15} /></button>}</div>
          <div className="rg-help">{narrow ? "向下滚动查看 · 点击节点" : "拖动画布 · 点击节点查看"}{layout.collapsedGroups > 0 ? " · 聚合节点可展开" : ""}</div>
        </> : <div className="rg-empty"><span><ScanLine size={32} strokeWidth={1.1} /></span><strong>{mode === "tasks" ? "研究从一个问题开始" : "来源会在读取资料后出现"}</strong><p>{mode === "tasks" ? "提交任务后，这里会逐步呈现实际任务、执行者和操作。" : "读取记录、保存的事实与回答引用，会在这里形成可查看的关联。"}</p></div>}
      </div>
      {selected && <aside className="rg-details" aria-label="图节点详情"><div className="rg-details-heading"><span style={{ color: TONES[selected.kind] }}>{NODE_LABELS[selected.kind]}</span><button className="rg-icon" aria-label="关闭节点详情" onClick={() => setSelectedId(null)}><X size={15} /></button></div><h3>{selected.label}</h3>{selected.status && <div className="rg-status-chip">{STATUS_LABELS[selected.status] || selected.status}</div>}{selected.details.filter((item) => item.value).map((item, index) => <div className="rg-detail-field" key={`${item.label}-${index}`}><label>{item.label}</label><p>{item.value}</p></div>)}{selected.href && (selected.href.startsWith("local://") ? onOpenLocalSource && <button className="rg-open" onClick={() => onOpenLocalSource(selected.href!)}>查看原文位置<ArrowUpRight size={14} /></button> : <a className="rg-open" href={selected.href} target="_blank" rel="noopener noreferrer">打开来源<ArrowUpRight size={14} /></a>)}</aside>}
    </div>
    <footer className="rg-footer"><span><i className="rg-legend-line" />已记录关联<i className="rg-legend-line dashed" />依赖或背景</span><span>{mode === "evidence" && graph.local ? "阅读片段与事实记录分别展示" : "节点状态来自任务记录"}</span></footer>
    {graph.unmatchedCitations > 0 && mode === "evidence" && <div className="rg-citation-note">{graph.unmatchedCitations} 条回答引用未在当前记录中匹配到对应来源，未绘制关联。</div>}
    {graph.operations.length > 0 && <div className="rg-timeline"><button className="rg-timeline-toggle" onClick={() => setTimelineOpen(!timelineOpen)} aria-expanded={timelineOpen}><span><Expand size={13} />操作记录 <b>{graph.operations.length}</b></span><ChevronDown size={14} style={{ transform: timelineOpen ? "rotate(180deg)" : undefined }} /></button>{timelineOpen && <ol>{graph.operations.slice(-20).map((operation) => <li key={operation.id}><i className={operation.status === "error" ? "failed" : ""} /><div><strong>{researchOperationLabel(operation.tool)}</strong><span>{operation.agent}</span></div><small>{STATUS_LABELS[operation.status] || operation.status}</small></li>)}</ol>}{timelineOpen && graph.operations.length > 20 && <p className="rg-timeline-note">显示最近 20 次操作；画布保留全部操作的聚合。</p>}</div>}
    <style jsx>{`
      .rg-shell{--rg-border:#355147;position:relative;overflow:hidden;border:1px solid #3c594d;border-radius:20px;color:#e8f0e4;background:#132c26;box-shadow:0 10px 40px #14332715;font-family:inherit}
      .rg-fullscreen{position:fixed;inset:14px;z-index:1000;display:flex;flex-direction:column;box-shadow:0 0 0 30px #101e19cc}.rg-header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:20px 22px 16px;border-bottom:1px solid var(--rg-border)}.rg-title{display:flex;align-items:center;gap:11px;flex:1;min-width:150px}.rg-title-icon{display:grid;place-items:center;width:34px;height:34px;border:1px solid #587565;border-radius:11px;color:#cee3be}.rg-title strong{display:block;font-size:15px;font-weight:550;letter-spacing:.03em}.rg-title small{display:block;margin-top:4px;font-size:10px;color:#8eaa9c;letter-spacing:.03em}.rg-mode{display:flex;padding:3px;gap:3px;background:#0e251f;border:1px solid #304c40;border-radius:10px}.rg-mode button{display:flex;gap:5px;align-items:center;font-size:11px;color:#95afa2;padding:7px 10px;border:0;border-radius:7px;background:transparent;cursor:pointer}.rg-mode button.active{background:#d2e3c0;color:#21382c;box-shadow:0 1px 4px #0002}.rg-icon{display:inline-flex;align-items:center;justify-content:center;width:29px;height:29px;border:1px solid #3a574a;border-radius:8px;background:transparent;color:#a9c4b4;cursor:pointer;flex-shrink:0}.rg-icon:hover{background:#294638;color:#f0f4e7}.rg-stats{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 22px;color:#8fac9b;font-size:10px}.rg-live{display:flex;align-items:center;gap:7px}.rg-live i{width:5px;height:5px;background:#8bb69b;border-radius:50%}.rg-live i.running{box-shadow:0 0 0 4px #95d0ad16}.rg-stats>div:last-child{display:flex;gap:20px}.rg-stats b{font-size:13px;font-weight:500;margin-right:5px;color:#d7e5cc;font-variant-numeric:tabular-nums}.rg-body{display:flex;min-width:0}.rg-viewport{position:relative;flex:1;min-width:0;height:430px;background:radial-gradient(ellipse at 48% 36%,#25483b45,transparent 70%),#122a25}.rg-fullscreen .rg-body{flex:1;min-height:250px}.rg-fullscreen .rg-viewport{height:100%;min-height:420px}.rg-svg{width:100%;height:100%;display:block;touch-action:none;cursor:grab}.rg-svg:active{cursor:grabbing}.rg-lanes{position:absolute;inset:6px 30px auto;display:flex;justify-content:space-around;color:#7d9a8b;font-size:9px;letter-spacing:.15em;pointer-events:none;z-index:1}.rg-node{cursor:pointer;outline:none;transition:opacity .2s}.rg-node:focus .rg-node-back{stroke:#d9ecbe;stroke-width:2.5px}.rg-node:hover .rg-node-back{fill:#294b3f;stroke:#96b99e}.rg-node-running .rg-node-back{animation:rg-breathe 2.2s ease-in-out infinite}@keyframes rg-breathe{50%{stroke:#cae8c4;stroke-width:2}}.rg-controls{position:absolute;left:17px;bottom:17px;display:flex;align-items:center;padding:4px;gap:2px;background:#10241fe8;border:1px solid #3c5949;border-radius:10px;box-shadow:0 5px 20px #0002}.rg-controls button{display:grid;place-items:center;width:26px;height:26px;background:transparent;border:0;color:#c1d7c3;border-radius:6px;cursor:pointer}.rg-controls button:hover{background:#2b4838}.rg-controls span{font-size:9px;min-width:35px;text-align:center;font-variant-numeric:tabular-nums;color:#9eb6a4}.rg-controls i{width:1px;height:14px;background:#3e5749;margin:0 3px}.rg-help{position:absolute;right:18px;bottom:23px;color:#7e9e8d;font-size:9px;pointer-events:none}.rg-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;height:100%;padding:35px;color:#9bb4a3}.rg-empty>span{display:grid;place-items:center;width:76px;height:76px;background:#1d3c3133;border:1px solid #375849;border-radius:24px;color:#9dbc96;margin-bottom:24px}.rg-empty strong{font-size:15px;letter-spacing:.02em;font-weight:500;color:#c9dcc1}.rg-empty p{font-size:11px;line-height:1.8;max-width:290px;margin-top:11px;color:#789c86}.rg-details{width:270px;max-height:430px;flex-shrink:0;border-left:1px solid var(--rg-border);background:#182f26;padding:17px 18px;overflow:auto}.rg-fullscreen .rg-details{max-height:none;width:300px}.rg-details-heading{display:flex;justify-content:space-between;align-items:center;font-size:10px;letter-spacing:.06em}.rg-details h3{font-size:14px;line-height:1.8;font-weight:500;margin:10px 0 8px;overflow-wrap:anywhere}.rg-status-chip{display:inline-block;padding:3px 7px;border:1px solid #49624f;color:#aac49e;border-radius:5px;font-size:9px}.rg-detail-field{margin-top:17px}.rg-detail-field label{display:block;color:#819f8d;font-size:9px;margin-bottom:6px}.rg-detail-field p{font-size:11px;line-height:1.85;color:#c0d2bb;white-space:pre-wrap;overflow-wrap:anywhere;margin:0;max-height:220px;overflow:auto}.rg-open{display:flex;justify-content:space-between;align-items:center;gap:15px;width:100%;margin-top:18px;padding:9px 10px;border:1px solid #536e53;border-radius:8px;background:#294634;color:#d1e2c3;text-decoration:none;cursor:pointer;font-size:11px}.rg-footer{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:12px 20px;border-top:1px solid var(--rg-border);font-size:9px;color:#819d8b}.rg-footer>span:first-child{display:flex;align-items:center;gap:6px}.rg-legend-line{width:17px;height:0;border-top:1.5px solid #86ac90}.rg-legend-line.dashed{border-top-style:dashed;margin-left:9px}.rg-citation-note{font-size:10px;padding:0 20px 12px;color:#bfae85}.rg-timeline{border-top:1px solid var(--rg-border);background:#193127}.rg-timeline-toggle{display:flex;width:100%;justify-content:space-between;align-items:center;padding:12px 20px;border:0;background:transparent;color:#a9c1a7;cursor:pointer;font-size:11px}.rg-timeline-toggle span{display:flex;align-items:center;gap:7px}.rg-timeline-toggle b{font-size:9px;font-weight:400;background:#2d4938;padding:1px 5px;border-radius:4px}.rg-timeline ol{list-style:none;margin:0;padding:0 20px 14px;max-height:190px;overflow:auto}.rg-timeline li{display:flex;align-items:center;gap:10px;min-height:38px;border-top:1px solid #304b3b;color:#b0c6ab}.rg-timeline li>i{width:5px;height:5px;background:#739f7c;border-radius:50%}.rg-timeline li>i.failed{background:#d4a07b}.rg-timeline li>div{flex:1;display:flex;gap:9px;align-items:center}.rg-timeline li strong{font-size:10px;font-weight:500}.rg-timeline li span,.rg-timeline li small{font-size:9px;color:#799783}.rg-timeline-note{font-size:9px;color:#819c86;margin:0;padding:0 20px 12px}.rg-shell button:focus-visible,.rg-shell a:focus-visible{outline:2px solid #cee3ba;outline-offset:3px}
      .rg-scroll-hint{padding:0 18px 10px;color:#90ad98;font-size:10px}.rg-narrow{overflow:auto;overscroll-behavior:contain;scrollbar-color:#456452 #152d25;scrollbar-width:thin}.rg-narrow .rg-svg{touch-action:pan-x pan-y;cursor:default;max-width:none;flex-shrink:0}.rg-narrow .rg-controls{position:sticky;bottom:12px;left:14px;z-index:3;width:max-content;margin-left:14px;margin-top:-46px}.rg-fullscreen .rg-body{min-height:0;overflow:hidden}.rg-fullscreen .rg-viewport{height:auto;min-height:0}
      @media(max-width:900px){.rg-body.has-detail{flex-direction:column}.rg-details{width:100%;max-height:270px;border-left:0;border-top:1px solid var(--rg-border)}.rg-fullscreen .rg-details{width:100%;max-height:230px}.rg-fullscreen .rg-body.has-detail .rg-viewport{min-height:220px;flex:1}.rg-header{padding:15px}.rg-title small{display:none}.rg-help{display:none}.rg-viewport{height:390px}}@media(max-width:540px){.rg-header{gap:8px}.rg-mode{order:3;width:100%}.rg-mode button{flex:1;justify-content:center}.rg-title strong{font-size:14px}.rg-stats{padding:12px 15px;gap:6px}.rg-stats>div:last-child{gap:9px}.rg-footer{flex-direction:column;align-items:flex-start;padding:11px 15px}.rg-viewport{height:340px}.rg-fullscreen{inset:5px;border-radius:14px}.rg-lanes{inset:8px 10px auto;font-size:8px}.rg-timeline li>div{flex-direction:column;align-items:flex-start;gap:3px;padding:6px 0}}@media(prefers-reduced-motion:reduce){.rg-node-running .rg-node-back{animation:none}.rg-node{transition:none}}
    `}</style>
  </section>;
}
