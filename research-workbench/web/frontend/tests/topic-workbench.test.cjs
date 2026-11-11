/* eslint-disable @typescript-eslint/no-require-imports -- Exercise topic entry and research workspace callbacks. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const flush = () => new Promise(setImmediate);
function find(node, predicate) {
  if (!node || typeof node !== 'object') return undefined;
  if (Array.isArray(node)) { for (const item of node) { const value = find(item, predicate); if (value) return value; } return undefined; }
  if (predicate(node)) return node;
  return find(node.props?.children, predicate);
}
function harness() {
  const data = { topic: { id: 'anduril', title: 'Anduril', counts: { entities: 112, sources: 189, runs: 0 } }, knowledge: { counts: { entities: 112 } }, runs: [], tasks: [], sources: [], changes: [], gaps: [], templates: {} };
  const requests = [], slots = []; let cursor = 0;
  const topics = { topics: [data.topic], selectedId: null, snapshot: null, loaded: true, error: null, listError: null, actionError: null, action: null,
    select(id) { topics.selectedId = id; topics.snapshot = id ? data : null; }, refresh() {}, stop() {},
    async create(title) { const topic = { id: 'new', title, counts: { entities: 0, sources: 0, runs: 0 } }; data.topic = topic; topics.topics.push(topic); topics.select(topic.id); return topic; },
    async research(input) { requests.push(input); return { topic_id: topics.selectedId, run_id: 'run-1' }; },
  };
  const settings = { engines: [{ id: 'openai', available: true }], openai: { model: 'gpt-4o' } };
  const dependencies = {
    react: { useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = initial; return [slots[index], (value) => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }]; }, useRef: (current) => ({ current }), useEffect() {} },
    'react/jsx-runtime': { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) },
    'next/image': { default: 'Image' }, 'lucide-react': new Proxy({}, { get: (_target, key) => key }),
    '@/hooks/useTopics': { useTopics: () => topics }, '@/hooks/useWorkbench': { useWorkbench: () => ({ engine: 'openai', settings, loading: false, setEngine() {} }) },
    '@/hooks/useDialogFocus': { default() {} }, '@/lib/workbench': { ENGINE_IDS: ['openai'], ENGINE_LABELS: { openai: 'OpenAI' } },
    '@/lib/topics': { activeTopicRun: (run) => ['running', 'queued', 'planning', 'researching', 'ingesting'].includes(run.status), topicDate: (value) => value, topicStatus: (value) => value },
  };
  const output = {};
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/components/topics/TopicWorkbench.tsx'), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  vm.runInNewContext(code, { exports: output, require: (name) => dependencies[name] ?? { default: name.split('/').pop() } });
  const page = () => { cursor = 0; return output.default(); };
  return { page, topics, data, requests, find: (predicate) => find(page(), predicate), renderChild: (node) => node.type(node.props) };
}

test('专题优先进入知识资产，可切换原文资料、研究和知识库', () => {
  const h = harness();
  assert.equal(h.find((node) => node.type === 'textarea'), undefined);
  assert.ok(h.find((node) => node.props?.className === 'tw-topic-card' && find(node, (child) => child.type === 'h2' && child.props.children === 'Anduril')));
  h.find((node) => node.props?.className === 'tw-topic-card').props.onClick();
  assert.equal(h.topics.selectedId, 'anduril');
  assert.ok(h.find((node) => node.type === 'KnowledgeAssetsView'));
  h.find((node) => node.type === 'button' && node.props.children === '原文资料').props.onClick();
  assert.ok(h.find((node) => node.type === 'RawMaterialsView'));
  h.find((node) => node.type === 'button' && node.props.children === '研究').props.onClick();
  assert.ok(h.find((node) => node.props?.className === 'tw-chat-workspace'));
  assert.ok(h.find((node) => node.props?.id === 'tw-query'));
  h.find((node) => node.type === 'button' && node.props.children === '知识库').props.onClick();
  assert.ok(h.find((node) => node.type === 'TopicKnowledgeView'));
  h.find((node) => node.props?.['aria-label'] === '返回专题选择').props.onClick();
  assert.equal(h.topics.selectedId, null);
  assert.equal(h.find((node) => node.type === 'textarea'), undefined);
});

test('宽输入提交绑定所选专题，提交后清空输入，不凭空创建其他示例专题', async () => {
  const h = harness(); h.topics.select('anduril');
  h.find((node) => node.props?.id === 'tw-query').props.onChange({ target: { value: '技术路线' } });
  h.find((node) => node.props?.className === 'tw-composer').props.onSubmit({ preventDefault() {} });
  await flush();
  assert.equal(h.requests.length, 1); assert.equal(h.requests[0].topic_id, 'anduril'); assert.equal(h.requests[0].query, '技术路线');
  assert.equal(h.find((node) => node.props?.id === 'tw-query').props.value, '');
  assert.equal(h.topics.topics.length, 1);
});

test('自适应采集开关默认关闭，选中后随专题任务提交', async () => {
  const h = harness(); h.topics.select('anduril');
  const toggle = h.find((node) => node.props?.['aria-label'] === '自适应采集');
  assert.equal(toggle.props.checked, false);
  toggle.props.onChange({ target: { checked: true } });
  h.find((node) => node.props?.id === 'tw-query').props.onChange({ target: { value: '接口资料' } });
  h.find((node) => node.props?.className === 'tw-composer').props.onSubmit({ preventDefault() {} });
  await flush();
  assert.deepEqual(Array.from(h.requests[0].modules), ['adaptive_collection']);
});

test('自动本地命中直接显示字段答案，行动保持按需展开', () => {
  const h = harness(); h.topics.select('anduril');
  h.data.runs = [{ id: 'r', query: 'Wisp采用什么技术', status: 'completed', message: '本地查询完成', local_result: 'Wisp：光纤通信', created_at: '2026-11-04', local_only: false }];
  h.data.tasks = [{ id: 't', run_id: 'r', kind: 'local', status: 'completed', title: '查询本地知识' }];
  const turn = h.find((node) => typeof node.type === 'function' && node.type.name === 'ResearchTurn');
  const rendered = h.renderChild(turn);
  assert.equal(find(rendered, (node) => node.props?.className === 'tw-local-result').props.children, 'Wisp：光纤通信');
  assert.equal(find(rendered, (node) => node.type === 'details').props.open, undefined);
});

test('首页分支检索只筛选真实专题，未采集专题不显示虚构实体', () => {
  const h = harness();
  h.data.topic.clusters = [{ id: 'lattice', label: 'Lattice', parent_id: null, count: 9 }];
  h.topics.topics.push({ id: 'palantir', title: 'Palantir', profile: { id: 'palantir' }, counts: { entities: 0, sources: 0, runs: 0 }, clusters: [{ id: 'aip', label: 'AIP', parent_id: null, count: 0 }] });
  const emptyCard = h.find((node) => node.props?.className === 'tw-topic-card' && find(node, (child) => child.type === 'h2' && child.props.children === 'Palantir'));
  assert.equal(find(emptyCard, (node) => node.props?.className === 'tw-topic-empty-state').props.children, '待采集');
  assert.equal(find(emptyCard, (node) => node.type === 'b'), undefined);
  h.find((node) => node.props?.['aria-label'] === '搜索专题或分支').props.onChange({ target: { value: 'lattice' } });
  assert.equal(h.find((node) => node.type === 'h2' && node.props.children === 'Palantir'), undefined);
  assert.ok(h.find((node) => node.type === 'h2' && node.props.children === 'Anduril'));
  assert.equal(h.requests.length, 0);
});

test('知识分支生成调查草稿后返回宽输入，等待用户提交', () => {
  const h = harness(); h.topics.select('anduril');
  h.find((node) => node.type === 'button' && node.props.children === '知识库').props.onClick();
  const view = h.find((node) => node.type === 'TopicKnowledgeView');
  view.props.onResearch('补充 Lattice 的接口与版本证据');
  assert.equal(h.find((node) => node.props?.id === 'tw-query').props.value, '补充 Lattice 的接口与版本证据');
  assert.equal(h.requests.length, 0);
  assert.equal(h.topics.selectedId, 'anduril');
});
