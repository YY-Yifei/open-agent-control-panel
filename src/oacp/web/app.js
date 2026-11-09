/* 智能体面板 —— 会话为中心的前端。
 *
 * 界面上只有三样东西：智能体、会话、消息。A2A 的 task / contextId / JSON-RPC
 * 都留在服务端，前端一次也不需要知道。三个静态文件，没有构建步骤。
 */

const API = "/api/panel";
const TOKEN_KEY = "oacp.token";
const LANG_KEY = "oacp.lang";
const MANAGE_KEY = "oacp.manageTokens";

/* ------------------------------------------------------------------ 文案 */

const DICT = {
  zh: {
    brand: "智能体面板",
    search: "搜索智能体",
    agents: "智能体",
    sessions: "会话",
    "session.new": "新建",
    "session.untitled": "新会话",
    add: "添加智能体",
    send: "发送",
    stop: "停止",
    rename: "重命名",
    delete: "删除",
    info: "信息",
    remove: "移除",
    cancel: "取消",
    save: "保存",
    loading: "加载中…",

    "composer.placeholder": "输入消息，Enter 发送，Shift+Enter 换行",
    "composer.busy": "对方正在处理，请稍候…",

    "state.idle": "空闲",
    "state.working": "正在处理",
    "state.waiting": "等待回复",
    "state.done": "已完成",
    "state.failed": "出错",
    "state.canceled": "已停止",
    "state.ok": "在线",
    "state.down": "离线",

    "agents.empty": "没有匹配的智能体",
    "agents.none": "面板里还没有智能体",
    "agents.count": "{n} 个会话",
    "sessions.empty": "还没有会话",
    "sessions.none": "点「新建」开始一段对话",

    "empty.noAgent": "选择一个智能体开始对话",
    "empty.noAgent.hint": "左侧列出了面板里所有的智能体。",
    "empty.noAgents": "面板里还没有智能体",
    "empty.noAgents.hint": "智能体自己会来注册，也可以手动添加它的 agent card 地址。",
    "empty.chat": "还没有消息",
    "empty.chat.hint": "在下面说第一句话，这段会话就开始了。",
    pending: "正在处理…",

    "add.title": "添加智能体",
    "add.sub": "填入 agent card 的地址，面板会去取这张卡片并登记。",
    "add.label": "Agent card 地址",
    "add.placeholder": "https://agent.example.com/.well-known/agent-card.json",
    "add.hint": "地址以 /.well-known/agent-card.json 结尾，是 A2A 约定的卡片位置。",
    "add.submit": "添加",
    "add.fetching": "正在获取…",
    "add.ok": "已添加",
    "add.exists": "这个智能体已经在面板里了",

    "token.title": "需要面板口令",
    "token.sub": "这个面板要求 X-Panel-Token 才能访问。口令只存在这台浏览器里。",
    "token.label": "口令",
    "token.submit": "确定",
    "token.required": "需要面板口令",

    "rename.title": "重命名会话",
    "rename.label": "标题",
    "rename.ok": "已重命名",

    "delete.title": "删除这段会话？",
    "delete.sub": "只删除面板里的这段对话；智能体那边的内容不会被改动。",
    "delete.ok": "已删除",

    "remove.title": "移除这个智能体？",
    "remove.sub": "它会从面板的注册表里消失；已有的会话仍然可以查看。",
    "remove.ok": "已移除",

    "info.title": "智能体信息",
    "info.description": "简介",
    "info.skills": "技能",
    "info.status": "状态",
    "info.seen": "最近一次成功获取",
    "info.sessions": "会话",

    "time.just": "刚刚",
    "time.minutes": "{n} 分钟前",
    "time.hours": "{n} 小时前",
    "error.generic": "出错了",
  },
  en: {
    brand: "Agents",
    search: "Search agents",
    agents: "Agents",
    sessions: "Conversations",
    "session.new": "New",
    "session.untitled": "New conversation",
    add: "Add agent",
    send: "Send",
    stop: "Stop",
    rename: "Rename",
    delete: "Delete",
    info: "Info",
    remove: "Remove",
    cancel: "Cancel",
    save: "Save",
    loading: "Loading…",

    "composer.placeholder": "Type a message — Enter to send, Shift+Enter for a new line",
    "composer.busy": "The agent is working…",

    "state.idle": "Idle",
    "state.working": "Working",
    "state.waiting": "Waiting for you",
    "state.done": "Done",
    "state.failed": "Failed",
    "state.canceled": "Stopped",
    "state.ok": "Online",
    "state.down": "Offline",

    "agents.empty": "No matching agents",
    "agents.none": "No agents yet",
    "agents.count": "{n} conversations",
    "sessions.empty": "No conversations yet",
    "sessions.none": "Press New to start one",

    "empty.noAgent": "Pick an agent to start talking",
    "empty.noAgent.hint": "Every agent on this panel is listed on the left.",
    "empty.noAgents": "No agents on this panel yet",
    "empty.noAgents.hint": "Agents register themselves, or you can add one by its agent card URL.",
    "empty.chat": "No messages yet",
    "empty.chat.hint": "Say something below and the conversation begins.",
    pending: "Working…",

    "add.title": "Add an agent",
    "add.sub": "Give the URL of an agent card and the panel will fetch and register it.",
    "add.label": "Agent card URL",
    "add.placeholder": "https://agent.example.com/.well-known/agent-card.json",
    "add.hint": "The card lives at /.well-known/agent-card.json by A2A convention.",
    "add.submit": "Add",
    "add.fetching": "Fetching…",
    "add.ok": "Added",
    "add.exists": "This agent was already on the panel",

    "token.title": "Panel token required",
    "token.sub": "This panel asks for X-Panel-Token. It is kept in this browser only.",
    "token.label": "Token",
    "token.submit": "OK",
    "token.required": "A panel token is required",

    "rename.title": "Rename conversation",
    "rename.label": "Title",
    "rename.ok": "Renamed",

    "delete.title": "Delete this conversation?",
    "delete.sub": "Only the panel's copy is removed; the agent keeps what it has.",
    "delete.ok": "Deleted",

    "remove.title": "Remove this agent?",
    "remove.sub": "It leaves the registry. Its conversations stay readable.",
    "remove.ok": "Removed",

    "info.title": "Agent",
    "info.description": "About",
    "info.skills": "Skills",
    "info.status": "Status",
    "info.seen": "Last fetched",
    "info.sessions": "Conversations",

    "time.just": "just now",
    "time.minutes": "{n} min ago",
    "time.hours": "{n} h ago",
    "error.generic": "Something went wrong",
  },
};

let lang = detectLang();

function detectLang() {
  const stored = localStorage.getItem(LANG_KEY);
  if (stored === "zh" || stored === "en") return stored;
  return (navigator.language || "").toLowerCase().startsWith("en") ? "en" : "zh";
}

function t(key, vars) {
  const template = (DICT[lang] && DICT[lang][key]) || (DICT.zh && DICT.zh[key]) || key;
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (whole, name) =>
    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole,
  );
}

function stateLabel(code) {
  const value = (DICT[lang] || {})[`state.${code}`] || (DICT.zh || {})[`state.${code}`];
  return value || code || "";
}

/* ------------------------------------------------------------------ 工具 */

const $ = (selector) => document.querySelector(selector);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);
}

function fmtTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString(lang === "zh" ? "zh-CN" : "en-GB", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function relTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const seconds = (Date.now() - date.getTime()) / 1000;
  if (seconds < 60) return t("time.just");
  if (seconds < 3600) return t("time.minutes", { n: Math.floor(seconds / 60) });
  if (seconds < 86400) return t("time.hours", { n: Math.floor(seconds / 3600) });
  return date.toLocaleDateString(lang === "zh" ? "zh-CN" : "en-GB", {
    month: "short",
    day: "numeric",
  });
}

function toast(message, kind = "ok") {
  const root = $("#toast-root");
  const node = document.createElement("div");
  node.className = `toast ${kind === "error" ? "error" : ""}`;
  node.textContent = message;
  root.appendChild(node);
  setTimeout(() => node.remove(), kind === "error" ? 5000 : 2400);
}

function autosize(textarea) {
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
}

/* -------------------------------------------------------------- API 层 */

async function api(path, options = {}) {
  const headers = { "accept-language": lang === "zh" ? "zh-CN,zh;q=0.9" : "en" };
  const init = { ...options, headers };
  if (options.body !== undefined && typeof options.body !== "string") {
    headers["content-type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const token = localStorage.getItem(TOKEN_KEY) || "";
  if (token) headers["x-panel-token"] = token;

  const response = await fetch(path, init);
  if (response.status === 401 && !options.noAuthPrompt) {
    const payload = await response.json().catch(() => null);
    if (payload && payload.code === "panel_token_required") {
      openTokenDialog();
      throw new Error(t("token.required"));
    }
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail = payload && (payload.detail || payload.message);
    const error = new Error(typeof detail === "string" && detail ? detail : t("error.generic"));
    // The card validator answers with a problem list under detail.problems; keep
    // the whole payload so the dialog can show it rather than one flat sentence.
    error.payload = payload;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

/* ------------------------------------------------------------------ 状态 */

const state = {
  agents: [],
  agentId: null,
  sessions: [],
  session: null,
  draft: false,
  q: "",
  busy: false,
};

let pollTimer = null;

/* ------------------------------------------------------------------ 渲染 */

function applyLang() {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  document.title = t("brand");
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((node) => {
    node.placeholder = t(node.dataset.i18nPh);
  });
  const toggle = $("#lang");
  if (toggle) toggle.textContent = lang === "zh" ? "English" : "中文";
}

function setLang(next) {
  lang = next === "en" ? "en" : "zh";
  localStorage.setItem(LANG_KEY, lang);
  applyLang();
  renderAll();
  const input = $("#input");
  if (input) autosize(input);
}

function currentAgent() {
  return state.agents.find((agent) => agent.id === state.agentId) || null;
}

function renderAll() {
  renderAgents();
  renderSessions();
  renderTop();
  renderChat();
  renderComposer();
}

function renderAgents() {
  const box = $("#agent-list");
  $("#agent-count").textContent = String(state.agents.length);
  if (!state.agents.length) {
    box.innerHTML = `<div class="pad small muted">${esc(t("agents.none"))}</div>`;
    return;
  }
  const needle = state.q.trim().toLowerCase();
  const items = state.agents.filter((agent) => {
    if (!needle) return true;
    const haystack = `${agent.name || ""} ${agent.description || ""}`.toLowerCase();
    return haystack.includes(needle);
  });
  if (!items.length) {
    box.innerHTML = `<div class="pad small muted">${esc(t("agents.empty"))}</div>`;
    return;
  }
  box.innerHTML = items
    .map((agent) => {
      const sub = t("agents.count", { n: agent.sessionCount || 0 });
      const offline = agent.status === "down" ? ` · ${stateLabel("down")}` : "";
      return `<button class="item ${agent.id === state.agentId ? "active" : ""}"
                      data-agent="${esc(agent.id)}" type="button">
        <span class="dot ${agent.status === "down" ? "down" : "ok"}"></span>
        <span class="item-main">
          <span class="item-title">${esc(agent.name || agent.url)}</span>
          <span class="item-sub">${esc(sub)}${esc(offline)}</span>
        </span>
      </button>`;
    })
    .join("");
}

function renderSessions() {
  const group = $("#session-group");
  group.hidden = !state.agentId;
  const box = $("#session-list");
  if (!state.agentId) {
    box.innerHTML = "";
    return;
  }
  const rows = state.sessions.map((session) => {
    const active = state.session && session.id === state.session.id ? "active" : "";
    const working = session.state === "working" ? '<span class="dot working"></span>' : "";
    return `<button class="item ${active}" data-session="${esc(session.id)}" type="button">
      <span class="item-main">
        <span class="item-title">${esc(session.title || t("session.untitled"))}</span>
        <span class="item-sub">${esc(relTime(session.updatedAt))} · ${esc(stateLabel(session.state))}</span>
      </span>${working}
    </button>`;
  });
  if (state.draft) {
    rows.unshift(`<button class="item active" data-session="" type="button">
      <span class="item-main">
        <span class="item-title">${esc(t("session.untitled"))}</span>
        <span class="item-sub">${esc(t("sessions.none"))}</span>
      </span>
    </button>`);
  }
  box.innerHTML = rows.join("") || `<div class="pad small muted">${esc(t("sessions.empty"))}</div>`;
}

function renderTop() {
  const top = $("#top");
  const agent = currentAgent();
  if (!agent) {
    top.innerHTML = "";
    return;
  }
  const session = state.session;
  const crumb = session
    ? `<div class="crumb">
         <span>${esc(session.title || t("session.untitled"))}</span>
         <button class="link" data-act="rename" type="button">${esc(t("rename"))}</button>
         <button class="link" data-act="delete-session" type="button">${esc(t("delete"))}</button>
       </div>`
    : "";
  const stop =
    session && session.state === "working"
      ? `<button class="btn sm" data-act="stop" type="button">${esc(t("stop"))}</button>`
      : "";
  top.innerHTML = `
    <div class="agent-head">
      <div class="agent-line">
        <span class="dot ${agent.status === "down" ? "down" : "ok"}"></span>
        <h1>${esc(agent.name || agent.url)}</h1>
      </div>
      <p class="agent-desc">${esc(agent.description || "")}</p>
      ${crumb}
    </div>
    <div class="top-actions">
      ${stop}
      <button class="btn sm ghost" data-act="info" type="button">${esc(t("info"))}</button>
      <button class="btn sm ghost" data-act="remove-agent" type="button">${esc(t("remove"))}</button>
    </div>`;
}

function emptyState(title, hint) {
  return `<div class="empty"><h2>${esc(title)}</h2><p>${esc(hint)}</p></div>`;
}

function renderChat() {
  const chat = $("#chat");
  const agent = currentAgent();
  if (!agent) {
    chat.innerHTML = state.agents.length
      ? emptyState(t("empty.noAgent"), t("empty.noAgent.hint"))
      : `${emptyState(t("empty.noAgents"), t("empty.noAgents.hint"))}
         <div class="empty"><button class="btn primary" data-act="add-agent" type="button">${esc(t("add"))}</button></div>`;
    return;
  }
  const session = state.session;
  const messages = session ? session.messages : [];
  if (!messages.length) {
    chat.innerHTML = emptyState(t("empty.chat"), t("empty.chat.hint"));
    return;
  }
  const rows = messages.map((message) => {
    const role = message.role === "user" ? "user" : message.role === "error" ? "error" : "agent";
    return `<div class="msg ${role}">
      <div class="bubble">${esc(message.text)}</div>
      <div class="meta">${esc(fmtTime(message.at))}</div>
    </div>`;
  });
  if (session && session.state === "working") {
    rows.push(`<div class="msg pending"><div class="pending">${esc(t("pending"))}</div></div>`);
  }
  const nearBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 120;
  chat.innerHTML = rows.join("");
  if (nearBottom || chat.scrollTop === 0) chat.scrollTop = chat.scrollHeight;
}

function renderComposer() {
  const form = $("#composer");
  form.hidden = !state.agentId;
  const input = $("#input");
  if (form.hidden) return;
  const working = Boolean(state.session && state.session.state === "working");
  const blocked = state.busy || working;
  input.disabled = blocked;
  input.placeholder = working ? t("composer.busy") : t("composer.placeholder");
  $("#send").disabled = blocked || !input.value.trim();
}

/* ------------------------------------------------------------------ 路由 */

function parseRoute() {
  const raw = location.hash.replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean);
  if (parts[0] === "a" && parts[1]) {
    return {
      agentId: decodeURIComponent(parts[1]),
      sessionId: parts[2] ? decodeURIComponent(parts[2]) : null,
    };
  }
  return { agentId: null, sessionId: null };
}

function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

async function route() {
  stopPolling();
  const { agentId, sessionId } = parseRoute();

  if (!agentId) {
    state.agentId = null;
    state.sessions = [];
    state.session = null;
    state.draft = false;
    renderAll();
    return;
  }

  if (agentId !== state.agentId) {
    state.agentId = agentId;
    state.sessions = [];
    state.session = null;
    state.draft = false;
  }
  if (!currentAgent()) await loadAgents().catch(() => {});
  if (!currentAgent()) {
    // The agent left the registry while this URL was in the address bar.
    location.replace("#/");
    return;
  }
  await loadSessions().catch(() => {});

  if (sessionId === "new") {
    state.session = null;
    state.draft = true;
    renderAll();
    return;
  }
  if (!sessionId) {
    const first = state.sessions[0];
    location.replace(`#/a/${encodeURIComponent(agentId)}/${first ? first.id : "new"}`);
    return;
  }
  await openSession(sessionId);
}

/* ------------------------------------------------------------------ 数据 */

async function loadSummary() {
  try {
    const summary = await api(`${API}/summary`);
    $("#version").textContent = summary.version || "";
  } catch (error) {
    if (error.message !== t("token.required")) toast(error.message, "error");
  }
}

async function loadAgents() {
  const data = await api(`${API}/agents?limit=100`);
  state.agents = data.items || [];
  renderAgents();
}

async function loadSessions() {
  if (!state.agentId) {
    state.sessions = [];
    return;
  }
  const data = await api(`${API}/sessions?agentId=${encodeURIComponent(state.agentId)}`);
  state.sessions = data.items || [];
  renderSessions();
}

async function refreshSessions() {
  await loadSessions().catch(() => {});
  renderSessions();
}

async function openSession(id) {
  try {
    const data = await api(`${API}/sessions/${encodeURIComponent(id)}`);
    state.session = data.session;
    state.draft = false;
  } catch (error) {
    state.session = null;
    state.draft = true;
    toast(error.message, "error");
    return renderAll();
  }
  renderAll();
  if (state.session.state === "working") startPolling();
  else stopPolling();
}

/* ------------------------------------------------------------------ 发送 */

async function send() {
  const input = $("#input");
  const text = input.value.trim();
  if (!text || state.busy || !state.agentId) return;
  if (state.session && state.session.state === "working") return;

  state.busy = true;
  renderComposer();
  try {
    const creating = !state.session || state.draft;
    const data = creating
      ? await api(`${API}/agents/${encodeURIComponent(state.agentId)}/sessions`, {
          method: "POST",
          body: { text },
        })
      : await api(`${API}/sessions/${encodeURIComponent(state.session.id)}/messages`, {
          method: "POST",
          body: { text },
        });

    input.value = "";
    autosize(input);
    state.busy = false;

    if (creating) {
      // 交给路由重新打开：会话列表、地址栏、当前会话一次对齐。
      state.session = data.session;
      state.draft = false;
      location.replace(
        `#/a/${encodeURIComponent(state.agentId)}/${encodeURIComponent(data.session.id)}`,
      );
      return;
    }

    state.session = data.session;
    renderAll();
    await refreshSessions();
    renderChat();
    if (state.session.state === "working") startPolling();
  } catch (error) {
    state.busy = false;
    renderComposer();
    toast(error.message, "error");
  }
}

async function stopTurn() {
  if (!state.session) return;
  try {
    const data = await api(`${API}/sessions/${encodeURIComponent(state.session.id)}/cancel`, {
      method: "POST",
    });
    state.session = data.session;
    renderAll();
    await refreshSessions();
    renderChat();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ------------------------------------------------------------------ 轮询 */

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function startPolling() {
  stopPolling();
  const id = state.session && state.session.id;
  if (!id) return;
  let misses = 0;

  const tick = async () => {
    pollTimer = null;
    if (!state.session || state.session.id !== id) return;
    if (state.session.state !== "working") return;

    try {
      const data = await api(`${API}/sessions/${encodeURIComponent(id)}/sync`, {
        method: "POST",
      });
      misses = 0;
      if (!state.session || state.session.id !== id) return;
      const previous = state.session;
      const changed =
        data.session.messages.length !== previous.messages.length ||
        data.session.state !== previous.state;
      state.session = data.session;
      if (changed) {
        renderTop();
        renderChat();
        renderComposer();
        await refreshSessions();
      }
    } catch (error) {
      if (++misses >= 3) return; // 连续失败就停下，用户可以手动刷新页面
    }

    if (state.session && state.session.state === "working") {
      pollTimer = setTimeout(tick, 1500);
    } else {
      renderAll();
      await refreshSessions();
    }
  };

  pollTimer = setTimeout(tick, 1200);
}

/* ------------------------------------------------------------------ 动作 */

async function handleAction(action) {
  switch (action) {
    case "rename":
      return openRenameDialog();
    case "delete-session":
      return openDeleteSessionDialog();
    case "stop":
      return stopTurn();
    case "info":
      return openInfoDialog();
    case "remove-agent":
      return openRemoveAgentDialog();
    case "add-agent":
      return openAddDialog();
    default:
      return undefined;
  }
}

/* ------------------------------------------------------------------ 弹窗 */

function openModal(html) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-backdrop" data-close="1">
    <div class="modal" role="dialog" aria-modal="true">${html}</div>
  </div>`;
  document.body.style.overflow = "hidden";
  const focusable = root.querySelector("input, textarea, button");
  if (focusable) setTimeout(() => focusable.focus(), 0);
}

function closeModal() {
  $("#modal-root").innerHTML = "";
  document.body.style.overflow = "";
}

function confirmDialog({ title, sub, ok, onConfirm }) {
  openModal(`
    <h2>${esc(title)}</h2>
    <p class="sub">${esc(sub)}</p>
    <footer>
      <button class="btn ghost" data-close="1" type="button">${esc(t("cancel"))}</button>
      <button class="btn primary" id="confirm-ok" type="button">${esc(ok)}</button>
    </footer>`);
  $("#confirm-ok").addEventListener("click", () => {
    closeModal();
    onConfirm();
  });
}

function openRenameDialog() {
  const session = state.session;
  if (!session) return;
  openModal(`
    <h2>${esc(t("rename.title"))}</h2>
    <form id="rename-form">
      <div class="field">
        <label for="rename-input">${esc(t("rename.label"))}</label>
        <input id="rename-input" value="${esc(session.title || "")}" maxlength="80" required />
      </div>
      <footer>
        <button class="btn ghost" data-close="1" type="button">${esc(t("cancel"))}</button>
        <button class="btn primary" type="submit">${esc(t("save"))}</button>
      </footer>
    </form>`);

  $("#rename-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const title = $("#rename-input").value.trim();
    if (!title) return;
    try {
      const data = await api(`${API}/sessions/${encodeURIComponent(session.id)}`, {
        method: "PATCH",
        body: { title },
      });
      state.session = data.session;
      closeModal();
      renderTop();
      renderChat();
      await refreshSessions();
      toast(t("rename.ok"));
    } catch (error) {
      toast(error.message, "error");
    }
  });
}

function openDeleteSessionDialog() {
  const session = state.session;
  if (!session) return;
  confirmDialog({
    title: t("delete.title"),
    sub: t("delete.sub"),
    ok: t("delete"),
    onConfirm: async () => {
      try {
        await api(`${API}/sessions/${encodeURIComponent(session.id)}`, { method: "DELETE" });
        state.session = null;
        state.draft = true;
        await refreshSessions();
        go(`#/a/${encodeURIComponent(state.agentId)}/new`);
        toast(t("delete.ok"));
      } catch (error) {
        toast(error.message, "error");
      }
    },
  });
}

function openInfoDialog() {
  const agent = currentAgent();
  if (!agent) return;
  const skills = Array.isArray(agent.skills) && agent.skills.length
    ? agent.skills.join("、")
    : "—";
  openModal(`
    <h2>${esc(t("info.title"))}</h2>
    <dl class="kv">
      <dt>${esc(t("agents"))}</dt><dd>${esc(agent.name || agent.url)}</dd>
      <dt>${esc(t("info.description"))}</dt><dd>${esc(agent.description || "—")}</dd>
      <dt>${esc(t("info.skills"))}</dt><dd>${esc(skills)}</dd>
      <dt>${esc(t("info.status"))}</dt><dd>${esc(stateLabel(agent.status))}</dd>
      <dt>${esc(t("info.seen"))}</dt><dd>${esc(fmtTime(agent.lastOkAt) || "—")}</dd>
      <dt>${esc(t("info.sessions"))}</dt><dd>${esc(String(agent.sessionCount || 0))}</dd>
    </dl>
    <footer>
      <button class="btn" data-close="1" type="button">${esc(t("cancel"))}</button>
    </footer>`);
}

function openRemoveAgentDialog() {
  const agent = currentAgent();
  if (!agent) return;
  confirmDialog({
    title: t("remove.title"),
    sub: t("remove.sub"),
    ok: t("remove"),
    onConfirm: async () => {
      let tokens = {};
      try {
        tokens = JSON.parse(localStorage.getItem(MANAGE_KEY) || "{}");
      } catch {
        tokens = {};
      }
      try {
        await api(`${API}/agents/${encodeURIComponent(agent.id)}`, {
          method: "DELETE",
          body: { token: tokens[agent.id] || "" },
        });
        delete tokens[agent.id];
        localStorage.setItem(MANAGE_KEY, JSON.stringify(tokens));
        state.agentId = null;
        state.session = null;
        state.sessions = [];
        await loadAgents();
        go("#/");
        toast(t("remove.ok"));
      } catch (error) {
        toast(error.message, "error");
      }
    },
  });
}

function openAddDialog() {
  openModal(`
    <h2>${esc(t("add.title"))}</h2>
    <p class="sub">${esc(t("add.sub"))}</p>
    <div id="add-error"></div>
    <form id="add-form">
      <div class="field">
        <label for="add-url">${esc(t("add.label"))}</label>
        <input id="add-url" type="url" required spellcheck="false"
               placeholder="${esc(t("add.placeholder"))}" />
        <span class="hint">${esc(t("add.hint"))}</span>
      </div>
      <footer>
        <button class="btn ghost" data-close="1" type="button">${esc(t("cancel"))}</button>
        <button class="btn primary" type="submit">${esc(t("add.submit"))}</button>
      </footer>
    </form>`);

  $("#add-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = $("#add-url").value.trim();
    if (!url) return;
    const submit = event.target.querySelector("button[type=submit]");
    submit.disabled = true;
    submit.textContent = t("add.fetching");
    try {
      const result = await api("/v1/agents", {
        method: "POST",
        body: { url },
        noAuthPrompt: true,
      });
      if (result.manageToken) {
        let tokens = {};
        try {
          tokens = JSON.parse(localStorage.getItem(MANAGE_KEY) || "{}");
        } catch {
          tokens = {};
        }
        tokens[result.id] = result.manageToken;
        localStorage.setItem(MANAGE_KEY, JSON.stringify(tokens));
        toast(t("add.ok"));
      } else {
        toast(t("add.exists"));
      }
      closeModal();
      await loadAgents();
      go(`#/a/${encodeURIComponent(result.id)}`);
    } catch (error) {
      submit.disabled = false;
      submit.textContent = t("add.submit");
      const detail = error.payload && error.payload.detail;
      const problems = (detail && detail.problems) || [];
      $("#add-error").innerHTML = `<div class="alert">${esc(error.message)}${
        problems.length ? `<br>${problems.map(esc).join("<br>")}` : ""
      }</div>`;
    }
  });
}

function openTokenDialog() {
  if ($("#token-form")) return;
  openModal(`
    <h2>${esc(t("token.title"))}</h2>
    <p class="sub">${esc(t("token.sub"))}</p>
    <form id="token-form">
      <div class="field">
        <label for="token-input">${esc(t("token.label"))}</label>
        <input id="token-input" type="password" autocomplete="off" required />
      </div>
      <footer>
        <button class="btn ghost" data-close="1" type="button">${esc(t("cancel"))}</button>
        <button class="btn primary" type="submit">${esc(t("token.submit"))}</button>
      </footer>
    </form>`);

  $("#token-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = $("#token-input").value.trim();
    if (!value) return;
    localStorage.setItem(TOKEN_KEY, value);
    location.reload();
  });
}

/* ------------------------------------------------------------------ 启动 */

function bindDom() {
  $("#search").addEventListener("input", (event) => {
    state.q = event.target.value;
    renderAgents();
  });

  $("#add-agent").addEventListener("click", () => openAddDialog());
  $("#lang").addEventListener("click", () => setLang(lang === "zh" ? "en" : "zh"));
  $("#new-session").addEventListener("click", () => {
    if (state.agentId) go(`#/a/${encodeURIComponent(state.agentId)}/new`);
  });

  $("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    send();
  });

  const input = $("#input");
  input.addEventListener("input", () => {
    autosize(input);
    $("#send").disabled = state.busy || !input.value.trim();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });

  document.addEventListener("click", (event) => {
    const close = event.target.closest("[data-close]");
    if (close) {
      closeModal();
      return;
    }
    const agent = event.target.closest("[data-agent]");
    if (agent) {
      go(`#/a/${encodeURIComponent(agent.dataset.agent)}`);
      return;
    }
    const session = event.target.closest("[data-session]");
    if (session) {
      const id = session.dataset.session;
      const base = `#/a/${encodeURIComponent(state.agentId)}`;
      go(id ? `${base}/${encodeURIComponent(id)}` : `${base}/new`);
      return;
    }
    const action = event.target.closest("[data-act]");
    if (action) handleAction(action.dataset.act);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeModal();
  });

  window.addEventListener("hashchange", () => route());
}

async function boot() {
  applyLang();
  bindDom();
  await loadSummary();
  await loadAgents().catch((error) => toast(error.message, "error"));
  await route();
}

boot();
