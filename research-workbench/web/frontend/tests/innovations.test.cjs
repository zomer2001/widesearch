/* eslint-disable @typescript-eslint/no-require-imports */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
function load(file) {
  const exports = {};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, "../src", file), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText, { exports, URL, require(name) {
    if (name === "@/lib/innovations") return load("lib/innovations.ts");
    return require(name);
  } });
  return exports;
}
const { innovationActivity, innovationSourceHref } = load("lib/innovations.ts");
const Panel = load("components/workbench/InnovationPanel.tsx").default;
const config = (...enabled) => ({ schema_version: 1, enabled, implementation_version: "1" });
const state = () => ({ module_config: config(), evidence_graph: { nodes: [], claim_annotations: {} }, coverage_map: { evidence_comparisons: {} } });

test("开启模块不等于触发，内部依赖追踪不冒充用户开启另一个模块", () => {
  const s = state();
  s.module_config = config("context_memory");
  s.p1 = { enabled: true, artifacts: { a: {} }, executions: { b: {} } };
  s.p2 = { enabled: true };
  const view = innovationActivity(s);
  assert.equal(view.status("evidence_revision"), "disabled");
  assert.equal(view.status("context_memory"), "enabled");
  assert.equal(innovationActivity(null).status("context_memory"), "unknown");
});

test("适用性标注与规则比较来自真实字段，不算成已确认修订", () => {
  const s = state();
  s.module_config = config("evidence_revision");
  s.evidence_graph.claim_annotations.e1 = { evidence_id: "e1", verified: true };
  s.coverage_map.evidence_comparisons.cell = { rule: { method: "p1_rule" }, ordinary: { method: "llm" } };
  s.p1 = { events: { pending: { event: { confirmed: false } } } };
  const view = innovationActivity(s);
  assert.equal(view.status("evidence_revision"), "triggered");
  assert.equal(view.annotations.length, 1);
  assert.equal(view.comparisons.length, 1);
  assert.equal(view.revisions.length, 0);
});

test("保留目标与压缩分开统计，失败摘要不算压缩成功", () => {
  const s = state();
  s.module_config = config("context_memory");
  s.p2 = { goal: { original_goal: "保存的目标", user_messages: [] }, compaction_metrics: [
    { outcome: "rejected", summary_model_calls: 1 },
    { outcome: "published", summary_model_calls: 0, rebuilt: true },
    { outcome: "published", summary_model_calls: 1, before_tokens: 100, after_tokens: 60 },
  ] };
  const view = innovationActivity(s);
  assert.equal(view.status("context_memory"), "triggered");
  assert.equal(view.compressed.length, 1);
  assert.equal(view.rebuilt.length, 1);
  assert.equal(view.rejected.length, 1);
  assert.equal(view.experiences.length, 0);
});

test("历史快照优先于当前传入选择，原文链接仅允许已支持的来源协议", () => {
  const s = state();
  assert.equal(innovationActivity(s, config("context_memory")).status("context_memory"), "disabled");
  assert.equal(innovationSourceHref("javascript:alert(1)"), undefined);
  assert.equal(innovationSourceHref("file:///etc/passwd"), undefined);
  assert.equal(innovationSourceHref("local://library/document#L1"), "local://library/document#L1");
  assert.equal(innovationSourceHref("https://example.com/source"), "https://example.com/source");
});

test("增强视图展示累计实际记录，未知反馈保持未知且提供原文入口", () => {
  const s = state();
  s.module_config = config("context_memory");
  s.p2 = { goal: { original_goal: "整理项目", user_messages: ["整理项目"], hard_constraints: [] }, experiences: {
    observed: { id: "technical-id", question: "查找项目负责人", status: "active", queries: [], sources: ["local://lib/doc#L1"], feedback: { grounded_evidence_ids: [], new_evidence_ids: null, newly_filled_cells: null, duration_s: null } },
  }, compaction_metrics: [] };
  const html = renderToStaticMarkup(React.createElement(Panel, { state: s, onOpenLocalSource() {} }));
  assert.match(html, /当前研究累计/);
  assert.match(html, /已启用 · 已触发/);
  assert.match(html, /新增证据 <b>未记录/);
  assert.match(html, /查看原文/);
  assert.match(html, /尚未触发需要记录的上下文压缩或重建/);
  assert.doesNotMatch(html, /technical-id|P1|P2|SearchOS/);
});

test("适用口径和未知时间边界原样展示，确认修订保留两份可查来源", () => {
  const s = state();
  s.module_config = config("evidence_revision");
  s.evidence_graph.nodes = [{ id: "e1", source: "https://example.com/old" }, { id: "e2", source: "https://example.com/new" }];
  s.evidence_graph.claim_annotations.e2 = { evidence_id: "e2", entity: "项目", attribute: "负责人", value: "李明", scope_key: "国内业务", verified: true, reason: "原文明确说明", valid_time: { start: "2026-01-01", end: null } };
  s.p1 = { events: { one: { event: { id: "internal-revision-id", confirmed: true, old_evidence_id: "e1", new_evidence_id: "e2", reason: "更新适用范围" }, impact: { affected_artifact_ids: ["report:answer"], affected_task_ids: ["t1"], unchanged_ids: [] } } } };
  const html = renderToStaticMarkup(React.createElement(Panel, { state: s, onOpenLocalSource() {} }));
  assert.match(html, /适用口径：国内业务/);
  assert.match(html, /2026-01-01 — 终点未记录/);
  assert.match(html, /href="https:\/\/example.com\/old"/);
  assert.match(html, /href="https:\/\/example.com\/new"/);
  assert.match(html, /影响 1 项内容、1 个任务/);
  assert.doesNotMatch(html, /internal-revision-id/);
});
