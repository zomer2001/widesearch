"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Focus, Minus, Plus } from "lucide-react";
import { KIND_LABELS, layoutEntityGraph, type KnowledgeEntity, type KnowledgeRelation, type KnowledgeView, type PositionedKnowledgeEntity } from "@/lib/knowledgeDatabase";

interface Props {
  entities: KnowledgeEntity[];
  relations: KnowledgeRelation[];
  view: KnowledgeView;
  selectedEntityId: string | null;
  selectedRelationId: string | null;
  onSelectEntity: (id: string) => void;
  onSelectRelation: (id: string) => void;
}
const COLORS = { product: "#2c7255", technology: "#427b99", route: "#967044", reference: "#827293" };
function short(value: string, length: number) { return value.length > length ? `${value.slice(0, length - 1)}…` : value; }
function nodeLines(value: string): string[] {
  const lines: string[] = []; let line = "", units = 0;
  for (const character of value) {
    const weight = /[^\u0000-\u00ff]/.test(character) ? 2 : 1;
    if (units + weight > 27 && line) { lines.push(line.trim()); line = ""; units = 0; }
    line += character; units += weight;
  }
  if (line) lines.push(line.trim());
  return lines.length > 2 ? [lines[0], `${lines[1].slice(0, -1)}…`] : lines;
}
function edgePath(source: PositionedKnowledgeEntity, target: PositionedKnowledgeEntity) {
  const y1 = source.y + source.height / 2, y2 = target.y + target.height / 2;
  if (source.x === target.x) {
    const x = source.x + source.width, out = x + 95;
    return { d: `M ${x} ${y1} C ${out} ${y1}, ${out} ${y2}, ${x} ${y2}`, x: x + 69, y: (y1 + y2) / 2 };
  }
  const forward = target.x > source.x;
  const x1 = source.x + (forward ? source.width : 0), x2 = target.x + (forward ? 0 : target.width), mid = (x1 + x2) / 2;
  return { d: `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`, x: mid, y: (y1 + y2) / 2 };
}
export default function KnowledgeGraphCanvas({ entities, relations, view, selectedEntityId, selectedRelationId, onSelectEntity, onSelectRelation }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const [viewport, setViewport] = useState({ width: 900, height: 590 });
  const [camera, setCamera] = useState({ zoom: 1, x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; panX: number; panY: number } | null>(null);
  const pattern = useId().replace(/:/g, "");
  const layout = useMemo(() => layoutEntityGraph(entities, relations, view), [entities, relations, view]);
  const nodes = new Map(layout.nodes.map((node) => [node.id, node]));
  const fit = Math.min((viewport.width - 44) / layout.width, (viewport.height - 40) / layout.height, 1.15);
  const scale = Math.max(.1, fit) * camera.zoom;
  const offsetX = (viewport.width - layout.width * scale) / 2 + camera.x;
  const offsetY = (viewport.height - layout.height * scale) / 2 + camera.y;
  const zoom = (change: number) => setCamera((previous) => ({ ...previous, zoom: Math.max(.35, Math.min(4, previous.zoom * change)) }));
  useEffect(() => {
    if (!container.current) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0 && entry.contentRect.height > 0) setViewport({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  return <div className="kd-graph-shell">
    <div className="kd-graph-legend" aria-label="图谱图例">{Object.entries(COLORS).map(([kind, color]) => <span key={kind}><i style={{ background: color }} />{KIND_LABELS[kind as keyof typeof KIND_LABELS]}</span>)}<span className="kd-graph-reference-note">虚线：独立参考或手工关系</span></div>
    <div className="kd-graph-viewport" ref={container}>
      {entities.length === 0 ? <div className="kd-empty"><p>当前范围没有匹配的实体。</p><span>清除搜索或切换为全部条目。</span></div> : <svg className="kd-graph-svg" aria-label={`${view === "products" ? "产品" : "技术"}知识图谱，${entities.length} 个实体，${relations.length} 条关系`} role="group" viewBox={`0 0 ${viewport.width} ${viewport.height}`}
        onPointerDown={(event) => { if (event.button !== 0) return; drag.current = { x: event.clientX, y: event.clientY, panX: camera.x, panY: camera.y }; event.currentTarget.setPointerCapture(event.pointerId); }}
        onPointerMove={(event) => { const start = drag.current; if (start) setCamera((previous) => ({ ...previous, x: start.panX + event.clientX - start.x, y: start.panY + event.clientY - start.y })); }}
        onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}>
        <defs><pattern id={`${pattern}-dots`} x="0" y="0" width="20" height="20" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".8" fill="#cad7d1" /></pattern><marker id={`${pattern}-arrow`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#7e9e91" /></marker><marker id={`${pattern}-selected-arrow`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#28765a" /></marker></defs>
        <rect width="100%" height="100%" fill={`url(#${pattern}-dots)`} />
        <g transform={`translate(${offsetX}, ${offsetY}) scale(${scale})`}>
          {layout.columns.map((column) => <g key={column.kind} className="kd-column-heading"><text x={column.x + 2} y={32} fill={COLORS[column.kind]}>{KIND_LABELS[column.kind]}<tspan dx="9" fill="#8a9e92">{column.count}</tspan></text><line x1={column.x} y1={46} x2={column.x + 250} y2={46} stroke="#d7e3db" /></g>)}
          {layout.edges.map((edge) => {
            const source = nodes.get(edge.source_id)!, target = nodes.get(edge.target_id)!, path = edgePath(source, target);
            const selected = edge.id === selectedRelationId;
            const related = source.id === selectedEntityId || target.id === selectedEntityId;
            const label = edge.label || edge.predicate;
            return <g key={edge.id} className={`kd-edge ${selected ? "selected" : ""}`} opacity={selected || related || !selectedEntityId ? 1 : .55}>
              <path d={path.d} fill="none" stroke={selected ? "#28765a" : "#91aa9b"} strokeWidth={selected ? 2.8 : related ? 2 : 1.3} strokeDasharray={edge.predicate === "reference_for" || edge.origin === "manual" ? "6 5" : undefined} markerEnd={`url(#${pattern}-${selected ? "selected-" : ""}arrow)`} pointerEvents="none" />
              <g role="button" tabIndex={0} aria-label={`${source.name} ${label} ${target.name}`} onPointerDown={(event) => event.stopPropagation()} onClick={() => onSelectRelation(edge.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelectRelation(edge.id); } }}>
                <path d={path.d} fill="none" stroke="transparent" strokeWidth={18} />
                <rect x={path.x - 40} y={path.y - 10} width="80" height="23" rx="6" fill={selected ? "#e7f2ea" : "#f4f8f4"} />
                <text x={path.x} y={path.y + 4} textAnchor="middle" fill={selected ? "#266c4d" : "#6c8674"} fontSize="11">{short(label, 7)}</text>
                <title>{source.name} → {label} → {target.name}</title>
              </g>
            </g>;
          })}
          {layout.nodes.map((node) => {
            const selected = node.id === selectedEntityId, lines = nodeLines(node.name);
            const connected = layout.edges.some((edge) => edge.source_id === node.id || edge.target_id === node.id);
            return <g key={node.id} role="button" tabIndex={0} aria-label={`${KIND_LABELS[node.kind]}：${node.name}${connected ? "" : "，暂无已确认关系"}`} aria-pressed={selected} className={`kd-node kd-node-${node.kind} ${selected ? "selected" : ""}`} transform={`translate(${node.x} ${node.y})`} onPointerDown={(event) => event.stopPropagation()} onClick={() => onSelectEntity(node.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelectEntity(node.id); } }}>
              <rect width={node.width} height={node.height} rx={11} fill={selected ? "#edf6ee" : "#fff"} stroke={selected ? COLORS[node.kind] : "#d4e2d7"} strokeWidth={selected ? 2 : 1} />
              <rect x="0" y="16" width="4" height={node.height - 32} rx="2" fill={COLORS[node.kind]} />
              <circle cx="19" cy="19" r="3" fill={COLORS[node.kind]} /><text x="29" y="23" fontSize="11" fill="#667e6e">{node.kind === "reference" ? "独立开源参考" : short(node.category || KIND_LABELS[node.kind], 22)}</text>
              {lines.map((line, index) => <text x="18" y={lines.length === 1 ? 53 : 46 + index * 18} key={index} fontSize="15" fontWeight="600" fill="#294d3b">{line}</text>)}
              <text x="18" y="86" fontSize="10" fill="#6f8577">{node.origin === "manual" ? "手工条目 · 待补证据" : connected ? "点击查看属性与依据" : "暂无已确认关系"}</text><title>{node.name}</title>
            </g>;
          })}
        </g>
      </svg>}
      <div className="kd-graph-controls"><button aria-label="放大图谱" onClick={() => zoom(1.25)}><Plus size={16} /></button><span>{Math.round(scale * 100)}%</span><button aria-label="缩小图谱" onClick={() => zoom(.8)}><Minus size={16} /></button><button aria-label="适应画布" title="适应画布" onClick={() => setCamera({ zoom: 1, x: 0, y: 0 })}><Focus size={16} /></button></div>
      <span className="kd-pan-help">拖动画布平移 · 点击实体或连线查看详情</span>
    </div>
  </div>;
}
