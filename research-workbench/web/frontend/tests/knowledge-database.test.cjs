/* eslint-disable @typescript-eslint/no-require-imports -- Exercise graph transformations and state transitions without a browser or live API. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
function load(file, dependencies = {}, globals = {}) {
  const exports = {};
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "../src", file), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText;
  vm.runInNewContext(code, { exports, URL, AbortSignal, AbortController, require: (name) => dependencies[name] ?? require(name), ...globals });
  return exports;
}
function library(fetch) { return load("lib/knowledgeDatabase.ts", { "./api": { API_BASE: "" } }, { fetch }); }
const pure = library();
const entity = (id, kind, name, overrides = {}) => ({ id, kind, name, summary: `${name}的机制说明`, category: "通用分类", attributes: {}, evidence_ids: ["e1"], origin: "archive", status: "source_backed", ...overrides });
const relation = (id, source_id, target_id, predicate = "uses_technology") => ({ id, source_id, target_id, predicate, label: pure.PREDICATE_LABELS[predicate], evidence_ids: ["e1"], origin: "archive", status: "source_backed" });
const fixture = () => ({
  title: "知识数据库", updated_at: "2026-11-02T12:00:00Z", counts: { entities: 5, relations: 2, products: 2, technologies: 1, routes: 1, references: 1 },
  entities: [entity("p1", "product", "产品甲"), entity("p2", "product", "远端产品乙"), entity("t1", "technology", "数据归一化"), entity("r1", "route", "数据归一化"), entity("o1", "reference", "外部项目", { evidence_ids: [], attributes: { execution_status: "not_run" } })],
  relations: [relation("x1", "p1", "t1"), relation("x2", "p1", "r1", "has_route")],
  evidence: [{ id: "e1", title: "归档原文", url: "https://example.org/source", quote: "Exact source quotation", location: "L10-12", ref_id: "local://library/document", available: true }],
  gaps: ["型号谱系待补充"],
});
const plain = (value) => JSON.parse(JSON.stringify(value));

test("图谱保留同名异类实体、真实孤立参考，不从分类名称推断谱系", () => {
  const data = fixture();
  const graph = pure.graphRecords(data, "", null, false);
  assert.equal(graph.entities.length, 5);
  assert.equal(graph.relations.length, 2);
  assert.deepEqual(plain(graph.entities.filter((item) => item.name === "数据归一化").map((item) => item.kind)), ["technology", "route"]);
  assert.ok(graph.entities.some((item) => item.id === "o1"));
  assert.ok(!graph.relations.some((edge) => edge.source_id === "o1" || edge.target_id === "o1"));
  assert.ok(!graph.relations.some((edge) => edge.source_id === "p2" || edge.target_id === "p2"));
});
test("邻接聚焦仅显示直接关联，搜索能够跳出旧中心查找全库", () => {
  const data = fixture();
  const focused = pure.graphRecords(data, "", "p1", true);
  assert.deepEqual(plain(focused.entities.map((item) => item.id)), ["p1", "t1", "r1"]);
  const search = pure.graphRecords(data, "远端", "p1", true);
  assert.deepEqual(plain(search.entities.map((item) => item.id)), ["p2"]);
  assert.equal(search.relations.length, 0);
  const linkedSearch = pure.graphRecords(data, "归一化", "p2", true);
  assert.deepEqual(plain(linkedSearch.entities.map((item) => item.id)), ["p1", "t1", "r1"]);
});
test("图谱删除悬空关系而不创造补全连线", () => {
  const data = fixture(); data.relations.push(relation("dangling", "p1", "missing"));
  const graph = pure.graphRecords(data, "", null, false);
  assert.equal(graph.relations.length, 2);
  const layout = pure.layoutEntityGraph(data.entities, data.relations, "products");
  assert.equal(layout.edges.length, 2);
});
test("布局按类型分列并保证节点不重叠，技术视图的路线放在入口侧", () => {
  const data = fixture();
  const layout = pure.layoutEntityGraph(data.entities, data.relations, "products");
  const overlaps = (a, b) => a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
  for (let i = 0; i < layout.nodes.length; i++) for (let j = i + 1; j < layout.nodes.length; j++) assert.equal(overlaps(layout.nodes[i], layout.nodes[j]), false);
  for (const node of layout.nodes) { assert.ok(node.x >= 0 && node.y > 46); assert.ok(node.x + node.width <= layout.width); assert.ok(node.y + node.height <= layout.height); }
  assert.deepEqual(plain(layout.columns.map((column) => column.kind)), ["product", "technology", "route", "reference"]);
  assert.deepEqual(plain(pure.layoutEntityGraph(data.entities, data.relations, "technologies").columns.map((column) => column.kind)), ["route", "technology", "product", "reference"]);
});
test("关系类型限制匹配后端，独立开源参考不能成为实际采用关系的起点", () => {
  assert.deepEqual(plain(pure.relationKinds("uses_technology")), { source: ["product"], target: ["technology"] });
  assert.deepEqual(plain(pure.relationKinds("has_route")), { source: ["product", "technology"], target: ["route"] });
  assert.deepEqual(plain(pure.relationKinds("reference_for")), { source: ["reference"], target: ["technology", "route"] });
  assert.deepEqual(plain(pure.relationKinds("variant_of")), { source: ["product"], target: ["product"] });
});
test("实体表过滤综合类型与名称，外链禁止脚本协议和内嵌凭据", () => {
  const item = entity("t1", "technology", "Data Mesh");
  assert.ok(pure.matchesEntity(item, "mesh", "technology"));
  assert.equal(pure.matchesEntity(item, "mesh", "product"), false);
  assert.equal(pure.safeEvidenceUrl("javascript:alert(1)"), null);
  assert.equal(pure.safeEvidenceUrl("https://secret@example.org/a"), null);
  assert.equal(pure.safeEvidenceUrl("https://example.org/a"), "https://example.org/a");
});
test("数据库操作使用结构化载荷，删除204不尝试读取JSON，失败不假报成功", async () => {
  const calls = [];
  const api = library(async (url, options) => {
    calls.push({ url, ...options });
    if (options.method === "DELETE") return { ok: true, status: 204, json: () => { throw Error("must not parse"); } };
    if (options.method === "PATCH") return { ok: false, status: 422, json: async () => ({ detail: "实体名称不能为空" }) };
    return { ok: true, status: 201, json: async () => ({ id: "new" }) };
  });
  await api.createKnowledgeEntity({ kind: "product", name: "产品甲" });
  assert.deepEqual(JSON.parse(calls[0].body), { kind: "product", name: "产品甲" });
  assert.equal(calls[0].url, "/api/knowledge/entities");
  await api.deleteKnowledgeEntity("product:a/b");
  assert.equal(calls[1].url, "/api/knowledge/entities/product%3Aa%2Fb");
  await assert.rejects(api.updateKnowledgeEntity("p1", { name: " " }), /实体名称不能为空/);
  assert.equal(api.knowledgeExportUrl("csv"), "/api/knowledge/export?format=csv");
});

function text(node) { if (Array.isArray(node)) return node.map(text).join(""); if (node == null || typeof node === "boolean") return ""; return typeof node === "object" ? text(node.props?.children) : String(node); }
function panelHarness() {
  const slots = [], effects = []; let cursor = 0, nodes = [], graphs = [], opened = [];
  const state = (initial) => { const index = cursor++; if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial; return [slots[index], (value) => { slots[index] = typeof value === "function" ? value(slots[index]) : value; }]; };
  const react = { useState: state, useRef: (initial) => state({ current: initial })[0], useEffect(effect, dependencies) { const index = cursor++; const prior = slots[index]; if (!prior || dependencies.some((value, i) => !Object.is(value, prior.dependencies[i]))) { prior?.cleanup?.(); slots[index] = { dependencies }; effects.push(() => { slots[index].cleanup = effect(); }); } } };
  const jsx = (type, props) => ({ type, props }); const blank = () => null;
  const services = { ...pure, getKnowledgeDatabase: async () => fixture(), syncKnowledgeDatabase: async () => fixture() };
  const Component = load("components/workbench/KnowledgeDatabasePanel.tsx", { react, "react/jsx-runtime": { jsx, jsxs: jsx, Fragment: "fragment" }, "lucide-react": new Proxy({}, { get: () => blank }), "@/hooks/useDialogFocus": { __esModule: true, default: blank }, "@/hooks/useKnowledgeIngestion": { __esModule: true, default: () => ({ data: null, error: null, actionError: null, action: null, notice: null, refresh() {}, queueLatest() {}, retry() {} }) }, "./KnowledgeIngestionView": { __esModule: true, default: blank }, "./KnowledgeGraphCanvas": { __esModule: true, default: (props) => { graphs.push(props); return null; } }, "@/lib/knowledgeDatabase": services });
  function visit(node) { if (Array.isArray(node)) return node.forEach(visit); if (!node || typeof node !== "object") return; if (typeof node.type === "function") return visit(node.type(node.props)); nodes.push(node); visit(node.props?.children); }
  const h = { render() { cursor = 0; nodes = []; graphs = []; visit(Component.default({ onClose() {}, onOpenLocalSource: (ref) => opened.push(ref) })); effects.splice(0).forEach((effect) => effect()); }, async settle() { await new Promise(setImmediate); h.render(); }, find: (type, label) => nodes.find((node) => node.type === type && text(node) === label), nodes: () => nodes, graphs: () => graphs, opened, text: () => nodes.map(text).join(" ") };
  h.render(); return h;
}
test("产品与技术图谱分别选取对应类型作为中心，并可展开全部孤立实体", async () => {
  const h = panelHarness(); await h.settle();
  assert.deepEqual(plain(h.graphs()[0].entities.map((item) => item.id)), ["p1", "t1", "r1"]);
  assert.match(h.text(), /型号、组件谱系待补充/);
  h.find("button", "查看全部").props.onClick(); h.render();
  assert.equal(h.graphs()[0].entities.length, 5);
  h.find("button", "聚焦关联").props.onClick(); h.render();
  h.find("button", "技术图谱").props.onClick(); h.render();
  assert.equal(h.graphs()[0].view, "technologies");
  assert.equal(h.graphs()[0].selectedEntityId, "t1");
  assert.deepEqual(plain(h.graphs()[0].entities.map((item) => item.id)), ["p1", "t1"]);
});
test("图谱点击连线能读独立关系详情与引句，原文按钮使用本地引用", async () => {
  const h = panelHarness(); await h.settle();
  h.graphs()[0].onSelectRelation("x1"); h.render();
  assert.match(h.text(), /关系详情/); assert.match(h.text(), /Exact source quotation/);
  const source = h.nodes().find((node) => node.type === "button" && text(node) === "本地原文");
  source.props.onClick(); assert.deepEqual(h.opened, ["local://library/document"]);
});
test("实体表和关系表读取同一组实体关系，搜索不会困在初始图谱范围", async () => {
  const h = panelHarness(); await h.settle();
  h.find("button", "实体表").props.onClick(); h.render();
  assert.match(h.text(), /远端产品乙/); assert.match(h.text(), /外部项目/);
  h.nodes().find((node) => node.type === "input" && node.props["aria-label"] === "搜索数据库实体与关系").props.onChange({ target: { value: "远端" } }); h.render();
  assert.match(h.text(), /当前显示 1 个实体/);
  h.find("button", "关系表").props.onClick(); h.render();
  assert.match(h.text(), /当前显示 2 条关系/);
  assert.doesNotMatch(h.text(), /完整产品谱系已完成|独立验证通过/);
});
