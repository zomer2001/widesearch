/* eslint-disable @typescript-eslint/no-require-imports -- Execute asset navigation with local readers. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const flush = () => new Promise(setImmediate);
function load(file, dependencies) {
  const exports = {};
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "../src", file), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText;
  vm.runInNewContext(code, { exports, AbortController, URLSearchParams, URL, Date,
    setTimeout: (fn) => { fn(); return 1; }, clearTimeout() {}, require: (name) => dependencies[name] ?? require(name) });
  return exports;
}
function find(node, predicate) {
  if (!node || typeof node !== "object") return undefined;
  if (Array.isArray(node)) { for (const child of node) { const result = find(child, predicate); if (result) return result; } return; }
  return predicate(node) ? node : find(node.props?.children, predicate);
}
function text(node) {
  if (node == null || typeof node === "boolean") return "";
  if (Array.isArray(node)) return node.map(text).join("");
  return typeof node === "object" ? text(node.props?.children) : String(node);
}
function hooks() {
  const slots = [], effects = []; let cursor = 0;
  const useState = (initial) => {
    const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === "function" ? initial() : initial;
    return [slots[i], (value) => { slots[i] = typeof value === "function" ? value(slots[i]) : value; }];
  };
  return {
    reset() { cursor = 0; }, run() { effects.splice(0).forEach((f) => f()); },
    react: { useState, useRef(initial) { return useState({ current: initial })[0]; },
      useEffect(effect, deps) { const i = cursor++, old = slots[i];
        if (!old || deps.some((d, j) => !Object.is(d, old.deps[j]))) {
          old?.cleanup?.(); slots[i] = { deps }; effects.push(() => { slots[i].cleanup = effect(); });
        }
      } },
  };
}
const product = { id: "p1", name: "Fixture product", kind: "product", kind_label: "产品", namespace: "anduril", can_open: true, field_count: 2, source_count: 1, summary: "", updated_at: null, relationship_count: 1 };
const technology = { id: "t1", name: "Fixture technology", kind: "technology", kind_label: "技术", namespace: "anduril", can_open: true };
const detail = (node) => ({ asset: node, records: [], fields: [], sources: [], related: [], dimensions: Array.from({ length: 7 }, (_, i) => ({ id: i + 1, name: `类别${i + 1}`, records: 0, elements: [] })) });

test("资产按产品和技术进入，资料与图谱共享对象，并支持跨对象跳转", async () => {
  const h = hooks(), calls = [], reads = {
    readAssets: async (slug, kind) => { calls.push(["index", slug, kind]); return { items: [product], total: 1, counts: { product: 1, technology: 1 } }; },
    readAsset: async (slug, id) => { calls.push(["detail", slug, id]); return detail(id === "p1" ? product : technology); },
    assetDate: () => "—",
  };
  const dependencies = { react: h.react,
    "react/jsx-runtime": { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) },
    "lucide-react": new Proxy({}, { get: (_, key) => key }),
    "@/lib/researchAssets": reads,
    "@/lib/researchDatabase": { fieldValue: String },
    "./AssetSourceDrawer": { default: "Drawer" }, "./AssetGraphWorkbench": { default: "Workbench" },
  };
  const Component = load("components/topics/KnowledgeAssetsView.tsx", dependencies).default;
  let tree;
  const render = () => { h.reset(); tree = Component({ slug: "anduril", title: "Anduril" }); h.run(); return tree; };
  render(); await flush(); render();
  find(tree, (n) => n.props?.className === "ka-asset-name").props.onClick();
  render(); await flush(); render();
  const fields = find(tree, (n) => typeof n.type === "function" && n.type.name === "AssetFields");
  assert.equal(fields.props.detail.asset.id, "p1");
  fields.props.onOpen(technology);
  render(); await flush(); render();
  assert.equal(find(tree, (n) => typeof n.type === "function" && n.type.name === "AssetFields").props.detail.asset.id, "t1");
  find(tree, (n) => n.type === "button" && text(n) === "对象图谱").props.onClick();
  render();
  const graph = find(tree, (n) => typeof n.type === "function" && n.type.name === "AssetGraphPanel");
  assert.equal(graph.props.asset.id, "t1");
  assert(calls.some(([type, slug, id]) => type === "detail" && slug === "anduril" && id === "t1"));
});

test("隐藏节点不会伪造跨节点边，筛选前后的关联度和来源保持不变", () => {
  const { filterGraph, nodeDiameter, graphGexf } = load("lib/assetGraphView.ts", {
    "./researchAssets": { NODE_COLORS: { product: "#111", technology: "#222" } },
  });
  const root = { ...product, name: 'A < B & "C"', neighbor_count: 10, source_count: 3 };
  const tech = { ...technology, neighbor_count: 5, source_count: 2 };
  const child = { ...product, id: "p2", neighbor_count: 2, source_count: 1 };
  const graph = { root_id: root.id, nodes: [root, tech, child], edges: [
    { id: "e1", source: root.id, target: tech.id, predicate: "uses_technology", label: "采用", claims: [{ id: "claim-1" }] },
    { id: "e2", source: tech.id, target: child.id, predicate: "related", label: "关联", claims: [{ id: "claim-2" }] },
  ] };
  const visible = filterGraph(graph, ["product"], ["uses_technology", "related"], 0);
  assert.equal(visible.nodes.length, 2);
  assert.equal(visible.edges.length, 0);
  assert.equal(visible.nodes[0].neighbor_count, 10);
  assert.equal(graph.edges.length, 2);
  assert(nodeDiameter(root, graph, "neighbor_count") > nodeDiameter(child, graph, "neighbor_count"));
  assert.equal(nodeDiameter(root, graph, "uniform"), nodeDiameter(child, graph, "uniform"));
  const xml = graphGexf(graph);
  assert(xml.includes("A &lt; B &amp; &quot;C&quot;"));
  assert(xml.includes('source="p1" target="t1"'));
  assert(xml.includes('defaultedgetype="directed"'));
  const noRelations = filterGraph(graph, ["technology", "product"], [], 0);
  assert.equal(noRelations.edges.length, 0);
  assert.equal(noRelations.nodes.length, 3);
});

test("相同字段同值同条件合并展示证据，版本和条件不同的记录分别保留", () => {
  const { groupAssetRecords } = load("lib/researchAssets.ts", { "./researchDatabase": {} });
  const record = { id: "c1", kind: "metric", field_id: "product_endurance", label: "续航", dimension_id: 5,
    element_id: "5.values", value: { claim_id: "c1", raw_value: "2", raw_unit: "h" },
    qualifiers: { version: "v2", assertion_type: "厂商自述" }, linked_entities: [], evidence: [{ revision_id: "r1", quote: "first", stance: "supports" }] };
  const second = { ...record, id: "c2", qualifiers: { version: "v2", assertion_type: "第三方转述" }, value: { ...record.value, claim_id: "c2" }, evidence: [{ revision_id: "r2", quote: "second", stance: "supports" }] };
  const anotherVersion = { ...second, id: "c3", qualifiers: { version: "v3" } };
  const rows = groupAssetRecords([record, second, anotherVersion]);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].evidence.length, 2);
  assert.equal(rows[0].evidence[1].source_context.assertion_type, "第三方转述");
  assert.equal(rows[1].qualifiers.version, "v3");
  assert.equal(record.evidence.length, 1);
});
