/* eslint-disable @typescript-eslint/no-require-imports */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const output = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, "../src/lib/conversation.ts"), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports: output });
const { turnModelLabel, turnElapsedLabel, workbenchModelSelection } = output;

test("结果模型来自任务记录，兼容只有旧模型分布的历史", () => {
  assert.equal(turnModelLabel({ engine: "ollama", engineModel: "qwen3.5:4b" }), "Ollama · qwen3.5:4b");
  assert.equal(turnModelLabel({ engine: "cli", engineModel: "gpt-6-luna" }), "CLI · gpt-6-luna");
  assert.equal(turnModelLabel({ engine: "openai", engineModel: "gpt-4o" }), "OpenAI API · gpt-4o");
  assert.equal(turnModelLabel({ modelDistribution: { main: { model: "deepseek-chat" }, text: { model: "deepseek-chat" } } }), "deepseek-chat");
  assert.equal(turnModelLabel({}), "模型未记录");
});

test("重新接入但不知道开始时间的任务只显示运行中", () => {
  assert.equal(turnElapsedLabel({ status: "running", meta: { elapsed: 0 } }, 200000), "运行中");
  assert.equal(turnElapsedLabel({ status: "running", meta: { elapsed: 25 } }, 200000), "运行中");
  assert.equal(turnElapsedLabel({ status: "completed", meta: {} }, 200000), "耗时未记录");
});

test("已知开始时间显示真实计时，完成后采用后端耗时", () => {
  assert.equal(turnElapsedLabel({ status: "running", meta: { startedAt: 100000 } }, 165900), "1 分 5 秒");
  assert.equal(turnElapsedLabel({ status: "completed", meta: { elapsed: 30.4 } }, 200000), "30 秒");
  assert.equal(turnElapsedLabel({ status: "completed", meta: { elapsed: 0 } }, 200000), "0 秒");
});

test("恢复运行任务时顶部和侧栏使用实际模型，完成后恢复下一任务选择", () => {
  const turns = [{ status: "completed", meta: { engine: "deepseek", engineModel: "deepseek-chat" } },
    { status: "running", meta: { engine: "ollama", engineModel: "qwen3.5:4b" } }];
  const next = { engine: "cli", model: "gpt-6-luna" };
  const active = workbenchModelSelection(turns, true, next);
  assert.equal(active.engine, "ollama");
  assert.equal(active.model, "qwen3.5:4b");
  turns[1].status = "completed";
  assert.equal(workbenchModelSelection(turns, false, next), next);
});

test("运行模型缺失时不借用默认模型冒充，旧模型分布仍可显示", () => {
  const next = { engine: "cli", model: "gpt-6-luna" };
  const unknown = workbenchModelSelection([{ status: "running", meta: {} }], true, next);
  assert.equal(unknown.engine, undefined);
  assert.equal(unknown.model, undefined);
  const legacy = workbenchModelSelection([{ status: "running", meta: { modelDistribution: { main: { model: "qwen3:8b" } } } }], true, next);
  assert.equal(legacy.engine, undefined);
  assert.equal(legacy.model, "qwen3:8b");
});
