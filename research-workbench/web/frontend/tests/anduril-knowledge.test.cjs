/* eslint-disable @typescript-eslint/no-require-imports -- Directly exercise reader interactions without a running service. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

function load(file, dependencies = {}) {
  const exports = {};
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "../src", file), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText;
  vm.runInNewContext(code, { exports, URL, AbortController, require: (name) => dependencies[name] ?? require(name) });
  return exports;
}

const readers = load("lib/andurilKnowledge.ts");
const fixture = () => ({
  title: "Anduril 知识", updated_at: "2026-11-02T08:00:00Z", status: "partial", summary: "根据归档原文阅读产品和技术。",
  counts: { products: 999, technologies: 999, routes: 999, maturity: 999, reproduction_units: 999 },
  products: [
    { id: "p1", name: "示例产品 A", family: "感知家族", category: "传感器", overview: "产品 A 的已整理概述。", technical_details: [{ label: "感知方式", text: "资料明确记载的技术细节 A。", source_ids: ["s1"] }], technology_ids: ["t1"], maturity: { stage: "公开介绍", basis: "产品 A 的原文阶段依据。", source_ids: ["s1"] }, limitations: ["参数待补充。"], source_ids: ["s1"] },
    { id: "p2", name: "示例产品 B", family: "平台家族", category: "平台", overview: "产品 B 的已整理概述。", technical_details: [], technology_ids: [], maturity: { stage: "公开试验", basis: "产品 B 的原文阶段依据。", source_ids: ["s2"] }, limitations: [], source_ids: ["s2"] },
  ],
  technologies: [{ id: "t1", name: "示例机制", mechanism: "只来自资料的机制说明。", product_ids: ["p1"], route_ids: ["r1"], source_ids: ["s1"], limitations: [] }],
  routes: [{ id: "r1", name: "示例路线", problem: "路线解决的问题。", mechanism: "路线中的技术机制。", tradeoffs: ["路线的取舍。"], product_ids: ["p2"], source_ids: ["s2"], limitations: [] }],
  reproduction_units: [{ id: "u1", name: "外部参考代码", repo_url: "https://example.com/repository", description: "用于学习机制的开源参考。", related_technology_ids: ["t1"], commit: "", license: "MIT", inputs: ["示例输入"], outputs: ["示例输出"], steps: ["运行示例命令"], acceptance_criteria: ["观察示例输出"], status: "not_run", limitations: ["Anduril 是否采用尚未确认。"], source_ids: ["s1"] }],
  sources: [
    { id: "s1", title: "已归档原文 A", url: "https://example.com/source-a", ref_id: "local://lib/doc/segment", quote: "证据原文 A。", location: "第 1 段" },
    { id: "s2", title: "已归档原文 B", url: "https://example.com/source-b", quote: "证据原文 B。" },
  ],
  gaps: ["继续补充公开技术参数。"], audit: { completed_tasks: 999, secret_internal_payload: "不要展示的审计原始对象" },
});

function content(node) {
  if (Array.isArray(node)) return node.map(content).join("");
  if (node == null || typeof node === "boolean") return "";
  return typeof node === "object" ? content(node.props?.children) : String(node);
}

function harness(fetchKnowledge = async () => fixture()) {
  const slots = [], effects = [];
  let cursor = 0, nodes = [], opened = [];
  const hook = (initial) => {
    const index = cursor++;
    if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial;
    return [slots[index], (value) => { slots[index] = typeof value === "function" ? value(slots[index]) : value; }];
  };
  const react = {
    useState: hook,
    useRef(initial) { return hook({ current: initial })[0]; },
    useEffect(effect, dependencies) {
      const index = cursor++;
      const prior = slots[index];
      if (!prior || dependencies.some((value, i) => !Object.is(value, prior.dependencies[i]))) {
        prior?.cleanup?.();
        slots[index] = { dependencies };
        effects.push(() => { slots[index].cleanup = effect(); });
      }
    },
  };
  const jsx = (type, props) => ({ type, props });
  const blank = () => null;
  const Component = load("components/workbench/AndurilKnowledgePanel.tsx", {
    react, "react/jsx-runtime": { jsx, jsxs: jsx, Fragment: "fragment" },
    "lucide-react": new Proxy({}, { get: () => blank }),
    "@/hooks/useDialogFocus": { __esModule: true, default: blank },
    "@/lib/andurilKnowledge": readers,
    "@/lib/api": { getAndurilKnowledge: fetchKnowledge },
  }).default;
  function visit(node) {
    if (Array.isArray(node)) return node.forEach(visit);
    if (!node || typeof node !== "object") return;
    if (typeof node.type === "function") return visit(node.type(node.props));
    nodes.push(node); visit(node.props?.children);
  }
  const h = {
    render() { cursor = 0; nodes = []; visit(Component({ onClose() {}, onOpenLocalSource: (ref) => opened.push(ref) })); effects.splice(0).forEach((effect) => effect()); },
    async settle() { await new Promise(setImmediate); h.render(); },
    nodes: () => nodes,
    text: () => nodes.filter((node) => typeof node.type === "string").map(content).join(" "),
    find: (type, label) => nodes.find((node) => node.type === type && content(node) === label),
    opened,
  };
  h.render();
  return h;
}

test("知识阅读展示可读内容、实际条目数量及可追溯引句，不展示收集任务统计或审计 JSON", async () => {
  const h = harness();
  assert.match(h.text(), /正在读取/);
  await h.settle();
  assert.match(h.text(), /产品 A 的已整理概述/);
  assert.match(h.text(), /资料明确记载的技术细节 A/);
  assert.match(h.text(), /只来自资料的机制说明/);
  assert.match(h.text(), /证据原文 A/);
  assert.match(h.text(), /2产品条目/);
  assert.doesNotMatch(h.text(), /999|secret_internal_payload|不要展示的审计原始对象/);
  h.find("button", "本地原文").props.onClick();
  assert.deepEqual(h.opened, ["local://lib/doc/segment"]);
  assert.equal(h.find("a", "已归档原文 A").props.href, "https://example.com/source-a");
});

test("点击产品卡片切换详情，搜索后可阅读匹配产品", async () => {
  const h = harness(); await h.settle();
  h.nodes().find((node) => node.type === "button" && content(node).startsWith("平台示例产品 B")).props.onClick(); h.render();
  assert.ok(h.nodes().some((node) => node.type === "aside" && node.props["aria-label"] === "示例产品 B 产品详情"));
  assert.match(h.text(), /产品 B 的原文阶段依据/);
  assert.doesNotMatch(h.text(), /资料明确记载的技术细节 A/);
  h.nodes().find((node) => node.type === "input").props.onChange({ target: { value: "产品 A" } }); h.render();
  assert.match(h.text(), /资料明确记载的技术细节 A/);
  assert.ok(!h.nodes().some((node) => node.type === "aside" && node.props["aria-label"] === "示例产品 B 产品详情"));
});

test("技术路线显示问题、机制和取舍，关联产品可跳回详情", async () => {
  const h = harness(); await h.settle();
  h.find("button", "技术路线").props.onClick(); h.render();
  for (const text of ["路线解决的问题。", "路线中的技术机制。", "路线的取舍。", "只来自资料的机制说明。"]) assert.match(h.text(), new RegExp(text));
  h.find("button", "示例产品 B").props.onClick(); h.render();
  assert.ok(h.nodes().some((node) => node.type === "aside" && node.props["aria-label"] === "示例产品 B 产品详情"));
  h.find("button", "成熟度").props.onClick(); h.render();
  assert.match(h.text(), /公开试验/);
  assert.match(h.text(), /产品 B 的原文阶段依据/);
});

test("开源参考未运行时不冒称实际跑通，版本、输入输出和验收条件可读", async () => {
  const h = harness(); await h.settle();
  h.find("button", "开源单元").props.onClick(); h.render();
  for (const text of ["外部开源参考", "尚未运行", "尚未固定版本", "示例输入", "示例输出", "观察示例输出", "Anduril 是否采用尚未确认。"]) assert.match(h.text(), new RegExp(text));
  assert.doesNotMatch(h.text(), /本机复现通过/);
  assert.equal(h.find("a", "查看开源仓库").props.href, "https://example.com/repository");
  assert.equal(readers.reproductionLabel("verified"), "复现状态待确认");
});

test("空视图如实说明，读取失败可重试", async () => {
  let count = 0;
  const h = harness(async () => {
    if (++count === 1) throw new Error("暂时无法读取知识");
    const data = fixture(); data.routes = []; data.technologies = []; data.products = []; data.reproduction_units = []; return data;
  });
  await h.settle();
  assert.match(h.text(), /暂时无法读取知识/);
  h.find("button", "重试读取").props.onClick(); h.render(); await h.settle();
  assert.doesNotMatch(h.text(), /暂时无法读取知识/);
  assert.match(h.text(), /尚未形成有来源支持的可读条目/);
  h.find("button", "开源单元").props.onClick(); h.render();
  assert.match(h.text(), /尚未形成有来源支持的可读条目/);
});

test("知识来源仅允许 HTTP/HTTPS 网页，缺失来源不借用其他条目的依据", () => {
  assert.equal(readers.safeKnowledgeUrl("javascript:alert(1)"), null);
  assert.equal(readers.safeKnowledgeUrl("https://example.com"), "https://example.com");
  assert.equal(readers.safeKnowledgeUrl("https://"), null);
  assert.equal(readers.knowledgeSources(["missing"], fixture().sources).length, 0);
});
