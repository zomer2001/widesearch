/* eslint-disable @typescript-eslint/no-require-imports -- Pure knowledge transformations run without browser or model calls. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const ts = require("typescript");
const vm = require("node:vm");
function load(file, dependencies) {
  const exports = {};
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "../src/lib", file), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  vm.runInNewContext(code, { exports, URL, require: (name) => dependencies[name] }); return exports;
}
const knowledge = load("knowledgeDatabase.ts", { "./api": { API_BASE: "" } });
const helpers = load("topicKnowledge.ts", { "./knowledgeDatabase": knowledge });
const plain = (value) => JSON.parse(JSON.stringify(value));
const entity = (attributes = {}, overrides = {}) => ({ id: "p1", kind: "product", name: "产品甲", summary: "", category: "", attributes, evidence_ids: ["whole-source"], origin: "archive", status: "source_backed", ...overrides });
const field = { key: "mechanism", label: "原理", type: "text" };

test("字段显示独立来源值，不以最后观察覆盖冲突，也不把实体引用冒称字段引用", () => {
  const item = entity({ mechanism: "机制 A", document_observations: [{ attributes: { mechanism: "机制 B" }, source_ids: ["whole-other"] }] });
  const cell = helpers.entityField(item, field);
  assert.equal(cell.status, "multiple");
  assert.deepEqual(plain(cell.values), [{ text: "机制 A", evidenceIds: [] }, { text: "机制 B", evidenceIds: [] }]);
  assert.deepEqual(plain(helpers.fieldEvidence(cell, [{ id: "whole-source" }, { id: "whole-other" }])), []);
});

test("新字段映射只展示该字段引句，来源观察的相同值合并依据", () => {
  const item = entity({ mechanism: "机制 A", document_observations: [{ attributes: { mechanism: "机制 A" }, field_evidence: { mechanism: ["quote-b"] } }] }, { field_evidence: { mechanism: ["quote-a"] } });
  const cell = helpers.entityField(item, field);
  assert.equal(cell.status, "recorded");
  assert.equal(cell.values.length, 1);
  assert.deepEqual(plain(cell.values[0].evidenceIds), ["quote-a", "quote-b"]);
  assert.deepEqual(plain(helpers.fieldEvidence(cell, [{ id: "quote-a" }, { id: "unrelated" }])), [{ id: "quote-a" }]);
});

test("手工字段覆盖与原文区分，未填写字段不会从概述中猜测", () => {
  const item = entity({ mechanism: "来源 A" }, { field_overrides: { mechanism: { value: "我的修订", updated_at: "now" } } });
  assert.deepEqual(plain(helpers.entityField(item, field)), { status: "manual", values: [{ text: "我的修订", evidenceIds: [] }] });
  assert.equal(helpers.entityField(entity({}, { summary: "该产品很成熟" }), { key: "maturity", label: "成熟度", type: "text" }).status, "missing");
});

test("附加属性隐藏内部证据数据，保留正文自由字段而不重复模板列", () => {
  const item = entity({ mechanism: "A", 原理: "B", field_evidence: { mechanism: ["e"] }, namespace: "org", "实际约束": "温度", document_observations: [{ attributes: { "新字段": "新值" } }] });
  assert.deepEqual(plain(helpers.additionalFields(item, [field]).map((value) => value.key)), ["实际约束", "新字段"]);
});

test("图谱不引入专题之外节点，谱系不把技术关联伪装成父子关系", () => {
  const data = { entities: [entity(), entity({}, { id: "p2", name: "产品乙" }), entity({}, { id: "t1", kind: "technology", name: "技术甲" })], relations: [
    { id: "one", source_id: "p2", target_id: "p1", predicate: "variant_of" },
    { id: "two", source_id: "p1", target_id: "t1", predicate: "uses_technology" },
    { id: "bad", source_id: "p1", target_id: "outside", predicate: "part_of" },
  ] };
  assert.deepEqual(plain(helpers.scopedGraph(data, "lineage", null, "").relations.map((value) => value.id)), ["one"]);
  assert.deepEqual(plain(helpers.scopedGraph(data, "network", "t1", "").entities.map((value) => value.id)), ["p1", "t1"]);
  assert.equal(helpers.scopedGraph(data, "network", null, "不存在").entities.length, 0);
});

test("产出仅导出保存的变化和缺项，不虚构分析或更新", () => {
  const snapshot = { topic: { title: "测试专题" }, knowledge: { entities: [entity()], relations: [] }, sources: [{ title: "原文 A", url: "https://example.org/a", available: true }], changes: [{ created_at: "2026-11-04T00:00:00Z", entities_added: 1, entities_updated: 0, entities_removed: 0, relations_added: 0, relations_removed: 0, sources_added: 1 }], gaps: [{ entity_name: "产品甲", label: "成熟度" }] };
  const text = helpers.topicUpdateText(snapshot);
  assert.match(text, /新增 1 个实体，收录 1 份资料/);
  assert.match(text, /产品甲：成熟度/);
  assert.match(text, /https:\/\/example.org\/a/);
  assert.doesNotMatch(text, /技术成熟度已经提升/);
  assert.equal(helpers.changeText({ entities_added: 0, entities_updated: 0, entities_removed: 0, relations_added: 0, relations_removed: 0, sources_added: 0 }), "本轮未记录知识变化");
});

test("精简切换实际限制节点数量，全量恢复真实关联，且不捏造缺失边", () => {
  const data = { entities: [entity(), ...Array.from({ length: 21 }, (_, i) => entity({}, { id: `t${i}`, kind: "technology", name: `技术${i}` }))], relations: Array.from({ length: 21 }, (_, i) => ({ id: `edge${i}`, source_id: "p1", target_id: `t${i}`, predicate: "uses_technology", status: "source_backed", evidence_ids: ["quote"] })) };
  data.relations[20].status = "manual";
  const options = { perspective: "product", mode: "network", focusId: "p1", query: "" };
  const compact = helpers.topicGraphRecords(data, { ...options, density: "compact" });
  const full = helpers.topicGraphRecords(data, { ...options, density: "full" });
  assert.equal(compact.entities.length, 16);
  assert.equal(compact.hidden, 6);
  assert.equal(full.entities.length, 22);
  assert.equal(full.relations.length, 21);
  assert.ok(compact.entities.some((item) => item.id === "p1"));
  assert.ok(compact.relations.every((edge) => edge.status === "source_backed" && data.relations.some((original) => original.id === edge.id)));
  assert.ok(!compact.relations.some((edge) => edge.id === "edge20"));
});

test("产品和技术视角选择各自真实中心，全量仍尊重视角与搜索范围", () => {
  const data = { entities: [entity(), entity({}, { id: "t1", kind: "technology", name: "技术甲" }), entity({}, { id: "t2", kind: "technology", name: "独立技术" }), entity({}, { id: "p2", name: "独立产品" })], relations: [{ id: "edge", source_id: "p1", target_id: "t1", predicate: "uses_technology", status: "source_backed", evidence_ids: ["q"] }] };
  assert.equal(helpers.defaultTopicGraphFocus(data, "product"), "p1");
  assert.equal(helpers.defaultTopicGraphFocus(data, "technology"), "t1");
  const options = { density: "full", mode: "network", focusId: null, query: "" };
  assert.deepEqual(plain(helpers.topicGraphRecords(data, { ...options, perspective: "product" }).entities.map((item) => item.id)), ["p1", "t1", "p2"]);
  assert.deepEqual(plain(helpers.topicGraphRecords(data, { ...options, perspective: "technology" }).entities.map((item) => item.id)), ["p1", "t1", "t2"]);
  assert.equal(helpers.topicGraphRecords(data, { ...options, perspective: "technology", query: "不存在" }).entities.length, 0);
});

test("全量遍历对应视角的完整连通分量，保留三跳参考而不扩到无关孤岛", () => {
  const data = { entities: [entity(), entity({}, { id: "t1", kind: "technology", name: "技术" }), entity({}, { id: "r1", kind: "route", name: "路线" }), entity({}, { id: "ref1", kind: "reference", name: "外部项目" }), entity({}, { id: "isolated", kind: "reference", name: "无关项目" })], relations: [
    { id: "e1", source_id: "p1", target_id: "t1", predicate: "uses_technology", status: "source_backed", evidence_ids: ["q1"] },
    { id: "e2", source_id: "t1", target_id: "r1", predicate: "has_route", status: "source_backed", evidence_ids: ["q2"] },
    { id: "e3", source_id: "ref1", target_id: "r1", predicate: "reference_for", status: "source_backed", evidence_ids: ["q3"] },
  ] };
  const options = { perspective: "product", density: "full", mode: "network", focusId: null, query: "" };
  const full = helpers.topicGraphRecords(data, options);
  assert.deepEqual(plain(full.entities.map((item) => item.id)), ["p1", "t1", "r1", "ref1"]);
  assert.deepEqual(plain(full.relations.map((item) => item.id)), ["e1", "e2", "e3"]);
  assert.deepEqual(plain(helpers.topicGraphRecords(data, { ...options, focusId: "p1" }).entities.map((item) => item.id)), ["p1", "t1"]);
});

test("专题字段区分未知、未公开与不适用，缺值状态保留自己的证据", () => {
  const original = entity();
  const projected = [{ ...field, value: null, status: "not_public", evidence_ids: ["publication-quote"], observations: [] }];
  const cell = helpers.projectedFieldCell(original, field, projected);
  assert.equal(cell.status, "not_public");
  assert.equal(cell.values.length, 0);
  assert.deepEqual(plain(helpers.fieldEvidence(cell, [{ id: "publication-quote" }, { id: "whole-source" }])), [{ id: "publication-quote" }]);
  assert.equal(helpers.projectedFieldCell(original, field, [{ ...projected[0], status: "unknown", evidence_ids: [] }]).status, "missing");
  assert.equal(helpers.projectedFieldCell(original, field, [{ ...projected[0], status: "not_applicable" }]).status, "not_applicable");
});

test("投影字段合并相同的来源值并保留冲突，不丢失字段引句", () => {
  const projection = [{ ...field, status: "conflicting", value: "甲", evidence_ids: ["a", "b", "c"], observations: [
    { value: "甲", status: "known", manual: false, evidence_ids: ["a"] },
    { value: "甲", status: "known", manual: false, evidence_ids: ["b"] },
    { value: "乙", status: "known", manual: false, evidence_ids: ["c"] },
  ] }];
  const cell = helpers.projectedFieldCell(entity(), field, projection);
  assert.equal(cell.status, "multiple");
  assert.deepEqual(plain(cell.values), [{ text: "甲", evidenceIds: ["a", "b"] }, { text: "乙", evidenceIds: ["c"] }]);
});

test("分支是对同一数据的过滤，跨分支和外部节点不被伪造为连边", () => {
  const data = { entities: [entity(), entity({}, { id: "p2" }), entity({}, { id: "p3" })], relations: [
    { id: "a", source_id: "p1", target_id: "p2" }, { id: "b", source_id: "p2", target_id: "p3" },
  ], evidence: [] };
  const scoped = helpers.branchKnowledge(data, ["p1", "p2"]);
  assert.deepEqual(plain(scoped.entities.map((item) => item.id)), ["p1", "p2"]);
  assert.deepEqual(plain(scoped.relations.map((item) => item.id)), ["a"]);
  assert.equal(data.entities.length, 3);
});

test("展开与收起只处理真实邻接，节点大小按不同邻居计数", () => {
  const data = { entities: [entity(), ...["p2", "p3", "p4"].map((id) => entity({}, { id }))], relations: [
    { id: "a", source_id: "p1", target_id: "p2" }, { id: "b", source_id: "p2", target_id: "p3" },
    { id: "c", source_id: "p2", target_id: "p3" }, { id: "d", source_id: "p2", target_id: "outside" },
  ] };
  const degrees = helpers.graphDegrees(data);
  assert.equal(degrees.get("p2"), 2);
  const base = { entities: data.entities.slice(0, 2), relations: [data.relations[0]] };
  const expanded = helpers.expandGraph(data, base, ["p2"], []);
  assert.deepEqual(plain(expanded.entities.map((item) => item.id)), ["p1", "p2", "p3"]);
  assert.ok(expanded.relations.every((edge) => edge.target_id !== "outside"));
  assert.deepEqual(plain(helpers.graphLeafIds(expanded, "p2")), ["p1", "p3"]);
  const collapsed = helpers.expandGraph(data, base, ["p2"], ["p3"]);
  assert.deepEqual(plain(collapsed.entities.map((item) => item.id)), ["p1", "p2"]);
});

test("人工维护显示当前覆盖值，历史来源仍留在投影中而不冒充当前冲突", () => {
  const projection = [{ ...field, status: "known", manual: true, value: "维护值", evidence_ids: [], observations: [
    { value: "旧来源", status: "known", manual: false, active: false, evidence_ids: ["old"] },
    { value: "维护值", status: "known", manual: true, active: true, evidence_ids: [] },
  ] }];
  const cell = helpers.projectedFieldCell(entity(), field, projection);
  assert.equal(cell.status, "manual");
  assert.deepEqual(plain(cell.values), [{ text: "维护值", evidenceIds: [] }]);
  assert.equal(projection[0].observations.length, 2);
});

test("参数的版本和测试条件保留在各自值下，不把合法的跨版本差异标成冲突", () => {
  const projection = [{ ...field, status: "known", value: "20", evidence_ids: ["a", "b"], observations: [
    { value: "20", status: "known", manual: false, evidence_ids: ["a"], context: { version: "V1", unit: "km", test_conditions: "静水" } },
    { value: "30", status: "known", manual: false, evidence_ids: ["b"], context: { version: "V2", unit: "km", test_conditions: "静水" } },
  ] }];
  const cell = helpers.projectedFieldCell(entity(), field, projection);
  assert.equal(cell.status, "contextual");
  assert.equal(cell.values[0].contexts[0].version, "V1");
  assert.equal(cell.values[1].contexts[0].version, "V2");
  assert.match(helpers.observationContextText(cell.values[0].contexts[0]), /版本：V1 · 单位：km · 测试条件：静水/);
});

test("相同参数值的多个适用条件均保留，迭代边进入谱系而部署关联不冒充谱系", () => {
  const cell = helpers.projectedFieldCell(entity(), field, [{ ...field, status: "known", value: "20", evidence_ids: ["a", "b"], observations: [
    { value: "20", status: "known", manual: false, evidence_ids: ["a"], context: { version: "V1" } },
    { value: "20", status: "known", manual: false, evidence_ids: ["b"], context: { version: "V2" } },
  ] }]);
  assert.equal(cell.values.length, 1);
  assert.equal(cell.values[0].contexts.length, 2);
  const data = { entities: [entity(), entity({}, { id: "p2" })], relations: [
    { id: "iteration", source_id: "p1", target_id: "p2", predicate: "evolves_from" },
    { id: "deployment", source_id: "p1", target_id: "p2", predicate: "deploys" },
  ] };
  assert.deepEqual(plain(helpers.scopedGraph(data, "lineage", null, "").relations.map((item) => item.id)), ["iteration"]);
});

test("分支资料按归档文档匹配，节选定位不同仍能找到全文，保留关系引用", () => {
  const data = { entities: [entity({}, { evidence_ids: ["e1", "external"] })], relations: [{ evidence_ids: ["e2"] }], evidence: [
    { id: "e1", library_id: "lib-a", document_id: "doc-a", ref_id: "local://lib-a/doc-a?rev=abc#excerpt" },
    { id: "e2", ref_id: "local://lib-b/doc-b?rev=xyz#relationship" },
    { id: "external", ref_id: "repository://project/revision" },
    { id: "unrelated", ref_id: "" },
  ] };
  const sources = [
    { id: "a", library_id: "lib-a", document_id: "doc-a", ref_id: "local://lib-a/doc-a?rev=abc#whole-document" },
    { id: "b", library_id: "lib-b", document_id: "doc-b", ref_id: "local://lib-b/doc-b?rev=xyz#whole-document" },
    { id: "external", ref_id: "repository://project/revision" },
    { id: "wrong-library", library_id: "lib-other", document_id: "doc-a", ref_id: "" },
    { id: "no-ref", ref_id: "" },
  ];
  assert.deepEqual(plain(helpers.sourcesForBranch(sources, data).map((item) => item.id)), ["a", "b", "external"]);
});

test("恢复字段清除当前实体的实际别名覆盖，不清除继承的人工记录或无关字段", () => {
  const projection = [{ ...field, status: "known", value: "A", evidence_ids: [], source_keys: ["原理", "机制说明"], manual: true }];
  const original = entity({}, { field_overrides: { 原理: { value: "人工A" }, 机制说明: { value: "人工B" }, unrelated: { value: "其他" } } });
  assert.deepEqual(plain(helpers.restorableFieldKeys(original, field, projection)), ["原理", "机制说明"]);
  assert.deepEqual(plain(helpers.restorableFieldKeys(entity({}, { origin: "manual" }), field, projection)), []);
  assert.deepEqual(plain(helpers.restorableFieldKeys(entity({ document_observations: [{ field_overrides: { 原理: { value: "人工" } } }] }), field, projection)), []);
});
