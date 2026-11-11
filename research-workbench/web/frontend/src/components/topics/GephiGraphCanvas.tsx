"use client";

import { useEffect, useRef, useState, type RefObject } from "react";
import type { Graph, IElementEvent } from "@antv/g6";
import { Loader2 } from "lucide-react";
import { nodeColor, nodeDiameter, type GraphColor, type GraphLayout, type GraphSize } from "@/lib/assetGraphView";
import type { AssetGraph } from "@/lib/researchAssets";

export interface GraphActions {
  fit: () => void; zoom: (ratio: number) => void; focus: (id: string) => void; exportPng: () => void;
}

// Published G6 Dagre/Polyline, ForceAtlas2 and hover-activate implementations.
export default function GephiGraphCanvas({ graph, reference, layout, size, color, scale, labels, edgeLabels,
  spacing, revision, selectedNode, selectedEdge, onNode, onEdge, onClear, actionsRef }: {
  graph: AssetGraph; reference: AssetGraph; layout: GraphLayout; size: GraphSize; color: GraphColor;
  scale: number; labels: boolean; edgeLabels: boolean; spacing: number; revision: number;
  selectedNode: string | null; selectedEdge: string | null;
  onNode: (id: string) => void; onEdge: (id: string) => void; onClear: () => void;
  actionsRef: RefObject<GraphActions | null>;
}) {
  const host = useRef<HTMLDivElement>(null), instance = useRef<Graph | null>(null);
  const callbacks = useRef({ onNode, onEdge, onClear }); callbacks.current = { onNode, onEdge, onClear };
  const positions = useRef(new Map<string, { x: number; y: number }>()), lastLayout = useRef("");
  const [error, setError] = useState(""), [busy, setBusy] = useState(true);
  useEffect(() => {
    if (!host.current) return;
    const container = host.current;
    let cancelled = false, renderer: Graph | null = null, observer: ResizeObserver | undefined;
    const signature = `${layout}-${spacing}-${size}-${scale}-${revision}-${graph.nodes.map((n) => n.id).join("|")}-${graph.edges.map((e) => e.id).join("|")}`;
    const canReuse = lastLayout.current === signature && positions.current.size > 0;
    const build = async () => {
      try {
        const { Graph } = await import("@antv/g6");
        if (cancelled) return;
        setBusy(true); setError("");
        const layered = layout === "antv-dagre", pairs = new Map<string, string[]>();
        graph.edges.forEach((e) => { const key = [e.source, e.target].sort().join("|"); pairs.set(key, [...(pairs.get(key) ?? []), e.id]); });
        renderer = new Graph({
          container, width: container.clientWidth, height: container.clientHeight, autoResize: true,
          animation: false, padding: [65, 75, 65, 75], zoomRange: [.12, 4],
          data: {
            nodes: graph.nodes.map((n) => {
              const diameter = nodeDiameter(n, reference, size, scale);
              const dimensions: number | [number, number] = layered ? [Math.max(130, diameter * 2.3), Math.max(48, diameter)] : diameter;
              return { id: n.id, data: { ...n, size: dimensions }, style: {
                ...positions.current.get(n.id), size: dimensions, fill: nodeColor(n, reference, color),
                labelText: labels ? n.name : "", labelFontSize: 13, labelFontWeight: n.id === graph.root_id ? 650 : 450,
                labelFill: layered ? "#fff" : "#33465d", labelPlacement: layered ? "center" : "bottom",
                labelWordWrap: true, labelMaxWidth: layered ? Math.max(120, diameter * 2.3 - 16) : 160,
                labelMaxLines: 2, labelBackground: !layered, labelBackgroundFill: "#fff",
                labelBackgroundOpacity: .88, labelPadding: [2, 4], labelOffsetY: layered ? 0 : 7,
                radius: layered ? 9 : 0, stroke: n.id === graph.root_id ? "#263b56" : "#fff",
                lineWidth: n.id === graph.root_id ? 3 : 2, cursor: "pointer",
              } };
            }),
            edges: graph.edges.map((e) => {
              const siblings = pairs.get([e.source, e.target].sort().join("|"))!;
              const offset = (siblings.indexOf(e.id) - (siblings.length - 1) / 2) * 38;
              return { id: e.id, source: e.source, target: e.target, data: { label: e.label },
                style: { labelText: edgeLabels ? e.label : "", curveOffset: e.source < e.target ? offset : -offset } };
            }),
          },
          node: { type: layered ? "rect" : "circle", state: {
            selected: { stroke: "#192b46", lineWidth: 4, halo: true, haloStroke: "#78a0e8", haloLineWidth: 8 },
            active: { lineWidth: 3, stroke: "#283c57", labelFontWeight: 700 }, inactive: { opacity: .15 },
          } },
          edge: { type: layered ? "polyline" : "quadratic", style: {
            stroke: "#879bb4", opacity: .65, lineWidth: layered ? 1.7 : 1.2,
            endArrow: true, endArrowSize: 7, radius: 12, ...(layered ? { router: { type: "orth" } } : {}),
            labelFontSize: 11, labelFill: "#536a87", labelBackground: true,
            labelBackgroundFill: "#fff", labelBackgroundOpacity: .96, labelPadding: [3, 6],
            labelAutoRotate: false, cursor: "pointer",
          }, state: {
            selected: { stroke: "#2e63b8", lineWidth: 3, opacity: 1, labelFill: "#234d8c", labelFontWeight: 650 },
            active: { stroke: "#355f9d", opacity: 1, lineWidth: 2.2, labelText: (d) => String(d.data?.label ?? "") },
            inactive: { opacity: .07 },
          } },
          layout: canReuse ? undefined : layered ? {
            type: "antv-dagre", rankdir: graph.mode === "lineage" ? "RL" : "LR", ranksep: 115 * spacing,
            nodesep: 38 * spacing, controlPoints: true, edgeLabelSpace: true,
          } : layout === "circular" ? { type: "circular", nodeSpacing: 35 * spacing, preventOverlap: true } : {
            type: "force-atlas2", preventOverlap: true, kr: 30 * spacing, kg: .5,
            maxIteration: graph.nodes.length > 150 ? 450 : 300, prune: false, nodeSpacing: 26,
          },
          behaviors: ["drag-canvas", "zoom-canvas", "drag-element",
            { type: "fix-element-size", state: "", node: { shape: "label" },
              edge: [{ shape: "key", fields: ["lineWidth"] }, { shape: "label" }] },
            { type: "auto-adapt-label", padding: 5, throttle: 32 },
            { type: "hover-activate", degree: 1, inactiveState: "inactive", animation: false }],
        });
        instance.current = renderer;
        renderer.on("node:click", (e: IElementEvent) => callbacks.current.onNode(e.target.id));
        renderer.on("edge:click", (e: IElementEvent) => callbacks.current.onEdge(e.target.id));
        renderer.on("canvas:click", () => callbacks.current.onClear());
        await renderer.render();
        if (cancelled || renderer.destroyed) return;
        await renderer.fitView({ when: "always" }, false);
        if (renderer.getZoom() > 1.35) await renderer.zoomTo(1.35, false);
        let width = container.clientWidth, height = container.clientHeight;
        observer = new ResizeObserver(([entry]) => {
          const nextWidth = Math.round(entry.contentRect.width), nextHeight = Math.round(entry.contentRect.height);
          if (!renderer || renderer.destroyed || nextWidth < 1 || nextHeight < 1 || (width === nextWidth && height === nextHeight)) return;
          width = nextWidth; height = nextHeight;
          renderer.resize(width, height);
          void renderer.fitView({ when: "always" }, false);
        });
        observer.observe(container);
        lastLayout.current = signature;
        actionsRef.current = {
          fit: () => { void renderer?.fitView({ when: "always" }, false); },
          zoom: (ratio) => { void renderer?.zoomBy(ratio, false); },
          focus: (id) => { void renderer?.focusElement(id); },
          exportPng: () => { void renderer?.toDataURL({ mode: "overall", type: "image/png" }).then((url) => {
            const link = document.createElement("a"); link.href = url; link.download = "knowledge-graph.png"; link.click();
          }).catch((e) => setError(String(e))); },
        };
        setBusy(false); setError("");
      } catch (e) { if (!cancelled) { setError(e instanceof Error ? e.message : String(e)); setBusy(false); } }
    };
    void build();
    return () => {
      cancelled = true; actionsRef.current = null; observer?.disconnect();
      if (renderer && !renderer.destroyed) {
        positions.current = new Map(renderer.getNodeData().filter((n) => Number.isFinite(n.style?.x) && Number.isFinite(n.style?.y))
          .map((n) => [n.id, { x: Number(n.style!.x), y: Number(n.style!.y) }]));
        renderer.destroy();
      }
      instance.current = null;
    };
  }, [graph, reference, layout, size, color, scale, labels, edgeLabels, spacing, revision, actionsRef]);
  useEffect(() => {
    const renderer = instance.current;
    if (!renderer || renderer.destroyed || busy) return;
    const states = Object.fromEntries([...graph.nodes, ...graph.edges].map((e) => [e.id, e.id === selectedNode || e.id === selectedEdge ? ["selected"] : []]));
    void renderer.setElementState(states, false);
  }, [selectedNode, selectedEdge, graph, busy]);
  return <div className="gx-canvas-shell"><div ref={host} className="gx-renderer" role="group" aria-label="数据库对象关系图谱" />
    {busy && !error && <div className="gx-canvas-message"><Loader2 className="animate-spin" size={18} />正在排列图谱…</div>}
    {error && <div className="gx-canvas-message gx-error" role="alert">图谱绘制失败：{error}</div>}
    {!graph.edges.length && !busy && <div className="gx-empty-note">{graph.nodes.length > 1 ? "当前筛选下没有相连关系" : "当前范围尚无已收录关系"}</div>}
  </div>;
}
