/* eslint-disable @typescript-eslint/no-require-imports -- 使用真实页面和回调验证重试请求。 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

const plain = (value) => JSON.parse(JSON.stringify(value));
const sources = (mode = "hybrid", selection = "documents", ids = ["original-doc"]) => ({
  mode, local: { library_id: "original-library", selection, document_ids: ids, model_access: true },
});

function find(node, predicate) {
  if (!node || typeof node !== "object") return undefined;
  if (predicate(node)) return node;
  for (const child of [node.props?.children ?? node].flat()) {
    if (child === node) continue;
    const match = find(child, predicate);
    if (match) return match;
  }
}

function harness() {
  const states = new Map();
  let owner;
  let cursor;
  let session = { status: "idle", events: [], workers: [] };
  let syncSession;
  const react = {
    useState(initial) {
      const slots = states.get(owner);
      const index = cursor++;
      if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial;
      return [slots[index], (update) => {
        slots[index] = typeof update === "function" ? update(slots[index]) : update;
      }];
    },
    useRef: (current) => react.useState(() => ({ current }))[0],
    useCallback: (fn) => fn,
    useMemo: (fn) => fn(),
    useEffect: (callback, dependencies) => { if (dependencies?.[0] === session) syncSession = callback; },
  };
  const render = (component, props = {}) => {
    owner = component;
    cursor = 0;
    if (!states.has(owner)) states.set(owner, []);
    return component(props);
  };
  const requests = [];
  const startCallbacks = [];
  let history;
  const localLibrary = {
    selection: sources(),
    restoreSelection(value) { localLibrary.selection = value; },
  };
  const dependencies = {
    react,
    "next/dynamic": { default: () => "AndurilKnowledgePanel" },
    "react/jsx-runtime": { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) },
    "lucide-react": {},
    "@/hooks/useWorkbench": { useWorkbench: () => ({ engine: "ollama", settings: null }) },
    "@/hooks/useSearch": { useSearch: () => ({
      session,
      run: (request, options) => { requests.push(plain(request)); startCallbacks.push(options?.onStarted); }, reset() {},
    }) },
    "@/hooks/useLocalLibrary": { useLocalLibrary: () => localLibrary },
    "@/hooks/useLiveFollowUps": { useLiveFollowUps: () => ({ delivered: [], hasSnapshot: false }) },
    "@/lib/api": { loadHistory: async () => history, listHistory: async () => [] },
    "@/lib/activityPreferences": { useActivityPreferences: () => ({ width: 360, tab: "activity" }) },
    "@/components/settings/SettingsProvider": { useSettings: () => ({ overrides: {}, notify() {} }) },
    "./Answer": { default: "Answer", cleanAnswer: (answer) => answer },
  };
  function load(file) {
    const output = {};
    vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, "../src", file), "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
    }).outputText, {
      exports: output, structuredClone,
      require(name) {
        if (name in dependencies) return dependencies[name];
        if (name.startsWith("@/lib/")) return load(`${name.slice(2)}.ts`);
        if (name.startsWith("@/components/") || name.startsWith("./")) return { default: name.split("/").pop() };
        throw new Error(`Unexpected import: ${name}`);
      },
    });
    return output;
  }
  const Home = load("app/legacy/page.tsx").default;
  const page = () => render(Home);
  const conversation = () => find(page(), (node) => node.type === "WorkbenchView").props;
  return {
    requests, localLibrary, conversation,
    started(response) { startCallbacks.at(-1)?.(response); },
    settle(result, liveAnswer = null) {
      session = { status: result.status, sessionId: "active", events: [], workers: [], result,
        liveState: result.search_state, liveAnswer, elapsed: result.elapsed_s, error: result.error };
      page();
      syncSession();
    },
    submit(selection, modules) {
      find(page(), (node) => node.type === "WorkbenchView").props.onSubmit("总结所选文献", { sources: selection, modules });
    },
    async restore(turns, metadata = {}) {
      history = { session_id: "saved", status: "error", answer: "", events: [], turns, ...metadata };
      find(page(), (node) => node.type === "WorkbenchView").props.onSelect("saved");
      await new Promise(setImmediate);
    },
    retry(index = 0) {
      const props = conversation();
      props.onRerun(props.turns[index]);
      return requests.at(-1);
    },
  };
}

test("提交后立即失败的重试保留文档快照，不使用后来更改的选择", () => {
  const h = harness();
  const original = sources();
  h.submit(original);
  const turn = h.conversation().turns[0];
  turn.status = "error";
  turn.error = "请求失败";
  original.local.document_ids.push("later-doc");
  h.localLibrary.selection = sources("local", "documents", ["other-doc"]);
  const request = h.retry();
  assert.deepEqual(request.sources, sources());
  assert.equal(request.follow_up_to, undefined);
  assert.equal(request.history, undefined);
  assert.deepEqual(plain(h.localLibrary.selection), sources());
});

test("历史失败重试保留显式文档 ID，包括暂时不可用的文档", async () => {
  const h = harness();
  await h.restore([{ query: "历史问题", answer: "", search_state: { source_scope: {
    mode: "hybrid", library_id: "original-library", selection: "documents",
    document_ids: ["original-doc", "unavailable-doc"], documents: [{ document_id: "original-doc" }],
  } } }]);
  assert.deepEqual(h.retry().sources, sources("hybrid", "documents", ["original-doc", "unavailable-doc"]));
});

for (const mode of ["local", "hybrid"]) {
  test(`${mode} 历史整库范围不会缩成当前快照中的文档或丢失空库`, async () => {
    for (const documents of [[], [{ document_id: "original-doc" }]]) {
      const h = harness();
      await h.restore([{ query: "检索整库", answer: "", search_state: { source_scope: {
        mode, library_id: "original-library", selection: "library", document_ids: [], documents,
      } } }]);
      const expected = sources(mode, "library", []);
      assert.deepEqual(plain(h.localLibrary.selection), expected);
      assert.deepEqual(h.retry().sources, expected);
    }
  });
}

test("重跑历史中的旧回答使用该轮文档，兼容只有 documents 的旧快照", async () => {
  const h = harness();
  await h.restore([
    { query: "旧问题", answer: "旧回答", search_state: { source_scope: {
      mode: "local", library_id: "original-library", documents: [{ document_id: "original-doc" }],
    } } },
    { query: "新问题", answer: "", search_state: { source_scope: { mode: "web", documents: [] } } },
  ]);
  const request = h.retry(0);
  assert.equal(request.query, "旧问题");
  assert.deepEqual(request.sources, sources("local"));
  assert.equal(request.follow_up_to, undefined);
});

for (const mode of ["web", "hybrid"]) {
  test(`${mode} 原本未附带本地库的请求重试后仍不附带`, () => {
    const h = harness();
    h.submit({ mode });
    Object.assign(h.conversation().turns[0], { status: "error", error: "失败" });
    assert.deepEqual(h.retry().sources, { mode });
  });
}

test("历史结果保留本轮模型，最新会话模型不会覆盖早期轮次", async () => {
  const h = harness();
  const distribution = { main: { model: "deepseek-chat", provider: "deepseek", profile: "main" } };
  await h.restore([
    { query: "旧问题", answer: "旧回答", search_state: null, model_distribution: distribution },
    { query: "新问题", answer: "新回答", search_state: null },
  ], { status: "completed", engine: "cli", engine_model: "gpt-6-luna" });
  const [older, latest] = h.conversation().turns;
  assert.equal(older.meta.engine, undefined);
  assert.equal(older.meta.engineModel, undefined);
  assert.deepEqual(plain(older.meta.modelDistribution), distribution);
  assert.equal(latest.meta.engine, "cli");
  assert.equal(latest.meta.engineModel, "gpt-6-luna");
  // The mocked next-task selector is Ollama; it must not determine this result's identity.
  assert.equal(h.conversation().workbench.engine, "ollama");
});

const moduleConfig = (enabled) => ({ schema_version: 1, enabled, implementation_version: "1" });
test("新研究模块默认关闭，显式选择会随请求和本轮快照保存", () => {
  const h = harness();
  assert.deepEqual(plain(h.conversation().modules), []);
  h.conversation().onModulesChange(["context_memory"]);
  h.submit(sources());
  assert.deepEqual(h.requests.at(-1).modules, ["context_memory"]);
  assert.deepEqual(plain(h.conversation().turns[0].moduleConfig), moduleConfig(["context_memory"]));
  assert.equal(h.conversation().modulesLocked, true);
  h.conversation().onNew();
  assert.deepEqual(plain(h.conversation().modules), []);
  assert.equal(h.conversation().modulesLocked, false);
});

test("恢复历史与续问沿用保存组合，新的选择不能冒充历史模块", async () => {
  const h = harness();
  h.conversation().onModulesChange(["context_memory"]);
  await h.restore([{ query: "已有研究", answer: "回答", search_state: null }], {
    status: "completed", module_config: moduleConfig(["evidence_revision"]),
  });
  assert.deepEqual(plain(h.conversation().modules), ["evidence_revision"]);
  assert.equal(h.conversation().modulesLocked, true);
  h.submit(sources(), ["context_memory"]);
  assert.equal(h.requests.at(-1).follow_up_to, "saved");
  assert.deepEqual(h.requests.at(-1).modules, ["evidence_revision"]);
});

test("旧历史未记录模块时续问不伪造组合；重跑使用原轮配置", async () => {
  const legacy = harness();
  await legacy.restore([{ query: "旧问题", answer: "回答", search_state: null }], { status: "completed" });
  legacy.submit(sources());
  assert.equal(legacy.requests.at(-1).modules, undefined);
  const h = harness();
  await h.restore([{ query: "旧问题", answer: "回答", search_state: { module_config: moduleConfig(["context_memory"]) } }]);
  assert.deepEqual(h.retry().modules, ["context_memory"]);
  assert.equal(h.requests.at(-1).follow_up_to, undefined);
});

test("启动响应的实际模型覆盖本轮预测值，不修改下一任务默认选择", () => {
  const h = harness();
  h.submit(sources());
  h.started({ session_id: "active", engine: "ollama", engine_model: "inherited-model", module_config: moduleConfig([]) });
  assert.equal(h.conversation().turns[0].meta.engineModel, "inherited-model");
  assert.equal(h.conversation().workbench.engine, "ollama");
});

test("失败终态采纳已保存部分答案和证据，但保留错误状态与提示", () => {
  const h = harness();
  h.submit(sources());
  const savedState = { intent: "部分研究", evidence_graph: { nodes: [{ id: "saved-evidence" }] } };
  h.settle({ status: "error", answer: "已保存的部分正文", partial: true, search_state: savedState,
    engine: "ollama", engine_model: "actual-model", elapsed_s: 8, error: "后续步骤失败" }, "较早的流式预览");
  const turn = h.conversation().turns[0];
  assert.equal(turn.status, "error");
  assert.equal(turn.answer, "已保存的部分正文");
  assert.equal(turn.error, "后续步骤失败");
  assert.deepEqual(plain(turn.searchState), savedState);
  assert.equal(turn.meta.engineModel, "actual-model");
  assert.equal(turn.meta.elapsed, 8);
  h.settle({ status: "error", answer: "", error: "没有保存正文" }, "不得冒充最终保存的预览");
  assert.equal(h.conversation().turns[0].answer, "");
});
