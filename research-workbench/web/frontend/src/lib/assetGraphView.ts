import { NODE_COLORS, type AssetGraph, type GraphNode } from "./researchAssets";

export type GraphLayout = "force-atlas2" | "antv-dagre" | "circular";
export type GraphSize = "neighbor_count" | "source_count" | "uniform";
export type GraphColor = "kind" | "namespace" | "distance";
export const GRAPH_PALETTE = ["#5579c6", "#33a58a", "#be87c4", "#e4aa55", "#62aebe", "#dd817f", "#9395b0", "#89ac65"];

export function filterGraph(graph: AssetGraph, kinds: string[], predicates: string[], minimumDegree: number): AssetGraph {
  const nodes = graph.nodes.filter((n) => n.id === graph.root_id ||
    (kinds.includes(n.kind) && n.neighbor_count >= minimumDegree));
  const ids = new Set(nodes.map((n) => n.id));
  return { ...graph, nodes, edges: graph.edges.filter((e) =>
    ids.has(e.source) && ids.has(e.target) && predicates.includes(e.predicate)) };
}

export function nodeDiameter(node: GraphNode, graph: AssetGraph, metric: GraphSize, multiplier = 1): number {
  if (metric === "uniform") return 28 * multiplier;
  const maximum = Math.max(1, ...graph.nodes.map((n) => n[metric] ?? 0));
  const value = Math.max(0, node[metric] ?? 0);
  return (20 + 42 * Math.sqrt(Math.log1p(value) / Math.log1p(maximum))) * multiplier;
}

export function nodeColor(node: GraphNode, graph: AssetGraph, attribute: GraphColor): string {
  if (attribute === "kind") return NODE_COLORS[node.kind] ?? "#8a97ac";
  if (attribute === "distance") return node.distance === null ? "#9ba7b8" : GRAPH_PALETTE[Math.abs(node.distance) % GRAPH_PALETTE.length];
  const namespaces = [...new Set(graph.nodes.map((n) => n.namespace))].sort();
  return GRAPH_PALETTE[namespaces.indexOf(node.namespace) % GRAPH_PALETTE.length];
}

const xml = (value: unknown) => String(value ?? "").replace(/[<>&"']/g,
  (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;" })[c]!);

// GEXF is Gephi's native exchange format. Only visible, evidence-bound edges are exported.
export function graphGexf(graph: AssetGraph): string {
  return `<?xml version="1.0" encoding="UTF-8"?>\n<gexf xmlns="http://gexf.net/1.3" version="1.3"><graph mode="static" defaultedgetype="directed"><attributes class="node"><attribute id="kind" title="kind" type="string"/><attribute id="namespace" title="namespace" type="string"/><attribute id="degree" title="degree" type="integer"/></attributes><nodes>${graph.nodes.map((n) => `<node id="${xml(n.id)}" label="${xml(n.name)}"><attvalues><attvalue for="kind" value="${xml(n.kind)}"/><attvalue for="namespace" value="${xml(n.namespace)}"/><attvalue for="degree" value="${n.neighbor_count}"/></attvalues></node>`).join("")}</nodes><edges>${graph.edges.map((e) => `<edge id="${xml(e.id)}" source="${xml(e.source)}" target="${xml(e.target)}" label="${xml(e.label)}"/>`).join("")}</edges></graph></gexf>`;
}

export function downloadGraph(name: string, body: string, type: string) {
  const url = URL.createObjectURL(new Blob([body], { type }));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = name;
  document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
