/* eslint-disable @typescript-eslint/no-require-imports */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

const source = fs.readFileSync(path.join(__dirname, "../src/lib/researchGraph.ts"), "utf8");
const exportsObject = {};
vm.runInNewContext(ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports: exportsObject, URL });
const { buildResearchGraph, layoutResearchGraph, researchOperations, STATUS_LABELS } = exportsObject;
const base = () => ({ intent: "真实研究问题", frontier: { questions: [] }, evidence_graph: { nodes: [], edges: [], task_bindings: [] }, coverage_map: { cells: {}, evidence_bindings: {}, tables: {} } });
const event = (data) => ({ type: "trajectory", data });
const evidence = (id, source = "https://example.com/report") => ({ id, source, claim: `事实 ${id}`, confidence: .8, entity: "实体", attribute: "参数", status: "active" });

test("空画布不生成演示任务或伪造过程", () => {
  const graph = buildResearchGraph(null);
  assert.equal(graph.nodes.length, 0);
  assert.equal(graph.edges.length, 0);
  assert.equal(layoutResearchGraph(graph).nodes.length, 0);
});

test("任务依赖、指派来自明确字段，整体结束不改写未完成子任务", () => {
  const state = base();
  state.frontier.questions = [
    { id: "a", question: "先读取", status: "completed", assigned_agent_id: "reader", blocked_by: [] },
    { id: "b", question: "后比较", status: "blocked", blocked_by: ["a", "missing"] },
  ];
  const graph = buildResearchGraph(state, [], "", "tasks", "completed");
  assert.ok(graph.edges.some((edge) => edge.source === "task:a" && edge.target === "task:b" && edge.relation === "dependency"));
  assert.ok(graph.edges.some((edge) => edge.source === "task:a" && edge.target === "agent:reader"));
  assert.equal(graph.nodes.find((node) => node.id === "task:b").status, "incomplete");
  assert.equal(state.frontier.questions[1].status, "blocked");
  assert.ok(graph.nodes.find((node) => node.id === "task:b").details.some((item) => item.label === "原始任务状态" && item.value === "blocked"));
  assert.ok(!graph.nodes.some((node) => node.id.includes("missing")));
});

test("事实到字段必须具有存储绑定，不能仅按实体和属性相同推导", () => {
  const state = base();
  state.evidence_graph.nodes = [evidence("e1"), evidence("e2")];
  state.coverage_map.cells = {
    "table/实体.参数": { value: "7", status: "filled", supporting_evidence_ids: ["e1", "not-recorded"] },
    "other/实体.参数": { value: "7", status: "filled" },
  };
  const graph = buildResearchGraph(state, [], "", "evidence");
  assert.ok(graph.edges.some((edge) => edge.source === "evidence:e1" && edge.target === "cell:table/实体.参数"));
  assert.ok(!graph.edges.some((edge) => edge.target === "cell:other/实体.参数"));
  assert.ok(!graph.edges.some((edge) => edge.source === "evidence:e2" && edge.target.startsWith("cell:")));
  assert.equal(graph.nodes.filter((node) => node.kind === "source").length, 1);
});

test("已排除证据绑定不会绘成有效支撑，冲突关系保留", () => {
  const state = base();
  state.evidence_graph.nodes = [evidence("e1"), evidence("e2")];
  state.evidence_graph.edges = [{ from_id: "e1", to_id: "e2", relation: "conflict" }, { from_id: "missing", to_id: "e2", relation: "support" }];
  state.coverage_map.cells["table/a.b"] = { value: "7", status: "filled" };
  state.coverage_map.evidence_bindings["table/a.b"] = [{ evidence_id: "e1", status: "rejected", value: "8" }];
  const graph = buildResearchGraph(state, [], "", "evidence");
  assert.equal(graph.edges.filter((edge) => edge.relation === "conflict").length, 1);
  assert.ok(graph.edges.some((edge) => edge.relation === "rejected"));
  assert.ok(!graph.edges.some((edge) => edge.relation === "supports_cell"));
});

test("本地阅读和精确引用形成阅读片段关联，不冒充事实证据", () => {
  const ref = "local://lib/document#L1-8";
  const events = [event({ type: "local_read_observed", source_mode: "local", observation: JSON.stringify({ ref_id: ref, file: "项目简报.md", location_label: "L1–8", tool_content: "真实原文片段" }) })];
  const graph = buildResearchGraph(base(), events, `[原文](${ref}) [未读取](local://lib/other#L1)`, "evidence", "completed");
  assert.equal(graph.nodes.filter((node) => node.kind === "reading").length, 1);
  assert.equal(graph.nodes.filter((node) => node.kind === "evidence").length, 0);
  assert.equal(graph.edges.filter((edge) => edge.relation === "cites").length, 1);
  assert.equal(graph.unmatchedCitations, 1);
  assert.ok(!graph.nodes.some((node) => node.id.includes("other")));
  assert.ok(graph.nodes.find((node) => node.kind === "reading").details.some((item) => item.value === "真实原文片段"));
});

test("没有精确阅读记录的本地文档其他位置不能建立引用边", () => {
  const events = [event({ type: "local_read_observed", observation: { ref_id: "local://lib/doc#L1-8", file: "资料" } })];
  const graph = buildResearchGraph(base(), events, "[不同位置](local://lib/doc#L20-30)", "evidence");
  assert.equal(graph.edges.filter((edge) => edge.relation === "cites").length, 0);
  assert.equal(graph.unmatchedCitations, 1);
});

test("损坏阅读事件和未声明因果不生成节点或边", () => {
  const events = [event({ type: "local_read_observed", observation: "{broken" }), event({ type: "agent_final", reasoning: "事实e1支持e2，任务A依赖B" })];
  const graph = buildResearchGraph(null, events, "", "evidence");
  assert.equal(graph.nodes.length, 0);
  assert.equal(graph.edges.length, 0);
});

test("工具开始和返回按执行者与调用标识合并，中断不视作成功", () => {
  const records = [
    event({ type: "tool_call_started", agent: "one", tool: "open", tool_call_id: "call", args: { url: "a" } }),
    event({ type: "tool_call_started", agent: "two", tool: "open", tool_call_id: "call" }),
    event({ type: "step", agent: "one", tool_call_id: "call", action: { name: "open" }, observation: "原文" }),
  ];
  const operations = researchOperations(records, "error");
  assert.equal(operations.length, 2);
  assert.equal(operations[0].status, "completed");
  assert.equal(operations[1].status, "interrupted");
});

test("聚合只合并真实节点和关联，展开恢复全部节点", () => {
  const state = base();
  state.evidence_graph.nodes = Array.from({ length: 12 }, (_, index) => evidence(`e${index}`));
  const graph = buildResearchGraph(state, [], "", "evidence");
  const folded = layoutResearchGraph(graph);
  const group = folded.nodes.find((node) => node.kind === "group");
  assert.equal(group.memberIds.length, 12);
  assert.equal(folded.edges[0].count, 12);
  const expanded = layoutResearchGraph(graph, new Set([group.groupKey]));
  assert.equal(expanded.nodes.length, graph.nodes.length);
  assert.equal(expanded.edges.length, graph.edges.length);
  for (const node of expanded.nodes) {
    assert.ok(node.x >= 0 && node.x + node.width <= expanded.width);
    assert.ok(node.y >= 0 && node.y + node.height <= expanded.height);
  }
});

test("网页答案引用仅关联来源，不宣称该来源每条事实都被答案采用", () => {
  const state = base();
  state.evidence_graph.nodes = [evidence("e1"), evidence("e2")];
  const graph = buildResearchGraph(state, [], "[来源](https://example.com/report)", "evidence");
  assert.ok(graph.edges.some((edge) => edge.source === "source:https://example.com/report" && edge.target === "answer"));
  assert.ok(!graph.edges.some((edge) => edge.source.startsWith("evidence:") && edge.target === "answer"));
});

test("窄画布纵向分层且保持280单位节点宽度，关联不因布局改变", () => {
  const state = base();
  state.evidence_graph.nodes = [evidence("e1")];
  const graph = buildResearchGraph(state, [], "[来源](https://example.com/report)", "evidence");
  const wide = layoutResearchGraph(graph);
  const narrow = layoutResearchGraph(graph, new Set(), { narrow: true });
  assert.equal(narrow.width, 360);
  assert.equal(narrow.nodes.length, 3);
  assert.ok(narrow.nodes.every((node) => node.width === 280 && node.x === 40));
  assert.ok(narrow.nodes[1].y > narrow.nodes[0].y + narrow.nodes[0].height);
  assert.ok(narrow.nodes[2].y > narrow.nodes[1].y + narrow.nodes[1].height);
  assert.equal(narrow.edges.length, wide.edges.length);
  // A 468px panel renders each node about 364px wide, rather than fitting the
  // entire wide graph into its short height and shrinking text to a few pixels.
  assert.ok(narrow.nodes[0].width * 468 / narrow.width > 220);
});

test("未知终态简写为已结束，内部保留interrupted并提供状态来源", () => {
  const graph = buildResearchGraph(base(), [event({ type: "task_start", agent: "reader", query: "测试" })], "", "tasks", "completed");
  const agent = graph.nodes.find((node) => node.kind === "agent");
  assert.equal(agent.status, "interrupted");
  assert.equal(STATUS_LABELS[agent.status], "已结束");
  assert.ok(agent.details.some((item) => item.label === "状态来源"));
});

test("本地调度与增强工具显示中文，原始名称和执行结果保留在详情", () => {
  const toolNames = ["create_schema", "enqueue_tasks", "check_agents", "read_evidence", "list_frontier", "inspect_table", "stop_task", "bind_evidence", "extract_observation", "read_coverage", "resolve_cell_provenance", "read_task_report", "write_section"];
  const graph = buildResearchGraph(base(), toolNames.map((tool, index) => event({ type: "step", agent: "orchestrator", tool_call_id: `call-${index}`, action: { name: tool }, observation: "已保存的返回结果" })), "", "tasks", "completed");
  const operations = graph.nodes.filter((node) => node.kind === "operation");
  assert.equal(operations.length, toolNames.length);
  for (const [index, operation] of operations.entries()) {
    assert.match(operation.label, /[\u3400-\u9fff]/);
    assert.equal(operation.status, "completed");
    assert.ok(operation.details.some((item) => item.label === "原始工具名称" && item.value === toolNames[index]));
    assert.ok(operation.details.some((item) => item.value === "已保存的返回结果"));
  }
});

test("结束后的未完成任务停止显示进行中，双视图保留原始状态和关联", () => {
  const s = base();
  s.frontier.questions = ["running", "pending", "blocked", "exploring", "completed", "cancelled"].map((status, index) => ({ id: String(index), question: `任务${index}`, status }));
  s.evidence_graph.nodes = [evidence("e1")];
  s.evidence_graph.task_bindings = [{ task_id: "0", evidence_id: "e1", relation: "supports" }];
  const live = buildResearchGraph(s, [], "", "tasks", "running");
  assert.equal(live.nodes.find((node) => node.id === "task:0").status, "running");
  for (const mode of ["tasks", "evidence"]) {
    const finished = buildResearchGraph(s, [], "部分回答", mode, "error");
    assert.equal(finished.nodes.find((node) => node.id === "task:0").status, "incomplete");
    assert.ok(finished.nodes.find((node) => node.id === "task:0").details.some((item) => item.label === "原始任务状态" && item.value === "running"));
    if (mode === "tasks") {
      assert.ok(finished.nodes.filter((node) => ["task:1", "task:2", "task:3"].includes(node.id)).every((node) => node.status === "incomplete"));
      assert.equal(finished.nodes.find((node) => node.id === "task:4").status, "completed");
      assert.equal(finished.nodes.find((node) => node.id === "task:5").status, "cancelled");
    } else {
      assert.equal(finished.nodes.find((node) => node.id === "answer").status, "incomplete");
      assert.ok(finished.edges.some((edge) => edge.target === "task:0"));
    }
  }
  assert.equal(s.frontier.questions[0].status, "running");
  assert.equal(STATUS_LABELS.incomplete, "未完成");
});

test("无独立阅读事件的本地事实仍可精确关联回答引用，不能推断引用了所有事实", () => {
  const ref = `local://library/document?rev=${"a".repeat(64)}#exact-locator`;
  const s = base();
  s.evidence_graph.nodes = [evidence("e1", ref), evidence("e2", ref)];
  const graph = buildResearchGraph(s, [], `[原文](${ref})`, "evidence", "completed");
  assert.equal(graph.unmatchedCitations, 0);
  assert.ok(graph.edges.some((edge) => edge.source === `source:${ref.split("#")[0]}` && edge.target === "answer" && edge.relation === "cites"));
  assert.ok(!graph.edges.some((edge) => edge.source.startsWith("evidence:") && edge.target === "answer"));
  assert.equal(graph.nodes.filter((node) => node.kind === "reading").length, 0);
});

test("本地引用只解码显示转义，不跨文档版本或位置匹配", () => {
  const ref = `local://library/document?rev=${"a".repeat(64)}#exact-locator`;
  const s = base();
  s.evidence_graph.nodes = [evidence("e1", ref)];
  const escaped = buildResearchGraph(s, [], `[原文](${ref.replace("#", "&#35;")})`, "evidence", "completed");
  assert.equal(escaped.unmatchedCitations, 0);
  for (const other of [ref.replace("exact-locator", "another-locator"), ref.replace("a".repeat(64), "b".repeat(64)), ref.replace("/document?", "/another?")]) {
    const graph = buildResearchGraph(s, [], `[另一位置](${other})`, "evidence", "completed");
    assert.equal(graph.unmatchedCitations, 1);
    assert.ok(!graph.edges.some((edge) => edge.target === "answer"));
  }
});
