/* eslint-disable @typescript-eslint/no-require-imports -- Test API isolation and asynchronous polling without a browser. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
function load(globals = {}) {
  const exports = {};
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/lib/topics.ts'), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  vm.runInNewContext(code, { exports, Error, AbortController, AbortSignal, setTimeout, clearTimeout, require: () => ({ API_BASE: '' }), ...globals });
  return exports;
}
const lib = load();
const flush = () => new Promise(setImmediate);
function timers() {
  let id = 0; const pending = new Map();
  return { pending, schedule(fn, ms) { const key = ++id; pending.set(key, { fn, ms }); return key; }, unschedule(key) { pending.delete(key); }, async tick() { const [key, value] = pending.entries().next().value; pending.delete(key); value.fn(); await flush(); } };
}
const snapshot = (status = 'completed') => ({ topic: { id: 'a' }, runs: [{ id: 'run-a', status }] });

test('浏览专题只读，启动和停止研究需要显式POST，专题和实体ID正确编码', async () => {
  const calls = [];
  const api = load({ fetch: async (url, options) => { calls.push({ url, ...options }); return { ok: true, status: options.method === 'DELETE' ? 204 : 200, json: async () => ({}) }; } });
  await api.listTopics(); await api.getTopic('topic/a');
  assert.deepEqual(calls.map((call) => call.method), ['GET', 'GET']);
  assert.equal(calls[1].url, '/api/topics/topic%2Fa');
  await api.researchTopic({ query: 'Anduril', local_only: true, max_tasks: 4, max_seconds: 1200 });
  assert.equal(calls[2].method, 'POST'); assert.equal(calls[2].url, '/api/topics/research');
  assert.equal(JSON.parse(calls[2].body).local_only, true);
  await api.stopTopicRun('run/a'); assert.equal(calls[3].url, '/api/topics/runs/run%2Fa/stop');
  await api.detachTopicEntity('topic/a', 'entity/b');
  assert.equal(calls[4].url, '/api/topics/topic%2Fa/entities/entity%2Fb'); assert.equal(calls[4].method, 'DELETE');
});

test('后端错误保持失败，不被当作空专题或研究成功', async () => {
  const api = load({ fetch: async () => ({ ok: false, status: 409, json: async () => ({ detail: '已有研究运行中' }) }) });
  await assert.rejects(api.researchTopic({ query: 'Anduril' }), /已有研究运行中/);
});

test('活动任务快速轮询，终态降低频率且不启动新任务', async () => {
  const clock = timers(), received = []; let reads = 0;
  const watcher = lib.watchTopic({ ...clock, id: 'a', read: async () => snapshot(reads++ === 0 ? 'running' : 'partial'), onData: (data) => received.push(data), onError() {} });
  await flush(); assert.equal(clock.pending.values().next().value.ms, 5000);
  await clock.tick(); assert.equal(clock.pending.values().next().value.ms, 15000);
  assert.equal(reads, 2); assert.equal(received[1].runs[0].status, 'partial');
  watcher.stop(); assert.equal(clock.pending.size, 0);
});

test('专题切换或卸载会中止请求，旧专题的迟到响应不更新界面', async () => {
  const clock = timers(); let resolve, requestSignal, writes = 0;
  const watcher = lib.watchTopic({ ...clock, id: 'old', read: (_id, signal) => { requestSignal = signal; return new Promise((done) => { resolve = done; }); }, onData() { writes++; }, onError() { writes++; } });
  watcher.stop(); assert.equal(requestSignal.aborted, true);
  resolve(snapshot()); await flush(); assert.equal(writes, 0); assert.equal(clock.pending.size, 0);
});

test('手动刷新取代旧请求，不产生并发定时器或覆盖更新结果', async () => {
  const clock = timers(), pending = [], updates = [];
  const watcher = lib.watchTopic({ ...clock, id: 'a', read: (_id, signal) => new Promise((resolve) => pending.push({ resolve, signal })), onData: (data) => updates.push(data), onError() {} });
  watcher.refresh(); assert.equal(pending[0].signal.aborted, true);
  pending[1].resolve(snapshot('completed')); await flush(); pending[0].resolve(snapshot('running')); await flush();
  assert.equal(updates.length, 1); assert.equal(updates[0].runs[0].status, 'completed'); assert.equal(clock.pending.size, 1);
  watcher.stop();
});

test('连续读取失败后暂停，显式刷新恢复，只读观察不自动重试付费工作', async () => {
  const clock = timers(), errors = []; let recovered = false, reads = 0;
  const watcher = lib.watchTopic({ ...clock, id: 'a', read: async () => { reads++; if (!recovered) throw Error('离线'); return snapshot(); }, onData() {}, onError: (error) => errors.push(error) });
  await flush(); await clock.tick(); await clock.tick();
  assert.equal(reads, 3); assert.equal(clock.pending.size, 0); assert.match(errors.at(-1), /暂停/);
  recovered = true; watcher.refresh(); await flush(); assert.equal(reads, 4); assert.equal(errors.at(-1), null);
  watcher.stop();
});

test('部分完成和失败不显示为运行中或已完成', () => {
  for (const status of ['partial', 'failed', 'stopped', 'completed']) assert.equal(lib.activeTopicRun({ status }), false);
  for (const status of ['queued', 'planning', 'researching', 'ingesting']) assert.equal(lib.activeTopicRun({ status }), true);
  assert.equal(lib.topicStatus('partial'), '部分完成'); assert.equal(lib.topicStatus('failed'), '失败');
});

test('首页进入专题工作台，历史会话仍保留在独立路由', () => {
  const exports = {}, marker = () => null;
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/app/page.tsx'), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  vm.runInNewContext(code, { exports, require: (name) => name === 'react/jsx-runtime' ? { jsx: (type, props) => ({ type, props }) } : { default: marker } });
  assert.equal(exports.default().type, marker);
  assert.ok(fs.existsSync(path.join(__dirname, '../src/app/legacy/page.tsx')));
});

test('专题详情不带counts时侧栏保留资料并从当前专题快照同步数量', () => {
  const previous = { id: 'a', title: 'Anduril', description: '已有专题', counts: { entities: 0, sources: 0, runs: 0 } };
  const detail = { topic: { id: 'a', title: 'Anduril' }, knowledge: { counts: { entities: 112 } }, sources: [{ id: 'source-1' }], runs: [{ id: 'run-1' }, { id: 'run-2' }] };
  const result = lib.topicFromSnapshot(detail, previous);
  assert.equal(result.description, '已有专题');
  assert.deepEqual(JSON.parse(JSON.stringify(result.counts)), { entities: 112, sources: 1, runs: 2 });
  detail.topic.counts = { entities: 110, sources: 0, runs: 0 };
  assert.equal(lib.topicFromSnapshot(detail, previous).counts.entities, 112);
});

test('初次列表读取不自动进入默认专题，刷新保留有效选择，返回后仍留在选择页', () => {
  const topics = [{ id: 'a' }, { id: 'b' }];
  assert.equal(lib.retainedTopicSelection(null, topics), null);
  assert.equal(lib.retainedTopicSelection('b', topics), 'b');
  assert.equal(lib.retainedTopicSelection('removed', topics), null);
});

test('专题消歧操作只修改选中专题，合并和撤销路径保持可逆映射', async () => {
  const calls = [];
  const api = load({ fetch: async (url, options) => { calls.push({ url, ...options }); return { ok: true, status: 200, json: async () => ({ topic: { id: 'a' } }) }; } });
  const merged = await api.mergeTopicEntities('topic/a', 'canonical/b', 'alias/c');
  assert.equal(merged.topic.id, 'a');
  assert.equal(calls[0].url, '/api/topics/topic%2Fa/merges');
  assert.equal(calls[0].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].body), { canonical_id: 'canonical/b', alias_id: 'alias/c' });
  await api.undoTopicMerge('topic/a', 'alias/c');
  assert.equal(calls[1].url, '/api/topics/topic%2Fa/merges/alias%2Fc');
  assert.equal(calls[1].method, 'DELETE');
});
