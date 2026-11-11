/* eslint-disable @typescript-eslint/no-require-imports -- Node tests exercise TSX handlers without a browser. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

function load(file, dependencies = {}, globals = {}) {
  const exports = {};
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "../src", file), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText;
  vm.runInNewContext(code, { exports, require: (name) => dependencies[name] ?? require(name), ...globals });
  return exports;
}

const connection = load("lib/workbench.ts", { "./api": { API_BASE: "" } });
const conversation = load("lib/conversation.ts");
const settings = () => ({
  engine: "openai",
  openai: { model: "gpt-4o", api_base: "https://api.openai.com/v1", api_key_configured: true, api_key: "must-never-be-displayed" },
  deepseek: { model: "deepseek-chat", api_base: "https://api.deepseek.com", api_key_configured: true },
  ollama: { model: "qwen3:8b", api_base: "http://localhost:11434" },
  cli: { model: "gpt-6-luna", available: true },
  engines: connection.ENGINE_IDS.map((id) => ({ id, available: true, message: "已配置" })),
  search: { provider: "ddgs", browser_backend: "auto", web_search_model: "gpt-4o", web_search_allowed_domains: [], openai_key_configured: true, serper_key_configured: false, tavily_key_configured: false },
  ollama_models: [],
});

function text(node) {
  if (Array.isArray(node)) return node.map(text).join("");
  if (node == null || typeof node === "boolean") return "";
  return typeof node === "object" ? text(node.props?.children) : String(node);
}

function harness(file, props, dependencies = {}) {
  const slots = [];
  let slot = 0;
  let nodes = [];
  const blank = () => null;
  const react = {
    useState(initial) {
      const index = slot++;
      if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial;
      return [slots[index], (value) => { slots[index] = typeof value === "function" ? value(slots[index]) : value; }];
    },
    useRef: (current) => ({ current }),
    useEffect() {},
  };
  const jsx = (type, props) => ({ type, props });
  const Component = load(file, {
    react,
    "react/jsx-runtime": { jsx, jsxs: jsx, Fragment: "fragment" },
    "lucide-react": new Proxy({}, { get: () => blank }),
    "@/hooks/useDialogFocus": { __esModule: true, default: blank },
    ...dependencies,
  }).default;
  function visit(node) {
    if (Array.isArray(node)) return node.forEach(visit);
    if (!node || typeof node !== "object") return;
    if (typeof node.type === "function") return visit(node.type(node.props));
    nodes.push(node);
    visit(node.props?.children);
  }
  function render() { slot = 0; nodes = []; visit(Component(props)); }
  render();
  return {
    render,
    nodes: () => nodes,
    button: (label) => nodes.find((node) => node.type === "button" && text(node) === label),
    input: (label) => {
      const field = nodes.find((node) => node.type === "label" && text(node.props.children[0]) === label);
      return field.props.children.find((child) => child?.type === "input");
    },
    flush: () => new Promise(setImmediate),
  };
}

test("OpenAI settings expose four engines and keep configured keys out of inputs", () => {
  const h = harness("components/workbench/WorkbenchSettings.tsx", {
    settings: settings(), engine: "openai", onClose() {}, onSaved() {},
  }, { "@/lib/workbench": connection });
  assert.deepEqual(h.nodes().filter((node) => node.props?.role === "tab").map(text), ["DeepSeek API", "OpenAI API", "Ollama", "CLI · 6-luna", "联网检索"]);
  assert.equal(h.input("API 密钥").props.type, "password");
  assert.equal(h.input("API 密钥").props.value, "");
  assert.equal(h.input("API 密钥").props.placeholder, "已配置 · 留空保留原密钥");
  assert.equal(h.input("模型名称").props.value, "gpt-4o");
  assert.equal(h.input("服务地址").props.value, "https://api.openai.com/v1");
  assert.doesNotMatch(JSON.stringify(h.nodes()), /must-never-be-displayed/);
});

test("saving OpenAI credentials preserves other engines, probes OpenAI and clears the input", async () => {
  const saves = [];
  const probes = [];
  const props = { settings: settings(), engine: "openai", onClose() {}, onSaved(next) { props.settings = next; } };
  const h = harness("components/workbench/WorkbenchSettings.tsx", props, {
    "@/lib/workbench": {
      ...connection,
      async putWorkbenchSettings(patch) { saves.push(JSON.parse(JSON.stringify(patch))); return settings(); },
      async probeEngine(engine) { probes.push(engine); return { ok: true, message: "OpenAI 连接成功" }; },
    },
  });
  h.input("API 密钥").props.onChange({ target: { value: "user-entered-test-key" } });
  h.render();
  h.button("保存并测试").props.onClick();
  await h.flush();
  h.render();
  assert.equal(saves[0].engine, "openai");
  assert.equal(saves[0].openai.api_key, "user-entered-test-key");
  assert.equal(saves[0].deepseek.model, "deepseek-chat");
  assert.equal(saves[0].deepseek.api_key, "");
  assert.equal(saves[0].ollama.model, "qwen3:8b");
  assert.equal(saves[0].search.provider, "ddgs");
  assert.deepEqual(probes, ["openai"]);
  assert.equal(h.input("API 密钥").props.value, "");
  h.button("保存配置").props.onClick();
  await h.flush();
  assert.equal(saves[1].openai.api_key, "");
  assert.deepEqual(probes, ["openai"]);
});

test("OpenAI 联网搜索使用独立模型、限定网站及已保存密钥，不改变分析模型", async () => {
  const probes = [], saves = [];
  const h = harness("components/workbench/WorkbenchSettings.tsx", {
    settings: settings(), engine: "openai", onClose() {}, onSaved() {},
  }, {
    "@/lib/workbench": {
      ...connection,
      async probeSearch(...args) { probes.push(JSON.parse(JSON.stringify(args))); return { ok: true, message: "搜索成功", results: [], elapsed_s: 1 }; },
      async putWorkbenchSettings(patch) { saves.push(JSON.parse(JSON.stringify(patch))); return settings(); },
    },
  });
  h.button("联网检索").props.onClick(); h.render();
  const providers = h.nodes().filter((node) => node.type === "button" && "aria-pressed" in node.props);
  assert.deepEqual(providers.map(text), ["OpenAI · web_search复用 OpenAI 配置", "免密钥 · DDGS无需密钥", "Serper需要密钥", "Tavily需要密钥"]);
  providers[0].props.onClick(); h.render();
  assert.equal(h.input("联网搜索模型").props.value, "gpt-4o");
  assert.ok(!h.nodes().some((node) => node.type === "input" && node.props.type === "password"));
  assert.match(h.nodes().map(text).join(" "), /搜索回答不作为原文保存/);
  assert.match(h.nodes().map(text).join(" "), /测试会调用 OpenAI 的联网搜索服务，使用已保存的密钥/);
  h.input("联网搜索模型").props.onChange({ target: { value: "gpt-4.1" } }); h.render();
  h.input("限定网站（可选）").props.onChange({ target: { value: "ANDURIL.COM, github.com，anduril.com" } }); h.render();
  h.button("测试 OpenAI 联网搜索").props.onClick(); await h.flush(); h.render();
  assert.deepEqual(probes, [["openai_web_search", "", "Python official website", "gpt-4.1", ["anduril.com", "github.com"]]]);
  h.button("保存配置").props.onClick(); await h.flush(); h.render();
  assert.equal(saves[0].engine, "openai");
  assert.equal(saves[0].openai.model, "gpt-4o");
  assert.equal(saves[0].openai.api_key, "");
  assert.equal(saves[0].search.web_search_model, "gpt-4.1");
  assert.deepEqual(saves[0].search.web_search_allowed_domains, ["anduril.com", "github.com"]);
  assert.equal(saves[0].search.provider, "openai_web_search");
  h.button("前往 OpenAI 配置").props.onClick(); h.render();
  assert.equal(h.input("模型名称").props.value, "gpt-4o");
  assert.equal(h.input("API 密钥").props.value, "");
});

test("OpenAI 搜索缺少已保存密钥时引导到 OpenAI 配置页", () => {
  const config = settings();
  config.search.provider = "openai_web_search";
  config.search.openai_key_configured = false;
  const h = harness("components/workbench/WorkbenchSettings.tsx", {
    settings: config, engine: "openai", onClose() {}, onSaved() {},
  }, { "@/lib/workbench": connection });
  h.button("联网检索").props.onClick(); h.render();
  assert.equal(h.button("测试 OpenAI 联网搜索").props.disabled, true);
  assert.match(h.nodes().map(text).join(" "), /尚未配置 OpenAI 密钥/);
  h.button("前往 OpenAI 配置").props.onClick(); h.render();
  assert.equal(h.input("API 密钥").props.value, "");
  assert.doesNotMatch(JSON.stringify(h.nodes()), /must-never-be-displayed/);
});

test("search probe sends the draft search model and domains with a 75 second deadline", async () => {
  const calls = [], deadlines = [];
  const api = load("lib/workbench.ts", { "./api": { API_BASE: "" } }, {
    AbortSignal: { timeout(ms) { deadlines.push(ms); return "fake-signal"; } },
    async fetch(url, options) { calls.push({ url, options }); return { ok: true, json: async () => ({ ok: true }) }; },
  });
  await api.probeSearch("openai_web_search", "", "public research", "gpt-4o", ["anduril.com"]);
  assert.deepEqual(deadlines, [75000]);
  assert.equal(calls[0].url, "/api/workbench/search-probe");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    provider: "openai_web_search", api_key: "", query: "public research",
    web_search_model: "gpt-4o", web_search_allowed_domains: ["anduril.com"],
  });
});

function workbenchHarness(materials = { library_id: "research-pages", library_deleted: false }) {
  const opened = [];
  const switched = [];
  let panelOpens = 0;
  let databaseOpens = 0;
  const blank = () => null;
  const component = new Proxy({}, { get: (_, key) => key === "__esModule" ? true : key === "default" ? blank : blank });
  const dependencies = {
    "next/dynamic": () => blank,
    "@/lib/workbench": connection,
    "@/lib/conversation": conversation,
    "@/lib/api": { async getResearchMaterials() { return materials; } },
    "@/lib/localCitations": { localCitationLabels: () => ({}) },
    "@/lib/innovations": { moduleConfigForTurn: () => ({}) },
    "@/lib/coverageTables": { declaredCoverageTables: () => [] },
    "@/components/settings/SettingsProvider": { useSettings: () => ({ overrides: {}, setOverrides() {} }) },
    "@/components/conversation/Answer": { __esModule: true, default: blank, cleanAnswer: (answer) => answer },
  };
  for (const name of ["@/components/conversation/AnswerActions", "@/components/conversation/OrchestrationCard", "@/components/coverage/CoverageTable", "@/components/coverage/ResultExportMenu", "./AgentWall", "./TraceDrawer", "./InnovationControls", "./InnovationPanel", "@/components/drawer/ResourcePanel", "@/components/drawer/FrontierPanel", "@/components/local/LocalSourceControl"]) dependencies[name] = component;
  const h = harness("components/workbench/WorkbenchView.tsx", {
    turns: [], history: [], historyStatus: "ready", historyQuery: "", running: false,
    modules: [], localLibrary: { documents: [], async openLibrary(id) { opened.push(id); } },
    workbench: { settings: settings(), engine: "openai", async setEngine(id) { switched.push(id); } },
    onOpenKnowledgeBase() { panelOpens++; },
    onOpenKnowledgeDatabase() { databaseOpens++; },
  }, dependencies);
  return { ...h, opened, switched, panelOpens: () => panelOpens, databaseOpens: () => databaseOpens };
}

test("知识数据库有独立入口，直接打开实体关系面板，不切换原文资料库", () => {
  const h = workbenchHarness();
  h.button("知识数据库").props.onClick();
  assert.equal(h.databaseOpens(), 1);
  assert.equal(h.panelOpens(), 0);
  assert.deepEqual(h.opened, []);
});

test("OpenAI has an independent selector and task materials reuse the library panel", async () => {
  const h = workbenchHarness();
  h.button("OpenAI API").props.onClick();
  assert.deepEqual(h.switched, ["openai"]);
  h.button("任务资料库").props.onClick();
  await h.flush();
  assert.deepEqual(h.opened, ["research-pages"]);
  assert.equal(h.panelOpens(), 1);
});

test("deleted and absent task archives show guidance without opening another library", async () => {
  for (const [materials, expected] of [
    [{ library_id: "deleted-pages", library_deleted: true }, "研究网页资料库已删除"],
    [{ library_id: "", library_deleted: false }, "尚无已保存的任务网页资料"],
  ]) {
    const h = workbenchHarness(materials);
    h.button("任务资料库").props.onClick();
    await h.flush();
    h.render();
    assert.equal(h.panelOpens(), 0);
    assert.deepEqual(h.opened, []);
    assert.ok(h.nodes().some((node) => node.props.role === "alert" && text(node).includes(expected)));
  }
});

test("opening an automatically created library refreshes its list and documents", async () => {
  const slots = [];
  let slot = 0;
  let documentReads = 0;
  const react = {
    useState(initial) {
      const index = slot++;
      if (!(index in slots)) slots[index] = initial;
      return [slots[index], (value) => { slots[index] = typeof value === "function" ? value(slots[index]) : value; }];
    },
    useRef(current) {
      const index = slot++;
      return slots[index] ??= { current };
    },
    useEffect() {}, useCallback: (fn) => fn, useMemo: (fn) => fn(),
  };
  const { useLocalLibrary } = load("hooks/useLocalLibrary.ts", {
    react,
    "@/lib/api": {
      async listLocalLibraries() { return [{ library_id: "research-pages", name: "研究网页资料" }]; },
      async listLocalDocuments(id) { assert.equal(id, "research-pages"); documentReads++; return [{ document_id: "page-a", searchable: true }]; },
    },
    "@/lib/sourceSelection": { buildSourceSelection: () => undefined },
  });
  const render = () => { slot = 0; return useLocalLibrary(); };
  let controller = render();
  await controller.openLibrary("research-pages");
  controller = render();
  assert.equal(controller.libraryId, "research-pages");
  assert.equal(controller.libraries[0].name, "研究网页资料");
  await controller.openLibrary("research-pages");
  controller = render();
  assert.equal(documentReads, 1);
  assert.equal(controller.documents[0].document_id, "page-a");
  await assert.rejects(controller.openLibrary("deleted-pages"), /已不存在/);
  assert.equal(render().libraryId, "research-pages");
});
