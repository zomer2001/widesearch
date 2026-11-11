/* eslint-disable @typescript-eslint/no-require-imports -- Exercise asynchronous ingestion observation with controlled requests and timers. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
function load(file, dependencies = {}, globals = {}) {
  const exports = {};
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src', file), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText;
  vm.runInNewContext(code, { exports, Error, URL, AbortSignal, AbortController, setTimeout, clearTimeout, require: (name) => dependencies[name] ?? require(name), ...globals });
  return exports;
}
const lib = load('lib/knowledgeDatabase.ts', { './api': { API_BASE: '' } });
const monitor = load('lib/knowledgeIngestion.ts', { './knowledgeDatabase': lib }).monitorKnowledgeIngestion;
const flush = () => new Promise(setImmediate);
const job = (status, overrides = {}) => ({ id: `job-${status}`, session_id: '20261102-research', document_id: `doc-${status}`, title: `原文-${status}`, status, message: '', engine: 'openai', model: 'gpt-4o', created_at: '2026-11-02T08:00:00Z', updated_at: '2026-11-02T08:01:00Z', entities: status === 'completed' ? 3 : 0, relations: status === 'completed' ? 2 : 0, ...overrides });
const snapshot = (jobs = []) => ({ enabled: true, counts: Object.fromEntries(['queued', 'running', 'completed', 'partial', 'failed', 'skipped'].map((status) => [status, jobs.filter((item) => item.status === status).length])), jobs });
function timers() {
  let id = 0; const waiting = new Map();
  return { waiting, schedule(fn, delay) { const key = ++id; waiting.set(key, { fn, delay }); return key; }, unschedule(key) { waiting.delete(key); }, async tick() { const [key, item] = waiting.entries().next().value; assert.equal(item.delay, 5000); waiting.delete(key); item.fn(); await flush(); } };
}

test('入库读取只GET，显式补抽取和重试才POST，错误不会变成成功载荷', async () => {
  const requests = [], controller = new AbortController();
  const api = load('lib/knowledgeDatabase.ts', { './api': { API_BASE: '' } }, { fetch: async (url, options) => { requests.push({ url, ...options }); return options.method === 'POST' ? { ok: false, status: 422, json: async () => ({ detail: '没有最近已归档的研究任务' }) } : { ok: true, status: 200, json: async () => snapshot() }; } });
  await api.getKnowledgeIngestion(controller.signal, '任务/a');
  assert.equal(requests.length, 1); assert.equal(requests[0].method, 'GET');
  assert.equal(requests[0].url, '/api/knowledge/ingestion?session_id=%E4%BB%BB%E5%8A%A1%2Fa');
  controller.abort(); assert.equal(requests[0].signal.aborted, true);
  await assert.rejects(api.startKnowledgeIngestion(), /没有最近已归档/);
  assert.equal(requests[1].url, '/api/knowledge/ingestion'); assert.deepEqual(JSON.parse(requests[1].body), {});
  await assert.rejects(api.retryKnowledgeIngestion('job/a'), /没有最近已归档/);
  assert.equal(requests[2].url, '/api/knowledge/ingestion/job%2Fa/retry');
});

test('进行中与需关注按真实状态筛选，部分入库不算完成', () => {
  assert.equal(lib.matchesIngestion(job('queued'), '', 'active'), true);
  assert.equal(lib.matchesIngestion(job('partial'), '', 'completed'), false);
  assert.equal(lib.matchesIngestion(job('partial'), '', 'attention'), true);
  assert.equal(lib.matchesIngestion(job('failed'), '', 'attention'), true);
  assert.equal(lib.matchesIngestion(job('skipped'), '', 'attention'), true);
  assert.equal(lib.matchesIngestion(job('completed'), '', 'attention'), false);
  assert.equal(lib.matchesIngestion(job('completed', { title: 'Lattice Mesh 原文' }), 'mesh', 'completed'), true);
  assert.equal(lib.matchesIngestion(job('completed'), '不存在', 'all'), false);
});

test('观察队列和完成转换时更新图谱，空闲轮询只读进度并在停止时清理', async () => {
  const clock = timers(), changes = [], database = [], states = [snapshot([job('queued')]), snapshot([job('completed')]), snapshot([job('completed')])];
  let requests = 0, databaseReads = 0;
  const observer = monitor({ ...clock, fetchJobs: async () => states[requests++], fetchDatabase: async () => ({ revision: ++databaseReads }), onUpdate: (value) => changes.push(value), onDatabase: (value) => database.push(value), onError() {} });
  await flush(); assert.equal(requests, 1); assert.equal(databaseReads, 1); assert.equal(clock.waiting.size, 1);
  await clock.tick(); assert.equal(databaseReads, 2); assert.equal(changes.at(-1).counts.completed, 1);
  await clock.tick(); assert.equal(requests, 3); assert.equal(databaseReads, 2);
  observer.stop(); assert.equal(clock.waiting.size, 0); assert.equal(database.length, 2);
});

test('卸载中止当前读取，迟到结果不能写页面或重新安排轮询', async () => {
  const clock = timers(); let resolve, signal, writes = 0;
  const observer = monitor({ ...clock, fetchJobs: (currentSignal) => { signal = currentSignal; return new Promise((done) => { resolve = done; }); }, fetchDatabase: async () => { throw Error('must not read'); }, onUpdate: () => { writes++; }, onDatabase: () => { writes++; }, onError: () => { writes++; } });
  observer.stop(); assert.equal(signal.aborted, true);
  resolve(snapshot([job('running')])); await flush();
  assert.equal(writes, 0); assert.equal(clock.waiting.size, 0);
});

test('连续失败三次后暂停，显式刷新能够恢复且不假报入库成功', async () => {
  const clock = timers(), errors = [], updates = []; let reads = 0, recovered = false;
  const observer = monitor({ ...clock, fetchJobs: async () => { reads++; if (!recovered) throw Error('offline'); return snapshot(); }, fetchDatabase: async () => { throw Error('idle must not load database'); }, onUpdate: (value) => updates.push(value), onDatabase() {}, onError: (value) => errors.push(value) });
  await flush(); await clock.tick(); await clock.tick();
  assert.equal(reads, 3); assert.equal(clock.waiting.size, 0); assert.equal(updates.length, 0); assert.match(errors.at(-1), /暂停自动刷新/);
  recovered = true; observer.refresh(); await flush();
  assert.equal(reads, 4); assert.equal(updates.length, 1); assert.equal(errors.at(-1), null); assert.equal(clock.waiting.size, 1);
  observer.stop();
});

test('手动刷新丢弃旧请求的迟到状态，不产生并行轮询定时器', async () => {
  const clock = timers(), pending = [], updates = [];
  const observer = monitor({ ...clock, fetchJobs: (signal) => new Promise((resolve) => pending.push({ resolve, signal })), fetchDatabase: async () => ({}), onUpdate: (value) => updates.push(value), onDatabase() {}, onError() {} });
  observer.refresh(); assert.equal(pending[0].signal.aborted, true);
  pending[1].resolve(snapshot([job('completed')])); await flush();
  pending[0].resolve(snapshot([job('failed')])); await flush();
  assert.equal(updates.length, 1); assert.equal(updates[0].jobs[0].status, 'completed'); assert.equal(clock.waiting.size, 1);
  observer.stop();
});

test('活动期数据库读取失败后，完成状态仍触发最终知识刷新', async () => {
  const clock = timers(), states = [snapshot([job('running')]), snapshot([job('completed')])], databases = []; let calls = 0, reads = 0;
  const observer = monitor({ ...clock, fetchJobs: async () => states[calls++], fetchDatabase: async () => { if (++reads === 1) throw Error('temporary database failure'); return { newEntity: true }; }, onUpdate() {}, onDatabase: (value) => databases.push(value), onError() {} });
  await flush(); await clock.tick();
  assert.equal(reads, 2); assert.equal(databases.length, 1); assert.equal(databases[0].newEntity, true); observer.stop();
});

function text(node) { if (Array.isArray(node)) return node.map(text).join(''); if (node == null || typeof node === 'boolean') return ''; return typeof node === 'object' ? text(node.props?.children) : String(node); }
function viewHarness(overrides = {}) {
  const slots = []; let index = 0, nodes = [], props = { data: snapshot(['queued', 'running', 'completed', 'partial', 'failed', 'skipped'].map((status) => job(status))), error: null, actionError: null, action: null, notice: null, query: '', onRefresh() {}, onQueueLatest() {}, onRetry() {}, ...overrides };
  const jsx = (type, values) => ({ type, props: values });
  const Component = load('components/workbench/KnowledgeIngestionView.tsx', { react: { useState(initial) { const slot = index++; if (!(slot in slots)) slots[slot] = initial; return [slots[slot], (value) => { slots[slot] = value; }]; } }, 'react/jsx-runtime': { jsx, jsxs: jsx, Fragment: 'fragment' }, 'lucide-react': new Proxy({}, { get: () => () => null }), '@/lib/knowledgeDatabase': lib }).default;
  function visit(node) { if (Array.isArray(node)) return node.forEach(visit); if (!node || typeof node !== 'object') return; if (typeof node.type === 'function') return visit(node.type(node.props)); nodes.push(node); visit(node.props?.children); }
  const h = { render() { index = 0; nodes = []; visit(Component(props)); }, text: () => nodes.map(text).join(' '), nodes: () => nodes, update: (next) => { props = { ...props, ...next }; h.render(); } }; h.render(); return h;
}

test('自动入库视图呈现各状态，只有失败和部分任务可显式重试', () => {
  const retried = [], h = viewHarness({ onRetry: (id) => retried.push(id) });
  assert.match(h.text(), /部分入库/); assert.match(h.text(), /研究结束后，入库处理可能仍在继续/); assert.match(h.text(), /API 模型可能产生费用/);
  const buttons = h.nodes().filter((node) => node.type === 'button' && text(node) === '重新抽取');
  assert.equal(buttons.length, 2); buttons.forEach((button) => button.props.onClick());
  assert.deepEqual(retried, ['job-partial', 'job-failed']);
  h.nodes().find((node) => node.type === 'select').props.onChange({ target: { value: 'attention' } }); h.render();
  assert.doesNotMatch(h.text(), /原文-completed|原文-running|原文-queued/); assert.match(h.text(), /原文-partial|原文-failed|原文-skipped/);
});

test('初始读取失败和补抽取失败明确可见，不显示已入库成功提示', () => {
  const h = viewHarness({ data: null, error: '读取入库进度失败，已暂停自动刷新。', actionError: '没有最近已归档的研究任务' });
  assert.match(h.text(), /暂停自动刷新/); assert.match(h.text(), /没有最近已归档/); assert.doesNotMatch(h.text(), /已通过原文校验并写入数据库|已登记重新抽取|还没有自动入库任务/);
  assert.equal(h.nodes().find((node) => node.type === 'button' && text(node) === '补抽取最近任务').props.disabled, true);
});

function actionHarness(operations) {
  const slots = [], effects = []; let cursor = 0, value, writes = 0, stopped = 0, refreshes = 0;
  const react = {
    useState(initial) { const index = cursor++; if (!(index in slots)) { const slot = { value: typeof initial === 'function' ? initial() : initial }; slot.set = (next) => { writes++; slot.value = typeof next === 'function' ? next(slot.value) : next; }; slots[index] = slot; } return [slots[index].value, slots[index].set]; },
    useRef(initial) { return react.useState({ current: initial })[0]; },
    useEffect(effect, dependencies) { const index = cursor++, old = slots[index]; if (!old || dependencies.some((item, i) => !Object.is(item, old.dependencies[i]))) { old?.cleanup?.(); slots[index] = { dependencies }; effects.push(() => { slots[index].cleanup = effect(); }); } },
  };
  const onDatabase = () => {};
  const hook = load('hooks/useKnowledgeIngestion.ts', { react, '@/lib/knowledgeDatabase': { ...lib, ...operations }, '@/lib/knowledgeIngestion': { monitorKnowledgeIngestion: () => ({ refresh: () => { refreshes++; }, stop: () => { stopped++; } }) } }).default;
  const h = { render() { cursor = 0; value = hook(onDatabase); effects.splice(0).forEach((effect) => effect()); }, get value() { return value; }, get writes() { return writes; }, get stopped() { return stopped; }, get refreshes() { return refreshes; }, unmount() { for (const slot of slots) slot?.cleanup?.(); } };
  h.render(); return h;
}

test('显式补抽取防止重复提交，失败不会显示已登记成功且仍可重试', async () => {
  let reject, calls = 0;
  const h = actionHarness({ startKnowledgeIngestion: () => { calls++; return new Promise((_, fail) => { reject = fail; }); } });
  const first = h.value.queueLatest(); const second = h.value.queueLatest();
  assert.equal(calls, 1); reject(Error('最近任务没有可用原文')); await Promise.all([first, second]); h.render();
  assert.match(h.value.actionError, /没有可用原文/); assert.equal(h.value.notice, null); assert.equal(h.value.action, null); assert.equal(h.refreshes, 0);
  h.unmount(); assert.equal(h.stopped, 1);
});

test('补抽取返回零新增时如实说明，关闭面板时中止动作并拒绝迟到成功提示', async () => {
  const empty = actionHarness({ startKnowledgeIngestion: async () => ({ ...snapshot(), scheduled: 0, reused: 0 }) });
  await empty.value.queueLatest(); empty.render(); assert.match(empty.value.notice, /没有新增可抽取原文/); assert.doesNotMatch(empty.value.notice, /已登记/); empty.unmount();
  let done, signal;
  const h = actionHarness({ startKnowledgeIngestion: (_, current) => { signal = current; return new Promise((resolve) => { done = resolve; }); } });
  const action = h.value.queueLatest(); h.unmount(); const writes = h.writes;
  assert.equal(signal.aborted, true); done({ ...snapshot([job('queued')]), scheduled: 1 }); await action;
  assert.equal(h.writes, writes); assert.equal(h.refreshes, 0);
});
