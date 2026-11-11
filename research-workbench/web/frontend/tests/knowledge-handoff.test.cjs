/* eslint-disable @typescript-eslint/no-require-imports -- Execute import decisions without a live API or model. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const test = require('node:test');
const flush = () => new Promise(setImmediate);

function find(node, predicate) {
  if (Array.isArray(node)) { for (const child of node) { const result = find(child, predicate); if (result) return result; } return; }
  if (!node || typeof node !== 'object') return;
  return predicate(node) ? node : find(node.props?.children, predicate);
}
function text(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (Array.isArray(node)) return node.map(text).join('');
  return typeof node === 'object' ? text(node.props?.children) : String(node);
}
function harness(overrides = {}) {
  const slots = [], effects = [], calls = [];
  let cursor = 0, tree;
  const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  const react = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = initial; return [slots[i], (v) => { slots[i] = typeof v === 'function' ? v(slots[i]) : v; }]; },
    useCallback(fn, deps) { const i = cursor++; if (!slots[i] || !same(slots[i].deps, deps)) slots[i] = { fn, deps }; return slots[i].fn; },
    useEffect(fn, deps) { const i = cursor++; if (!slots[i] || !same(slots[i].deps, deps)) { slots[i]?.cleanup?.(); slots[i] = { deps }; effects.push(() => { slots[i].cleanup = fn(); }); } },
  };
  const response = { plan_id: 'plan-1', document_id: 'doc-1', source_revision: 'a'.repeat(64), status: 'ready', counts: { records: 2 }, records: [], entities: [], unmapped: [], conflicts: [], can_commit: true };
  const api = {
    readHandoffStatus: async () => ({ plans: [], batches: [] }),
    previewHandoff: async (...args) => { calls.push(['preview', ...args]); return response; },
    commitHandoff: async (...args) => { calls.push(['commit', ...args]); return { status: 'committed' }; },
    saveHandoffSummary: async (...args) => { calls.push(['summary', ...args]); return { id: 'summary-1' }; },
    handoffStatusLabel: String,
    ...overrides,
  };
  const dependencies = { react, 'react/jsx-runtime': { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) }, 'lucide-react': new Proxy({}, { get: (_, key) => key }), '@/lib/researchDatabase': { fieldValue: String }, '@/lib/knowledgeHandoff': api, './HandoffSourceReader': { default: 'SourceReader' } };
  const exports = {};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/components/topics/HandoffReviewPanel.tsx'), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText, { exports, require: (id) => dependencies[id] ?? require(id), AbortController, Date, setInterval() { return 1; }, clearInterval() {} });
  const h = {
    calls, response,
    render() { cursor = 0; tree = exports.default({ slug: 'anduril', onCommitted: () => calls.push(['refresh']), onClose() {} }); effects.splice(0).forEach((fn) => fn()); return tree; },
    find: (predicate) => find(tree, predicate),
    button: (label) => find(tree, (n) => n.type === 'button' && text(n).includes(label)),
    text: () => text(tree),
    async upload(value) { h.find((n) => n.type === 'input' && n.props.type === 'file').props.onChange({ currentTarget: { files: [{ size: 100, text: async () => JSON.stringify(value) }], value: 'file' } }); await flush(); h.render(); },
  };
  h.render(); return h;
}
const result = { schema_version: 'knowledge-handoff-v2', document_id: 'doc-1', source_revision: 'a'.repeat(64), outputs: [{ entities: [] }], status: 'completed' };

test('上传只创建预览，确认后才提交；部分结果必须显式选择', async () => {
  const h = harness();
  h.response.unmapped.push({ reason: '参数缺少单位' });
  h.response.requires_partial_confirmation = true;
  await h.upload(result);
  assert.deepEqual(h.calls.map((c) => c[0]), ['preview']);
  assert.equal(h.button('确认入库').props.disabled, true);
  h.find((n) => n.type === 'input' && n.props.type === 'checkbox').props.onChange({ target: { checked: true } }); h.render();
  assert.equal(h.button('确认入库').props.disabled, false);
  h.button('确认入库').props.onClick(); await flush(); h.render();
  assert.deepEqual(h.calls.find((c) => c[0] === 'commit'), ['commit', 'anduril', 'plan-1', true]);
  assert.equal(h.button('确认入库'), undefined);
});

test('旧结构不直接入库，失败提示不伪装成功', async () => {
  const h = harness(); await h.upload({ entities: [] });
  assert.equal(h.calls.length, 0);
  assert.match(h.text(), /旧格式需先/);
  assert.equal(h.button('确认入库'), undefined);
});

test('只有摘要时可以独立保存，不创建事实计划或调用提交', async () => {
  const h = harness(); await h.upload({ ...result, outputs: [], summary: { kind: 'extractive', text: 'Fictional excerpt.', quotes: ['Fictional excerpt.'] } });
  assert.equal(h.calls.length, 0);
  h.button('仅保存摘要').props.onClick(); await flush(); h.render();
  assert.deepEqual(h.calls.map((c) => c[0]), ['summary']);
  assert.match(h.text(), /摘要已独立保存/);
});

test('身份选择后旧计划不可提交，必须按选择重新预览', async () => {
  const h = harness(); h.response.entities.push({ name: 'Product X', resolution_key: 'e1', resolution: 'unresolved', candidates: [{ id: 'existing:1', name: 'Product X', namespace: 'anduril' }] });
  await h.upload(result);
  h.find((n) => n.type === 'select').props.onChange({ target: { value: 'existing:1' } }); h.render();
  assert.equal(h.button('确认入库').props.disabled, true);
  h.button('按身份选择重新预览').props.onClick(); await flush(); h.render();
  const calls = h.calls.filter((c) => c[0] === 'preview');
  assert.equal(calls.length, 2); assert.equal(calls[1][3].e1, 'existing:1');
});

test('过期写入计划报错，不显示已入库', async () => {
  const h = harness({ commitHandoff: async () => { throw new Error('原文版本已变化，请重新预览'); } });
  await h.upload(result); h.button('确认入库').props.onClick(); await flush(); h.render();
  assert.match(h.text(), /原文版本已变化/);
  assert.doesNotMatch(h.text(), /已完成本次确认入库/);
  assert(h.button('确认入库'));
});
