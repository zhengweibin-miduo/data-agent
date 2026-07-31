"use strict";

const MAX_DDL_BYTES = 262144;
const TERMINAL = new Set(["succeeded", "rejected", "failed"]);
const STAGES = [
  ["queued", "任务已受理"],
  ["running", "开始处理"],
  ["parsing", "解析物理结构"],
  ["memory_loading", "加载可复用知识"],
  ["metadata_generating", "生成表列语义"],
  ["metadata_validating", "校验表列语义"],
  ["question_planning", "规划指标澄清"],
  ["waiting_input", "等待业务澄清"],
  ["metric_generating", "生成指标定义"],
  ["metric_validating", "校验指标定义"],
  ["memory_building", "整理可复用知识"],
  ["persisting", "持久化语义快照"],
  ["succeeded", "语义元数据已生成"],
];
const STATUS_LABELS = {
  pending: "已受理",
  running: "运行中",
  waiting_input: "等待澄清",
  succeeded: "生成完成",
  rejected: "已拒绝",
  failed: "处理失败",
};

const browserSession = typeof sessionStorage === "undefined" ? null : sessionStorage;
const browserLocal = typeof localStorage === "undefined" ? null : localStorage;
const state = {
  source: "",
  ddl: "",
  job: null,
  stages: new Map(),
  eventSource: null,
  pollTimer: null,
  reconnectTimer: null,
  conversationUid: browserSession?.getItem("schema-loom-conversation") || null,
  userId: browserLocal?.getItem("schema-loom-user") || "local-user",
  activeQuestionId: null,
  failedChat: null,
  memory: null,
  preview: null,
  previewKey: null,
};
browserLocal?.setItem("schema-loom-user", state.userId);

class ApiError extends Error {
  constructor(status, payload) {
    const error = payload?.error || {};
    super(error.code || `http_${status}`);
    this.status = status;
    this.code = error.code || `http_${status}`;
    this.stage = error.stage || "request";
    this.retryable = Boolean(error.retryable);
    this.details = error.details || {};
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json", ...options.headers } : options.headers,
  });
  const payload = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function announce(message) {
  document.querySelector("#live-region").textContent = message;
}

function showError(target, error, fallback) {
  target.hidden = false;
  target.textContent = error instanceof ApiError
    ? `${fallback}（${error.code} · ${error.stage}${error.retryable ? " · 可重试" : ""}）`
    : fallback;
  target.focus?.();
}

function clearError(target) {
  target.hidden = true;
  target.textContent = "";
}

function byteLength(value) {
  return new TextEncoder().encode(value).length;
}

function setBusy(button, busy, busyText) {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? busyText : button.dataset.label;
}

function ddlInput() {
  return {
    source: document.querySelector("#source").value.trim(),
    ddl: document.querySelector("#ddl").value.trim(),
  };
}

function previewKey(source, ddl) {
  return `${source}\u0000${ddl}`;
}

function setPreviewStatus(label, tone = "idle") {
  const status = document.querySelector("#preview-status");
  status.textContent = label;
  status.dataset.tone = tone;
}

function relationshipLabel(relationship, tableById, columnById) {
  const sourceTable = tableById.get(relationship.source_table_id);
  const targetTable = tableById.get(relationship.target_table_id);
  const sourceColumn = columnById.get(relationship.source_column_id);
  const targetColumn = columnById.get(relationship.target_column_id);
  return `${sourceTable?.qualified_name || "外部表"}.${sourceColumn?.name || "字段"} → ${targetTable?.qualified_name || relationship.target_table_name}.${targetColumn?.name || relationship.target_column_name}`;
}

function setCanvasHighlight(ids) {
  const highlighted = new Set(ids);
  document.querySelectorAll(".schema-node").forEach((node) => {
    const hasColumn = [...node.querySelectorAll("[data-column-id]")].some((column) => highlighted.has(column.dataset.columnId));
    node.classList.toggle("is-related", highlighted.has(node.dataset.tableId) || hasColumn);
  });
}

function focusRelationships(tableId) {
  document.querySelectorAll(".relationship-path").forEach((path) => {
    path.classList.toggle("is-focused", path.dataset.sourceTable === tableId || path.dataset.targetTable === tableId);
  });
}

function drawRelationships() {
  const inner = document.querySelector("#lineage-inner");
  const svg = document.querySelector("#relationship-layer");
  if (!state.preview || !inner || !svg) return;
  svg.replaceChildren();
  const innerRect = inner.getBoundingClientRect();
  const width = inner.scrollWidth;
  const height = inner.scrollHeight;
  const paths = state.preview.relationships.map((relationship) => {
    const source = document.querySelector(`[data-column-id="${CSS.escape(relationship.source_column_id)}"]`);
    const target = document.querySelector(`[data-column-id="${CSS.escape(relationship.target_column_id)}"]`);
    if (!source || !target) return null;
    const from = source.getBoundingClientRect();
    const to = target.getBoundingClientRect();
    return {
      relationship,
      x1: from.right - innerRect.left,
      y1: from.top + from.height / 2 - innerRect.top,
      x2: to.left - innerRect.left,
      y2: to.top + to.height / 2 - innerRect.top,
    };
  }).filter(Boolean);
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(height));
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const fragment = document.createDocumentFragment();
  paths.forEach(({ relationship, x1, y1, x2, y2 }) => {
    const bend = Math.max(44, Math.abs(x2 - x1) * .42);
    const direction = x2 >= x1 ? 1 : -1;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M ${x1} ${y1} C ${x1 + bend * direction} ${y1}, ${x2 - bend * direction} ${y2}, ${x2} ${y2}`);
    path.setAttribute("class", "relationship-path");
    path.dataset.sourceTable = relationship.source_table_id;
    path.dataset.targetTable = relationship.target_table_id;
    fragment.append(path);
  });
  svg.append(fragment);
}

function renderSchema(preview) {
  state.preview = preview;
  const tableById = new Map(preview.tables.map((table) => [table.id, table]));
  const columnById = new Map(preview.tables.flatMap((table) => table.columns.map((column) => [column.id, column])));
  const nodes = document.querySelector("#schema-nodes");
  const outline = document.querySelector("#schema-outline-list");
  nodes.replaceChildren();
  outline.replaceChildren();
  document.querySelector("#canvas-empty").hidden = preview.tables.length > 0;
  document.querySelector("#outline-empty").hidden = preview.tables.length > 0;
  document.querySelector("#outline-count").textContent = `${preview.table_count} TABLES`;
  document.querySelector("#canvas-summary").textContent = `${preview.table_count} tables · ${preview.column_count} columns · ${preview.relationships.length} foreign keys`;
  preview.tables.forEach((table) => {
    const related = preview.relationships.filter((relationship) => relationship.source_table_id === table.id || relationship.target_table_id === table.id);
    const node = el("article", "schema-node");
    node.tabIndex = 0;
    node.dataset.tableId = table.id;
    node.setAttribute("aria-label", `${table.qualified_name}，${table.columns.length} 个字段，${related.length} 条外键关系`);
    const head = el("header", "node-head");
    head.append(el("strong", "", table.qualified_name), el("span", "", `${table.columns.length} COLS`));
    const columns = el("ul", "column-list");
    table.columns.forEach((column) => {
      const row = el("li", "column-row");
      row.dataset.columnId = column.id;
      row.append(
        el("span", "key-role", column.structural_role === "primary_key" ? "PK" : column.structural_role === "foreign_key" ? "FK" : ""),
        el("span", "column-name", column.name),
        el("span", "column-type", `${column.data_type}${column.nullable ? " ?" : ""}`),
      );
      columns.append(row);
    });
    node.append(head, columns);
    if (related.length) node.append(el("p", "relation-note", related.map((relationship) => relationshipLabel(relationship, tableById, columnById)).join(" · ")));
    node.addEventListener("focus", () => focusRelationships(table.id));
    node.addEventListener("blur", () => focusRelationships(null));
    nodes.append(node);

    const item = el("li");
    const link = el("button", "outline-link");
    link.type = "button";
    link.append(el("strong", "", table.name), el("span", "", String(table.columns.length)));
    link.addEventListener("click", () => { node.scrollIntoView({ block: "center", inline: "center" }); node.focus(); });
    item.append(link);
    outline.append(item);
  });
  requestAnimationFrame(drawRelationships);
}

async function previewDDL() {
  const form = document.querySelector("#ddl-form");
  if (!form.reportValidity()) return;
  const errorTarget = document.querySelector("#submit-error");
  clearError(errorTarget);
  const input = ddlInput();
  if (byteLength(input.ddl) > MAX_DDL_BYTES) return showError(errorTarget, null, "DDL 超过 262,144 bytes，请拆分后再预览");
  const button = document.querySelector("#preview-ddl");
  setBusy(button, true, "解析中…");
  setPreviewStatus("正在解析", "idle");
  try {
    const preview = await api("/api/v1/metadata/ddl-preview", { method: "POST", body: JSON.stringify({ source: input.source, dialect: "mysql", ddl: input.ddl }) });
    state.source = input.source;
    state.ddl = input.ddl;
    state.previewKey = previewKey(input.source, input.ddl);
    document.querySelector("#top-source").textContent = `source: ${input.source}`;
    renderSchema(preview);
    setPreviewStatus("PREVIEW READY", "ready");
    announce(`结构预览完成：${preview.table_count} 张表，${preview.column_count} 个字段`);
  } catch (error) {
    setPreviewStatus("PREVIEW ERROR", "error");
    showError(errorTarget, error, "结构预览失败，请检查 DDL");
  } finally { setBusy(button, false); }
}

function setView() {
  const knowledge = location.pathname === "/knowledge";
  document.querySelector("#workbench-view").hidden = knowledge;
  document.querySelector("#knowledge-view").hidden = !knowledge;
  document.querySelectorAll("[data-nav]").forEach((link) => {
    const active = link.dataset.nav === (knowledge ? "knowledge" : "workbench");
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

function statusTone(status) {
  if (status === "succeeded") return "success";
  if (status === "failed" || status === "rejected") return "error";
  if (status === "pending" || status === "waiting_input") return "pending";
  return "running";
}

function inferStage(job) {
  if (job.status === "pending") return "queued";
  if (job.status === "waiting_input") return "waiting_input";
  if (TERMINAL.has(job.status)) return job.status;
  return "running";
}

function stageLabel(stage) {
  return STAGES.find(([key]) => key === stage)?.[1] || stage.replaceAll("_", " ");
}

function rememberStage(stage, emittedAt) {
  if (!state.stages.has(stage)) state.stages.set(stage, emittedAt || new Date().toISOString());
}

function renderTrace(currentStage) {
  const list = document.querySelector("#trace-list");
  list.replaceChildren();
  const reached = new Set(state.stages.keys());
  STAGES.forEach(([stage, label]) => {
    if (!reached.has(stage) && stage !== currentStage) return;
    const item = el("li");
    if (reached.has(stage) && stage !== currentStage) item.classList.add("reached");
    if (stage === currentStage) item.classList.add("current");
    if (stage === "waiting_input") item.classList.add("waiting");
    item.append(el("strong", "", label));
    const when = state.stages.get(stage);
    item.append(el("small", "", when ? new Date(when).toLocaleTimeString("zh-CN", { hour12: false }) : "当前阶段"));
    list.append(item);
  });
  if (!STAGES.some(([stage]) => stage === currentStage)) {
    const item = el("li", `current ${currentStage === "waiting_input" ? "waiting" : ""}`);
    item.append(el("strong", "", stageLabel(currentStage)), el("small", "", "当前阶段"));
    list.append(item);
  }
}

function renderTerminal(job) {
  const target = document.querySelector("#terminal-result");
  target.replaceChildren();
  target.hidden = !TERMINAL.has(job.status);
  target.classList.toggle("failed", job.status !== "succeeded");
  if (job.status === "succeeded" && job.result) {
    target.append(el("h3", "", "语义元数据已生成"));
    const counts = el("div", "result-counts");
    [[job.result.table_count, "表"], [job.result.column_count, "列"], [job.result.metric_count, "指标"]].forEach(([value, label]) => {
      const item = el("span"); item.append(el("strong", "", String(value)), el("small", "", label)); counts.append(item);
    });
    target.append(counts, el("p", "mono", `DDL 指纹 ${job.result.ddl_hash}`));
    const link = el("a", "", "到知识记忆核对口径 →"); link.href = `/knowledge?source=${encodeURIComponent(job.source)}`; target.append(link);
  } else if (TERMINAL.has(job.status)) {
    target.append(el("h3", "", job.status === "rejected" ? "任务已拒绝" : "任务处理失败"));
    const error = job.error || {};
    target.append(el("p", "mono", `${error.code || "unknown_error"} · ${error.stage || job.status}${error.retryable ? " · 可重试" : ""}`));
    target.append(el("p", "", error.retryable ? "可检查本机服务后重新提交这份 DDL。" : "请修正 DDL 或业务输入后重新提交。"));
  }
}

function renderQuestions(job) {
  const target = document.querySelector("#clarification");
  target.replaceChildren();
  const questions = job.status === "waiting_input" && job.question_set_id ? job.questions || [] : [];
  target.hidden = questions.length === 0;
  target.closest(".workbench-grid")?.classList.toggle("has-clarification", questions.length > 0);
  if (!questions.length) { setCanvasHighlight([]); return; }
  setCanvasHighlight(questions.flatMap((question) => [question.fact_table_id, ...(question.column_ids || [])].filter(Boolean)));
  target.append(el("h3", "", "需要你确认的业务含义"));
  questions.forEach((question) => {
    const card = el("div", "question-card");
    card.dataset.questionId = question.question_id;
    const related = [question.fact_table_id, ...(question.column_ids || [])].filter(Boolean).join(" · ");
    card.append(el("p", "question-meta", `${question.required ? "必答" : "可选"} · ${related}`));
    const label = el("label", "", question.prompt); label.htmlFor = `answer-${question.question_id}`;
    const input = el("textarea"); input.id = label.htmlFor; input.name = `answer_${question.question_id}`; input.autocomplete = "off"; input.rows = 3; input.required = question.required; input.dataset.answer = question.question_id;
    const actions = el("div", "question-actions");
    const draft = el("button", "quiet-action", "让 AI 起草"); draft.type = "button";
    draft.addEventListener("click", () => draftQuestion(question));
    actions.append(draft);
    card.append(label, input, actions);
    target.append(card);
  });
  const submit = el("button", "primary-action", "提交回答并继续 →"); submit.type = "button"; submit.id = "submit-answers";
  submit.addEventListener("click", submitAnswers);
  target.append(submit);
}

function renderJob(job, stage = inferStage(job), emittedAt) {
  state.job = { ...state.job, ...job };
  if (state.job.source) state.source = state.job.source;
  rememberStage(stage, emittedAt);
  document.querySelector("#task-source").textContent = `source: ${state.source || "unknown"}`;
  document.querySelector("#task-job").textContent = `job: ${state.job.job_id}`;
  const chip = document.querySelector("#task-status"); chip.textContent = STATUS_LABELS[state.job.status] || state.job.status; chip.dataset.tone = statusTone(state.job.status);
  renderTrace(stage);
  renderQuestions(state.job);
  renderTerminal(state.job);
  if (TERMINAL.has(state.job.status)) stopTaskUpdates();
}

function eventToJob(data) {
  return {
    job_id: data.job_id,
    source: state.source,
    status: data.status,
    revision: data.revision,
    attempt: data.attempt,
    // SSE 不包含提交回答所需的 question_set_id；waiting_input 必须先回读权威记录。
    questions: data.status === "waiting_input" ? null : data.questions,
    question_set_id: data.status === "waiting_input" ? null : state.job?.question_set_id,
    result: data.result,
    error: data.error,
  };
}

function stopTaskUpdates() {
  state.eventSource?.close(); state.eventSource = null;
  clearInterval(state.pollTimer); state.pollTimer = null;
  clearTimeout(state.reconnectTimer); state.reconnectTimer = null;
  document.querySelector("#connection-state").textContent = "任务已到达终态";
}

function startPolling() {
  state.eventSource?.close(); state.eventSource = null;
  clearTimeout(state.reconnectTimer);
  clearInterval(state.pollTimer);
  const connection = document.querySelector("#connection-state");
  connection.textContent = "事件流不可用，已切换状态查询";
  const poll = async () => {
    try {
      const job = await api(`/api/v1/metadata/ddl-jobs/${state.job.job_id}`);
      renderJob(job);
    } catch (error) {
      showError(document.querySelector("#task-error"), error, "无法查询任务状态");
    }
  };
  poll();
  state.pollTimer = setInterval(poll, 3000);
}

async function refreshJob() {
  return api(`/api/v1/metadata/ddl-jobs/${encodeURIComponent(state.job.job_id)}`);
}

function startEvents(eventsUrl) {
  if (!window.EventSource) return startPolling();
  const connection = document.querySelector("#connection-state");
  const source = new EventSource(eventsUrl);
  state.eventSource = source;
  source.onopen = () => { clearTimeout(state.reconnectTimer); connection.textContent = "事件流已连接"; };
  const receive = async (event) => {
    const data = JSON.parse(event.data);
    clearError(document.querySelector("#task-error"));
    renderJob(eventToJob(data), data.stage, data.emitted_at);
    if (data.status === "waiting_input") {
      try {
        renderJob(await refreshJob(), data.stage, data.emitted_at);
      } catch (error) {
        showError(document.querySelector("#task-error"), error, "无法读取当前问题，暂不能提交回答");
      }
    }
  };
  ["snapshot", "progress", "waiting_input", "succeeded", "rejected", "failed"].forEach((type) => source.addEventListener(type, receive));
  source.addEventListener("stream_error", () => startPolling());
  source.onerror = () => {
    if (TERMINAL.has(state.job?.status)) return;
    connection.textContent = "事件流中断，正在重连";
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = setTimeout(startPolling, 5000);
  };
}

async function submitDDL(event) {
  event.preventDefault();
  const errorTarget = document.querySelector("#submit-error"); clearError(errorTarget);
  ({ source: state.source, ddl: state.ddl } = ddlInput());
  if (byteLength(state.ddl) > MAX_DDL_BYTES) return showError(errorTarget, null, "DDL 超过 262,144 bytes，请拆分后再提交");
  const button = document.querySelector("#submit-ddl"); setBusy(button, true, "正在受理…");
  try {
    const accepted = await api("/api/v1/metadata/ddl-jobs", { method: "POST", body: JSON.stringify({ source: state.source, dialect: "mysql", ddl: state.ddl }) });
    history.replaceState(null, "", `/workbench/${accepted.job_id}`);
    state.stages.clear();
    renderJob({ ...accepted, source: state.source, revision: 0 });
    document.querySelector("#top-source").textContent = `source: ${state.source}`;
    announce("任务已受理");
    startEvents(accepted.events_url || `${accepted.status_url}/events`);
  } catch (error) {
    showError(errorTarget, error, "DDL 任务未受理，请检查输入或本机服务");
  } finally { setBusy(button, false); }
}

async function submitAnswers() {
  const errorTarget = document.querySelector("#task-error"); clearError(errorTarget);
  if (!state.job?.question_set_id) {
    try { renderJob(await refreshJob()); }
    catch (error) { return showError(errorTarget, error, "无法读取当前问题，暂不能提交回答"); }
  }
  if (!state.job?.question_set_id) return showError(errorTarget, null, "当前问题集尚未就绪，请稍后重试");
  const inputs = [...document.querySelectorAll("[data-answer]")];
  const missing = inputs.filter((input) => input.required && !input.value.trim());
  if (missing.length) { missing[0].focus(); return showError(errorTarget, null, "请先完成所有必答问题"); }
  const answers = inputs.filter((input) => input.value.trim()).map((input) => ({ question_id: input.dataset.answer, answer: input.value.trim() }));
  const button = document.querySelector("#submit-answers"); setBusy(button, true, "正在提交…");
  try {
    const job = await api(`/api/v1/metadata/ddl-jobs/${state.job.job_id}/answers`, { method: "POST", body: JSON.stringify({ revision: state.job.revision, question_set_id: state.job.question_set_id, answers }) });
    renderJob(job);
    announce("回答已提交，任务继续运行");
  } catch (error) {
    showError(errorTarget, error, error.status === 409 ? "问题版本已变化，请刷新当前任务后重新回答" : "回答未提交");
  } finally { setBusy(button, false); }
}

function addMessage(role, content, draftFor) {
  const target = document.querySelector("#chat-messages");
  const message = el("article", `message ${role}`);
  message.append(el("small", "", role === "user" ? "你" : "AI 协作"), el("p", "", content));
  if (role === "assistant" && draftFor) {
    const use = el("button", "", "采用为当前答案"); use.type = "button";
    use.addEventListener("click", () => {
      const input = document.querySelector(`[data-answer="${CSS.escape(draftFor)}"]`);
      if (input) { input.value = content; input.focus(); announce("AI 草稿已填入，确认后再提交"); }
    });
    message.append(use);
  }
  target.append(message); target.scrollTop = target.scrollHeight;
}

async function ensureConversation() {
  if (state.conversationUid) return state.conversationUid;
  const conversation = await api("/api/v1/conversations", { method: "POST", body: JSON.stringify({ user_id: state.userId }) });
  state.conversationUid = conversation.uid;
  browserSession?.setItem("schema-loom-conversation", conversation.uid);
  return conversation.uid;
}

function createChatAttempt(content, draftFor, turnUid = crypto.randomUUID()) {
  return { content, draftFor, turnUid, source: state.source, ddl: state.ddl };
}

function chatPayload(attempt) {
  return {
    user_id: state.userId,
    turn_uid: attempt.turnUid,
    content: attempt.content,
    ddl_context: { source: attempt.source, dialect: "mysql", ddl: attempt.ddl },
  };
}

async function sendChat(attempt, displayUser = true) {
  const errorTarget = document.querySelector("#chat-error"); clearError(errorTarget);
  const retry = document.querySelector("#retry-chat"); retry.hidden = true;
  if (state.failedChat && attempt.turnUid !== state.failedChat.turnUid) {
    retry.hidden = false;
    retry.focus();
    return showError(errorTarget, null, "请先重试未完成的上一轮，避免创建冲突轮次");
  }
  if (!state.ddl || !state.source) return showError(errorTarget, null, "聊天需要本机当前 DDL；从已知 job_id 恢复时原始 DDL 不会由 API 返回");
  if (displayUser) addMessage("user", attempt.content);
  const button = document.querySelector("#send-chat"); setBusy(button, true, "AI 正在整理…");
  try {
    const conversationUid = await ensureConversation();
    const response = await api(`/api/v1/conversations/${conversationUid}/chat-turns`, { method: "POST", body: JSON.stringify(chatPayload(attempt)) });
    state.failedChat = null;
    addMessage("assistant", response.message.content, attempt.draftFor);
  } catch (error) {
    if (error.status === 404 && state.conversationUid) { state.conversationUid = null; browserSession?.removeItem("schema-loom-conversation"); }
    state.failedChat = attempt;
    showError(errorTarget, error, "AI 回复未生成；消息状态以服务端会话为准，可重试本轮");
    retry.hidden = false;
  } finally { setBusy(button, false); }
}

async function draftQuestion(question) {
  state.activeQuestionId = question.question_id;
  const content = `请根据当前 DDL 起草这个澄清问题的回答，明确指出仍需我确认的业务假设：${question.prompt}`;
  await sendChat(createChatAttempt(content, question.question_id));
  state.activeQuestionId = null;
}

async function submitChat(event) {
  event.preventDefault();
  const input = document.querySelector("#chat-input"); const content = input.value.trim();
  if (!content) return;
  const current = ddlInput();
  if (current.source && current.ddl) ({ source: state.source, ddl: state.ddl } = current);
  input.value = "";
  await sendChat(createChatAttempt(content, state.activeQuestionId));
  state.activeQuestionId = null;
}

async function restoreKnownJob() {
  const match = location.pathname.match(/^\/workbench\/([^/]+)$/);
  if (!match) return;
  state.job = { job_id: decodeURIComponent(match[1]) };
  try {
    const job = await api(`/api/v1/metadata/ddl-jobs/${encodeURIComponent(state.job.job_id)}`);
    renderJob(job);
    document.querySelector("#source").value = job.source || "";
    document.querySelector("#top-source").textContent = `source: ${job.source || "unknown"}`;
    setPreviewStatus("DDL 未载入", "stale");
    if (!TERMINAL.has(job.status)) startEvents(`/api/v1/metadata/ddl-jobs/${encodeURIComponent(job.job_id)}/events`);
  } catch (error) { showError(document.querySelector("#submit-error"), error, "无法恢复这个本机任务"); }
}

function memorySummary(memory) {
  return memory.memory_text || memory.memory_key || memory.uid;
}

async function searchMemories(event) {
  event.preventDefault();
  const button = event.submitter || event.currentTarget.querySelector('button[type="submit"]');
  const source = document.querySelector("#memory-source").value.trim();
  const query = document.querySelector("#memory-query").value.trim();
  history.replaceState(null, "", `/knowledge?source=${encodeURIComponent(source)}&query=${encodeURIComponent(query)}`);
  const errorTarget = document.querySelector("#memory-error"); clearError(errorTarget);
  setBusy(button, true, "正在搜索…");
  try {
    const response = await api(`/api/v1/metadata/memories/search?source=${encodeURIComponent(source)}&query=${encodeURIComponent(query)}`);
    const list = document.querySelector("#memory-results"); list.replaceChildren();
    const empty = document.querySelector("#memory-empty"); empty.hidden = response.items.length > 0;
    empty.textContent = response.items.length ? "" : "没有找到权威记忆。换一个具体业务术语再试。";
    response.items.forEach((hit) => {
      const item = el("li"); const button = el("button", "memory-result"); button.type = "button";
      button.append(el("span", "", `${hit.memory.category} · v${hit.memory.record_version}`), el("strong", "", memorySummary(hit.memory)));
      button.addEventListener("click", () => openMemory(hit.memory.uid)); item.append(button); list.append(item);
    });
    if (response.degraded_targets?.length) announce(`部分检索信号已降级：${response.degraded_targets.join("、")}`);
  } catch (error) { showError(errorTarget, error, "知识搜索失败"); }
  finally { setBusy(button, false); }
}

function renderMemoryDetail(memory, historyPage) {
  state.memory = memory;
  const target = document.querySelector("#memory-detail"); target.replaceChildren();
  const label = el("div", "section-row"); label.append(el("h2", "", "记忆详情"), el("span", "", "RECORD")); target.append(label);
  const header = el("div", "detail-header");
  const title = el("div"); title.append(el("h3", "", memory.memory_key), el("p", "mono", `${memory.category} · v${memory.record_version} · ${memory.status}`)); header.append(title); target.append(header);
  target.append(el("p", "", memory.memory_text), el("pre", "detail-content", JSON.stringify(memory.content, null, 2)));
  const actions = el("div", "detail-actions");
  const edit = el("button", "secondary-action", "修正知识"); edit.type = "button"; edit.addEventListener("click", () => showMemoryEditor(target));
  const remove = el("button", "danger-action", "软删除…"); remove.type = "button"; remove.addEventListener("click", () => document.querySelector("#delete-dialog").showModal());
  actions.append(edit, remove); target.append(actions, el("h3", "", "版本历史"));
  const history = el("ul", "history-list");
  historyPage.items.forEach((event) => { const item = el("li"); item.append(el("strong", "", event.event_type), el("span", "", ` · ${new Date(event.created_at).toLocaleString("zh-CN")} · ${event.actor_type}`)); history.append(item); });
  if (!historyPage.items.length) history.append(el("li", "", "暂无历史事件")); target.append(history);
}

async function openMemory(uid) {
  const errorTarget = document.querySelector("#memory-error"); clearError(errorTarget);
  try {
    const url = new URL(location.href); url.searchParams.set("memory", uid); history.replaceState(null, "", url);
    const [memory, historyPage] = await Promise.all([api(`/api/v1/metadata/memories/${encodeURIComponent(uid)}`), api(`/api/v1/metadata/memories/${encodeURIComponent(uid)}/history`)]);
    renderMemoryDetail(memory, historyPage);
  } catch (error) { showError(errorTarget, error, "无法读取记忆详情"); }
}

function showMemoryEditor(target) {
  target.querySelector(".edit-memory")?.remove();
  const form = el("form", "edit-memory");
  const label = el("label", "", "结构化内容（JSON）"); label.htmlFor = "memory-content";
  const input = el("textarea"); input.id = "memory-content"; input.name = "memory_content"; input.autocomplete = "off"; input.value = JSON.stringify(state.memory.content, null, 2); input.spellcheck = false;
  const note = el("p", "field-note", "修正会创建新版本，并要求重新处理对应 DDL；不会直接改写当前 Meta 快照。");
  const save = el("button", "primary-action", "保存修正"); save.type = "submit";
  form.append(label, input, note, save);
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); clearError(document.querySelector("#memory-error"));
    let content; try { content = JSON.parse(input.value); } catch { input.focus(); return showError(document.querySelector("#memory-error"), null, "结构化内容不是有效 JSON"); }
    setBusy(save, true, "正在保存…");
    try {
      const result = await api(`/api/v1/metadata/memories/${encodeURIComponent(state.memory.uid)}`, { method: "PATCH", body: JSON.stringify({ content, expected_version: state.memory.record_version }) });
      announce(result.requires_reprocess ? "修正已保存，需要重新处理 DDL" : "修正已保存");
      await openMemory(state.memory.uid);
    } catch (error) { showError(document.querySelector("#memory-error"), error, error.status === 409 ? "记忆版本已变化，请重新打开详情" : "修正未保存"); }
    finally { setBusy(save, false); }
  });
  target.append(form); input.focus();
}

async function deleteMemory(event) {
  if (event.submitter?.value !== "confirm" || !state.memory) return;
  event.preventDefault();
  const button = event.submitter; setBusy(button, true, "正在软删除…");
  try {
    await api(`/api/v1/metadata/memories/${encodeURIComponent(state.memory.uid)}?expected_version=${state.memory.record_version}`, { method: "DELETE" });
    document.querySelector("#delete-dialog").close();
    document.querySelector("#memory-detail").replaceChildren(el("p", "empty-state", "这条知识已软删除，不再参与后续召回。"));
    announce("知识已软删除");
  } catch (error) { document.querySelector("#delete-dialog").close(); showError(document.querySelector("#memory-error"), error, error.status === 409 ? "记忆版本已变化，请重新打开详情" : "软删除失败"); }
  finally { setBusy(button, false); }
}

function initialize() {
  setView();
  document.querySelector("#ddl-form").addEventListener("submit", submitDDL);
  document.querySelector("#preview-ddl").addEventListener("click", previewDDL);
  document.querySelector("#chat-form").addEventListener("submit", submitChat);
  document.querySelector("#retry-chat").addEventListener("click", () => {
    if (state.failedChat) sendChat(state.failedChat, false);
  });
  document.querySelector("#memory-search-form").addEventListener("submit", searchMemories);
  document.querySelector("#delete-dialog form").addEventListener("submit", deleteMemory);
  const ddl = document.querySelector("#ddl");
  ddl.addEventListener("input", () => {
    const bytes = byteLength(ddl.value); const counter = document.querySelector("#ddl-count");
    counter.textContent = `${bytes.toLocaleString("en-US")} / ${MAX_DDL_BYTES.toLocaleString("en-US")} bytes`;
    counter.parentElement.classList.toggle("over-limit", bytes > MAX_DDL_BYTES);
    if (state.previewKey && previewKey(document.querySelector("#source").value.trim(), ddl.value.trim()) !== state.previewKey) setPreviewStatus("PREVIEW STALE", "stale");
  });
  document.querySelector("#source").addEventListener("input", () => {
    if (state.previewKey && previewKey(document.querySelector("#source").value.trim(), ddl.value.trim()) !== state.previewKey) setPreviewStatus("PREVIEW STALE", "stale");
  });
  const params = new URLSearchParams(location.search);
  const querySource = params.get("source");
  if (querySource) document.querySelector("#memory-source").value = querySource;
  const query = params.get("query"); const memoryUid = params.get("memory");
  if (query) document.querySelector("#memory-query").value = query;
  if (memoryUid && location.pathname === "/knowledge") openMemory(memoryUid);
  window.addEventListener("beforeunload", (event) => {
    if (!state.job && document.querySelector("#ddl").value.trim()) event.preventDefault();
  });
  window.addEventListener("resize", drawRelationships);
  restoreKnownJob();
}

function selfCheck() {
  console.assert(byteLength("表") === 3, "UTF-8 byte count");
  console.assert(statusTone("succeeded") === "success", "terminal tone");
  console.assert(inferStage({ status: "waiting_input" }) === "waiting_input", "status projection");
  const attempt = createChatAttempt("重试内容", null, "turn-stable");
  console.assert(chatPayload(attempt).turn_uid === chatPayload(attempt).turn_uid, "chat retry turn_uid");
  console.assert(eventToJob({ status: "waiting_input" }).question_set_id === null, "SSE questions require authoritative GET");
  console.log("Schema Loom self-check passed");
}

if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", initialize);
if (typeof process !== "undefined" && process.argv?.includes("--self-check")) selfCheck();
