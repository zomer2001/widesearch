import type { SearchState, WSEvent } from "./types";

export type ResearchGraphMode = "tasks" | "evidence";
export type ResearchNodeKind = "question" | "task" | "agent" | "operation" | "source" | "evidence" | "reading" | "cell" | "answer" | "group";
export interface ResearchGraphNode {
  id: string;
  kind: ResearchNodeKind;
  label: string;
  subtitle: string;
  status?: string;
  column: number;
  groupKey: string;
  details: { label: string; value: string }[];
  href?: string;
  memberIds?: string[];
}
export interface ResearchGraphEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  relation: string;
  count: number;
}
export interface ResearchOperation {
  id: string;
  agent: string;
  tool: string;
  status: string;
  arguments: string;
  output: string;
  timestamp: string;
}
export interface ResearchGraph {
  mode: ResearchGraphMode;
  nodes: ResearchGraphNode[];
  edges: ResearchGraphEdge[];
  operations: ResearchOperation[];
  local: boolean;
  unmatchedCitations: number;
}
export interface PositionedResearchNode extends ResearchGraphNode {
  x: number; y: number; width: number; height: number;
}
export interface ResearchGraphLayout {
  nodes: PositionedResearchNode[];
  edges: ResearchGraphEdge[];
  width: number;
  height: number;
  collapsedGroups: number;
}

const text = (value: unknown): string => typeof value === "string" ? value : "";
const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const display = (value: unknown): string => typeof value === "string" ? value : value == null ? "" : JSON.stringify(value, null, 2);
const detail = (label: string, value: unknown) => ({ label, value: display(value) });
export const NODE_LABELS: Record<ResearchNodeKind, string> = {
  question: "研究问题", task: "研究任务", agent: "执行者", operation: "实际操作",
  source: "来源", evidence: "事实记录", reading: "阅读片段", cell: "结果字段", answer: "研究回答", group: "聚合节点",
};
export const STATUS_LABELS: Record<string, string> = {
  pending: "待开始", open: "待开始", running: "进行中", exploring: "进行中",
  completed: "已完成", resolved: "已完成", blocked: "待前置任务", cancelled: "已取消",
  error: "失败", failed: "失败", interrupted: "已结束", incomplete: "未完成", unknown: "未记录状态",
  active: "有效记录", rejected: "已排除", superseded: "已替代", filled: "已填写",
  missing: "待补齐", uncertain: "不确定", hard_cell: "待补齐", read: "已读取", cited: "已引用",
};

const OPERATION_LABELS: Record<string, string> = {
  local_catalog: "浏览资料目录", local_open: "读取原文", local_find: "查找片段",
  local_retrieve: "检索资料", local_read: "读取片段",
  search: "检索网页", web_search: "检索网页", explore_web: "探索网页来源",
  open: "读取网页", find: "查找片段", scroll: "继续阅读", read: "读取资料",
  socm_read: "查看研究记录", socm_write: "记录研究发现", run_python: "计算与处理",
  local_answer: "完成本地回答",
  create_schema: "建立比较表", enqueue_tasks: "分配研究任务", check_agents: "等待并汇总结果",
  read_evidence: "读取证据", list_frontier: "查看任务进度", inspect_table: "检查结果表", stop_task: "停止任务",
  add_columns: "增加比较字段", add_entities: "添加研究对象", remove_entities: "移除研究对象", edit_entities: "调整研究对象",
  project_evidence: "填入证据结果", read_coverage: "查看信息缺口", resolve_cell_provenance: "追溯字段来源",
  read_task_report: "读取任务报告", bind_evidence: "关联任务证据", extract_observation: "提取原文事实",
  read_outline: "查看报告提纲", update_outline: "调整报告提纲", write_section: "撰写内容段落",
  edit_section: "修改内容段落", annotate_section: "记录段落说明",
  list_skills: "查看可用方法", load_skill: "载入研究方法", load_anti_patterns: "查看避错经验",
  read_skill_file: "读取方法说明", exec: "执行研究方法", python: "执行计算分析", read_visual: "阅读图片内容",
};
export function researchOperationLabel(tool: string): string {
  return OPERATION_LABELS[tool] || tool;
}

function eventData(event: WSEvent): Record<string, unknown> {
  return event.type === "trajectory" ? object(event.data) : {};
}
function sourceLabel(source: string): string {
  try {
    const url = new URL(source);
    return url.protocol === "local:" ? decodeURIComponent(url.pathname.split("/").pop() || url.hostname) : url.hostname.replace(/^www\./, "");
  } catch { return source.split(/[\\/]/).pop() || source; }
}
function safeHref(source: string): string | undefined {
  return /^(https?:\/\/|local:\/\/)/i.test(source) ? source : undefined;
}
function localDocumentKey(ref: string): string {
  return ref.split("#", 1)[0];
}
/** Decode one Markdown HTML-entity layer; never discard revisions or locators. */
function citationIdentity(ref: string): string {
  return ref.replace(/&(?:amp|quot|apos|lt|gt|#(?:x[0-9a-f]+|[0-9]+));/gi, (entity) => {
    const named: Record<string, string> = { "&amp;": "&", "&quot;": '"', "&apos;": "'", "&lt;": "<", "&gt;": ">" };
    const key = entity.toLowerCase();
    if (named[key]) return named[key];
    const hex = key.startsWith("&#x");
    const code = Number.parseInt(key.slice(hex ? 3 : 2, -1), hex ? 16 : 10);
    return code >= 32 && code < 127 ? String.fromCharCode(code) : entity;
  });
}

/** Fold actual tool events. A terminal session never implies that a tool completed. */
export function researchOperations(events: WSEvent[], status = "idle"): ResearchOperation[] {
  const operations: ResearchOperation[] = [];
  const byId = new Map<string, ResearchOperation>();
  const pending = new Map<string, ResearchOperation[]>();
  events.forEach((event, index) => {
    const data = eventData(event), type = text(data.type);
    if (!["tool_call_started", "step", "tool_call_failed"].includes(type)) return;
    const action = object(data.action);
    const tool = text(data.tool) || text(action.name);
    if (!tool) return;
    const agent = text(data.agent) || text(data.agent_name) || text(data.worker);
    if (!agent) return;
    const callId = text(data.tool_call_id);
    const queueKey = `${agent}|${tool}`;
    const key = callId ? `${agent}|${callId}` : `${agent}|${tool}|${data.step_index ?? index}`;
    let entry = byId.get(key);
    if (!entry && type !== "tool_call_started") entry = pending.get(queueKey)?.shift();
    if (!entry) {
      entry = { id: `operation:${key}`, agent, tool, status: "running", arguments: "", output: "", timestamp: "" };
      operations.push(entry);
      byId.set(key, entry);
      if (type === "tool_call_started") pending.set(queueKey, [...(pending.get(queueKey) || []), entry]);
    }
    entry.arguments = display(data.args ?? action.args) || entry.arguments;
    entry.timestamp = text(data.timestamp) || entry.timestamp;
    if (type !== "tool_call_started") {
      entry.status = type === "tool_call_failed" || data.status === "error" ? "error" : "completed";
      entry.output = display(data.observation ?? data.observation_summary ?? data.error);
      pending.set(queueKey, (pending.get(queueKey) || []).filter((item) => item !== entry));
    }
  });
  if (status !== "running") operations.forEach((item) => { if (item.status === "running") item.status = "interrupted"; });
  return operations;
}

/** Every edge below is either explicit membership, a stored binding, or an exact citation. */
export function buildResearchGraph(
  state: SearchState | null,
  events: WSEvent[] = [],
  answer = "",
  mode: ResearchGraphMode = "tasks",
  status = "idle",
): ResearchGraph {
  const nodes = new Map<string, ResearchGraphNode>();
  const edges = new Map<string, ResearchGraphEdge>();
  const records = events.map(eventData);
  const local = state?.source_scope?.mode === "local" || records.some((item) => item.source_mode === "local");
  const operations = researchOperations(events, status);
  const add = (node: ResearchGraphNode) => { if (!nodes.has(node.id)) nodes.set(node.id, node); };
  const connect = (source: string, target: string, label: string, relation = label) => {
    if (!nodes.has(source) || !nodes.has(target) || source === target) return;
    const id = `${source}→${target}:${relation}`;
    if (!edges.has(id)) edges.set(id, { id, source, target, label, relation, count: 1 });
  };
  const query = state?.intent || text(records.find((item) => item.type === "task_start")?.query);
  const terminal = ["completed", "error", "failed", "cancelled", "incomplete"].includes(status);
  const taskStatus = (raw: string) => terminal && ["running", "exploring", "pending", "open", "blocked"].includes(raw) ? "incomplete" : raw;
  const taskStatusDetails = (raw: string) => [detail("状态", STATUS_LABELS[taskStatus(raw)] || raw),
    ...(taskStatus(raw) === "incomplete" ? [detail("原始任务状态", raw), detail("状态来源", "本次研究已结束，任务记录中尚无完成状态。")] : [])];
  let unmatchedCitations = 0;

  if (mode === "tasks") {
    if (query) add({ id: "question", kind: "question", label: query, subtitle: "本次研究", status,
      column: 0, groupKey: "question", details: [detail("研究问题", query)] });
    const tasks = state?.frontier?.questions || [];
    const assignedAgents = new Set<string>();
    const agentStates = new Map<string, string>();
    for (const data of records) {
      const agent = text(data.agent) || text(data.agent_name);
      if (!agent) continue;
      if (["dispatch", "task_start"].includes(text(data.type))) agentStates.set(agent, "running");
      if (["agent_complete", "task_complete", "agent_final"].includes(text(data.type))) agentStates.set(agent, "completed");
      if (["agent_error", "task_error", "error"].includes(text(data.type))) agentStates.set(agent, "error");
    }
    const agentNode = (agent: string) => {
      let agentStatus = agentStates.get(agent) || "unknown";
      if (status !== "running" && agentStatus === "running") agentStatus = "interrupted";
      add({ id: `agent:${agent}`, kind: "agent", label: agent === "local_search_agent" ? "本地资料检索" : agent === "orchestrator" ? "研究调度" : agent,
        subtitle: "实际执行记录", status: agentStatus, column: 2, groupKey: "agents",
        details: [detail("执行者标识", agent), detail("状态", STATUS_LABELS[agentStatus] || agentStatus),
          ...(agentStatus === "interrupted" ? [detail("状态来源", "会话已结束；执行者最后一条状态记录为进行中。")] : [])] });
    };
    for (const task of tasks) {
      add({ id: `task:${task.id}`, kind: "task", label: task.question, subtitle: task.kind === "write" ? "写作任务" : task.kind === "explore" ? "探索任务" : "检索任务",
        status: taskStatus(task.status), column: 1, groupKey: `tasks:${taskStatus(task.status)}`,
        details: [detail("任务", task.question), ...taskStatusDetails(task.status), detail("执行次数", task.attempts), detail("目标字段", task.target_cells), detail("结果记录", task.resolution)] });
      connect("question", `task:${task.id}`, "包含", "contains");
      const agent = task.assigned_agent_id || task.assigned_worker;
      if (agent) { agentNode(agent); assignedAgents.add(agent); connect(`task:${task.id}`, `agent:${agent}`, "指派", "assigned"); }
    }
    for (const task of tasks) for (const dependency of task.blocked_by || []) {
      connect(`task:${dependency}`, `task:${task.id}`, "前置依赖", "dependency");
    }
    for (const data of records) {
      const agent = text(data.agent) || text(data.agent_name);
      if (!agent || !["dispatch", "task_start"].includes(text(data.type))) continue;
      agentNode(agent);
      if (!assignedAgents.has(agent)) connect("question", `agent:${agent}`, data.type === "dispatch" ? "派发" : "执行", "execution");
    }
    for (const operation of operations) {
      agentNode(operation.agent);
      add({ id: operation.id, kind: "operation", label: researchOperationLabel(operation.tool),
        subtitle: STATUS_LABELS[operation.status] || operation.status, status: operation.status,
        column: 3, groupKey: `operations:${operation.agent}`,
        details: [detail("原始工具名称", operation.tool), detail("执行者", operation.agent), detail("时间", operation.timestamp), detail("输入", operation.arguments), detail("返回记录", operation.output),
          ...(operation.status === "interrupted" ? [detail("状态来源", "会话已结束；最后一条操作记录为工具调用开始。")] : [])] });
      connect(`agent:${operation.agent}`, operation.id, "调用", "tool_call");
    }
  } else {
    const evidence = state?.evidence_graph?.nodes || [];
    const localEvidenceSources = new Map<string, string>();
    for (const item of evidence) {
      const metadata = item.local_source_metadata;
      const source = item.source;
      const sourceKey = source.startsWith("local://") ? localDocumentKey(source) : source;
      if (source) add({ id: `source:${sourceKey}`, kind: "source", label: metadata?.file_name || sourceLabel(source), subtitle: metadata ? "本地文档" : "来源页面",
        column: 0, groupKey: `sources:${sourceLabel(source)}`, href: safeHref(source),
        details: [detail("来源", source), detail("文档版本", metadata?.source_revision)] });
      add({ id: `evidence:${item.id}`, kind: "evidence", label: item.claim || item.value || "事实记录", subtitle: item.entity || "已保存的事实",
        status: item.status || "active", column: 1, groupKey: `evidence:${sourceKey || "unlinked"}`,
        href: safeHref(source), details: [detail("事实", item.claim), detail("原文摘录", item.source_excerpt), detail("来源", source), detail("记录状态", STATUS_LABELS[item.status || "active"]),
          detail("模型标注置信度", typeof item.confidence === "number" ? `${Math.round(item.confidence * 100)}%` : undefined)] });
      connect(`source:${sourceKey}`, `evidence:${item.id}`, "记录来源", "provenance");
      if (source.startsWith("local://")) localEvidenceSources.set(citationIdentity(source), `source:${sourceKey}`);
    }
    for (const edge of state?.evidence_graph?.edges || []) {
      connect(`evidence:${edge.from_id}`, `evidence:${edge.to_id}`, { support: "支持", conflict: "冲突", refine: "细化" }[edge.relation], edge.relation);
    }
    for (const [key, cell] of Object.entries(state?.coverage_map?.cells || {})) {
      const table = key.split("/", 1)[0];
      add({ id: `cell:${key}`, kind: "cell", label: display(cell.value) || key.slice(table.length + 1),
        subtitle: key.slice(table.length + 1), status: cell.status, column: 2, groupKey: `cells:${table}`,
        details: [detail("结果字段", key), detail("当前值", cell.value), detail("状态", STATUS_LABELS[cell.status]), detail("存在冲突", cell.has_conflict ? "是" : "未标记冲突")] });
      const bindings = state?.coverage_map?.evidence_bindings?.[key];
      if (bindings?.length) {
        for (const binding of bindings) connect(`evidence:${binding.evidence_id}`, `cell:${key}`,
          binding.status === "active" ? "支撑" : binding.status === "rejected" ? "已排除关联" : "已替代关联", binding.status === "active" ? "supports_cell" : binding.status);
      } else {
        for (const id of cell.supporting_evidence_ids || []) connect(`evidence:${id}`, `cell:${key}`, "支撑", "supports_cell");
        if (cell.primary_evidence_id) connect(`evidence:${cell.primary_evidence_id}`, `cell:${key}`, "支撑", "supports_cell");
      }
    }
    const tasks = new Map((state?.frontier?.questions || []).map((task) => [task.id, task]));
    for (const binding of state?.evidence_graph?.task_bindings || []) {
      const task = tasks.get(binding.task_id);
      if (!task || !nodes.has(`evidence:${binding.evidence_id}`)) continue;
      add({ id: `task:${task.id}`, kind: "task", label: task.question, subtitle: "关联研究任务", status: taskStatus(task.status), column: 2, groupKey: "bound-tasks", details: [detail("任务", task.question), ...taskStatusDetails(task.status), detail("结果记录", task.resolution)] });
      connect(`evidence:${binding.evidence_id}`, `task:${task.id}`, { supports: "支持任务", contradicts: "反对记录", context: "任务背景" }[binding.relation], binding.relation);
    }
    const readingByRef = new Map<string, string>();
    for (const data of records) {
      if (data.type !== "local_read_observed") continue;
      let observation: Record<string, unknown>;
      try { observation = object(typeof data.observation === "string" ? JSON.parse(data.observation) : data.observation); } catch { continue; }
      const ref = text(observation.ref_id);
      if (!ref.startsWith("local://")) continue;
      const docKey = localDocumentKey(ref);
      const file = text(observation.file) || sourceLabel(ref);
      const location = text(observation.location_label) || ref.split("#")[1] || "阅读位置";
      add({ id: `source:${docKey}`, kind: "source", label: file, subtitle: "本地文档", column: 0, groupKey: "local-sources", href: ref, details: [detail("文档", file), detail("来源引用", ref)] });
      const id = `reading:${ref}`;
      add({ id, kind: "reading", label: location, subtitle: file, status: "read", column: 1,
        groupKey: `readings:${docKey}`, href: ref, details: [detail("来源引用", ref), detail("阅读位置", location), detail("阅读记录", observation.tool_content || observation.content || observation.text), detail("记录类型", "已读取的原文片段；不是经事实抽取后形成的事实记录")] });
      readingByRef.set(citationIdentity(ref), id);
      const alias = text(observation.tool_ref_id);
      if (alias.startsWith("local://")) readingByRef.set(citationIdentity(alias), id);
      connect(`source:${docKey}`, id, "已读取", "read");
    }
    if (answer.trim()) {
      add({ id: "answer", kind: "answer", label: "研究回答", subtitle: status === "completed" ? "已提交的回答与引用" : status === "running" ? "正在生成的回答与引用" : "已保留的部分回答与引用", status: status === "completed" ? "completed" : status === "running" ? "running" : "incomplete", column: 2, groupKey: "answer", details: [detail("回答", answer)] });
      // Only exact displayed Markdown citations create answer edges. No fuzzy
      // entity matching and no claim that all facts on a cited page were used.
      const references = new Set([...answer.matchAll(/\[[^\]\n]*\]\((<?(?:local|https?):\/\/[^\s)>]+>?)\)/g)].map((match) => citationIdentity(match[1].replace(/^<|>$/g, ""))));
      for (const ref of references) {
        const reading = readingByRef.get(ref);
        if (reading) { connect(reading, "answer", "答案引用", "cites"); continue; }
        // Enhanced local runs can publish facts without local_read_observed.
        // An exact persisted source URI proves source citation, not adoption
        // of every fact extracted from that same source window.
        const localSource = localEvidenceSources.get(ref);
        if (localSource) { connect(localSource, "answer", "答案引用", "cites"); continue; }
        const sourceKey = ref.startsWith("local://") ? localDocumentKey(ref) : ref;
        if (nodes.has(`source:${sourceKey}`) && !ref.startsWith("local://")) connect(`source:${sourceKey}`, "answer", "答案引用", "cites");
        else unmatchedCitations += 1;
      }
    }
  }
  return { mode, nodes: [...nodes.values()], edges: [...edges.values()], operations, local, unmatchedCitations };
}

/** Collapsing represents existing members and edges only; expansion is lossless. */
export function layoutResearchGraph(graph: ResearchGraph, expanded: ReadonlySet<string> = new Set(), options: { narrow?: boolean } = {}): ResearchGraphLayout {
  const groups = new Map<string, ResearchGraphNode[]>();
  graph.nodes.forEach((node) => groups.set(node.groupKey, [...(groups.get(node.groupKey) || []), node]));
  const representative = new Map<string, string>();
  let visible: ResearchGraphNode[] = [];
  let collapsedGroups = 0;
  for (const [key, members] of groups) {
    if (members.length > 8 && !expanded.has(key)) {
      const id = `group:${key}`;
      const first = members[0];
      visible.push({ id, kind: "group", label: `${members.length} 个${NODE_LABELS[first.kind]}`,
        subtitle: "点击展开全部", column: first.column, groupKey: key,
        status: members.some((node) => ["running", "exploring"].includes(node.status || "")) ? "running" : undefined,
        details: [detail("成员", members.map((node) => node.label).join("\n"))], memberIds: members.map((node) => node.id) });
      members.forEach((node) => representative.set(node.id, id));
      collapsedGroups += 1;
    } else members.forEach((node) => { visible.push(node); representative.set(node.id, node.id); });
  }
  // Many one-item source groups would otherwise form a very tall wall. Keep a
  // few concrete examples visible and collect only the remaining real nodes.
  for (const column of new Set(visible.map((node) => node.column))) {
    const members = visible.filter((node) => node.column === column);
    const key = `column:${column}`;
    if (members.length <= 6 || expanded.has(key) || members.some((node) => expanded.has(node.groupKey))) continue;
    const hidden = members.slice(4), hiddenIds = new Set(hidden.map((node) => node.id));
    const memberIds = hidden.flatMap((node) => node.memberIds || [node.id]);
    const id = `group:${key}`;
    visible = visible.filter((node) => !hiddenIds.has(node.id));
    visible.push({ id, kind: "group", label: `另有 ${memberIds.length} 个记录`, subtitle: "点击展开其余记录", column, groupKey: key,
      memberIds, details: [detail("成员", hidden.map((node) => node.label).join("\n"))],
      status: hidden.some((node) => ["running", "exploring"].includes(node.status || "")) ? "running" : undefined });
    for (const [original, current] of representative) if (hiddenIds.has(current)) representative.set(original, id);
    collapsedGroups += 1;
  }
  const edges = new Map<string, ResearchGraphEdge>();
  graph.edges.forEach((edge) => {
    const source = representative.get(edge.source), target = representative.get(edge.target);
    if (!source || !target || source === target) return;
    const key = `${source}→${target}:${edge.relation}`, previous = edges.get(key);
    if (previous) previous.count += edge.count;
    else edges.set(key, { ...edge, id: key, source, target });
  });
  const columns = [...new Set(visible.map((node) => node.column))].sort((a, b) => a - b);
  if (options.narrow) {
    const positions: PositionedResearchNode[] = [];
    let y = 42;
    for (const column of columns) {
      y += 28;
      for (const node of visible.filter((item) => item.column === column)) {
        positions.push({ ...node, x: 40, y, width: 280, height: 94 });
        y += 124;
      }
      y += 24;
    }
    return { nodes: positions, edges: [...edges.values()], width: 360,
      height: Math.max(340, y + 26), collapsedGroups };
  }
  const maxRows = Math.max(1, ...columns.map((col) => visible.filter((node) => node.column === col).length));
  const height = Math.max(400, maxRows * 124 + 100);
  const width = Math.max(840, columns.length * 300 + 80);
  const positions: PositionedResearchNode[] = [];
  columns.forEach((column, index) => {
    const members = visible.filter((node) => node.column === column);
    members.forEach((node, row) => positions.push({ ...node,
      x: 70 + index * 300 + (width - (columns.length * 300 + 80)) / 2,
      y: (height - members.length * 124) / 2 + row * 124,
      width: 222, height: 94,
    }));
  });
  return { nodes: positions, edges: [...edges.values()], width, height, collapsedGroups };
}
