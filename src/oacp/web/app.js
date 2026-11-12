/* 智能体面板 —— 会话为中心的前端。
 *
 * 界面上只有三样东西：智能体、会话、消息。A2A 的 task / contextId / JSON-RPC
 * 都留在服务端，前端一次也不需要知道。四个静态文件，没有构建步骤。
 */

import { renderMarkdown } from "./md.js";
import {
  IMAGE_TYPES,
  fitPlan,
  isAnimatedImage,
  pasteFiles,
  pastedFiles,
  sniffImageType,
} from "./paste.js";

const API = "/api/panel";
const TOKEN_KEY = "oacp.token";
const LANG_KEY = "oacp.lang";
const THEME_KEY = "oacp.theme";
const MANAGE_KEY = "oacp.manageTokens";

// 图片：真正的上限由 /summary 给出（服务端是唯一真源），这里的值是「还没拿到
// summary 之前」的兜底，好让选图这一刻就能拦住明显太大的文件。
// 可选类型只有一份，在 paste.js —— 粘贴也要用同一张表判「这是不是图」。
const IMAGE_LIMITS = { maxBytes: 5 * 1024 * 1024, maxPerMessage: 4 };

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

    "attach.add": "添加图片",
    "attach.remove": "移除这张图",
    "attach.unsupported": "这个智能体的卡片声明只接受文本输入",
    "attach.tooBig": "{name} 超过 {max}",
    "attach.tooMany": "一条消息最多 {n} 张图片",
    "attach.type": "只支持 PNG、JPEG、WebP、GIF",
    "attach.failed": "这张图读不出来：{name}",
    "attach.shrunk": "{name} 超过 {max}，已压缩到 {size} 后附加",
    "attach.shrunkSize": "{width}×{height}，{size}",
    "attach.gifTooBig": "动图不压缩：{name} 超过 {max}，请截取区域或先压缩",
    "attach.drawerNoImages": "助手不收图片，请到主对话里粘贴",

    // 附件到了但没显示。存进库里的是原因码，文案在这一层选 —— 同一段会话
    // 换个语言再读，也仍然是它对的那一种语言。
    "attach.skip.too_large": "{name} 太大，没有显示",
    "attach.skip.unsupported": "{name} 不是能显示的图片",
    "attach.skip.bad_data": "{name} 的数据读不出来",
    "attach.skip.unreachable": "{name} 取不回来",
    "attach.skip.too_many": "{name} 超出一条消息的张数上限",

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
    "agent.title": "助手",
    "agent.drawer": "内置助手",
    "agent.close": "关闭",
    "agent.input": "内置助手输入框",
    "agent.online": "在线",
    "agent.offline": "未连接",
    "agent.elsewhere": "助手在另一个标签页运行",
    "agent.ready": "已连接，{n} 个能力可用",
    "agent.thinking": "正在回答…",
    "agent.placeholder": "问我面板里的事，或让我操作界面",
    "agent.clear": "清空",
    "agent.stop": "停止",
    "agent.cleared": "对话已清空",
    "agent.cancelled": "这一轮被取消了",
    "agent.failed": "这一轮失败了",
    "agent.empty": "这一轮没有文字输出",
    "agent.confirm.ok": "确认",
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

    "attach.add": "Add images",
    "attach.remove": "Remove this image",
    "attach.unsupported": "This agent's card declares text input only",
    "attach.tooBig": "{name} is larger than {max}",
    "attach.tooMany": "One message may carry at most {n} images",
    "attach.type": "Only PNG, JPEG, WebP and GIF are supported",
    "attach.failed": "This image could not be read: {name}",
    "attach.shrunk": "{name} was over {max}; attached at {size}",
    "attach.shrunkSize": "{width}×{height}, {size}",
    "attach.gifTooBig": "Animated images are not re-encoded: {name} is over {max}",
    "attach.drawerNoImages": "The assistant takes no images — paste in the main conversation",

    "attach.skip.too_large": "{name} was too large to show",
    "attach.skip.unsupported": "{name} is not an image this panel can show",
    "attach.skip.bad_data": "{name} could not be read",
    "attach.skip.unreachable": "{name} could not be fetched",
    "attach.skip.too_many": "{name} is over the per-message limit",

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
    "agent.title": "Assistant",
    "agent.drawer": "Assistant panel",
    "agent.close": "Close",
    "agent.input": "Message to the assistant",
    "agent.online": "online",
    "agent.offline": "not connected",
    "agent.elsewhere": "The assistant is running in another tab",
    "agent.ready": "connected, {n} capabilities available",
    "agent.thinking": "Answering…",
    "agent.placeholder": "Ask about this panel, or tell me to change it",
    "agent.clear": "Clear",
    "agent.stop": "Stop",
    "agent.cleared": "Conversation cleared",
    "agent.cancelled": "The turn was cancelled",
    "agent.failed": "The turn failed",
    "agent.empty": "The turn produced no text",
    "agent.confirm.ok": "Confirm",
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
  node.className = "toast";
  if (kind === "error" || kind === "warn") node.classList.add(kind);
  node.textContent = message;
  root.appendChild(node);
  // A warning is something to act on ("the assistant is in another tab"), so it
  // lives as long as an error rather than blinking past.
  setTimeout(() => node.remove(), kind === "ok" ? 2400 : 5000);
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
  // 还没发出去的图片：{ name, mime, size, data(base64), preview(data URL) }
  pending: [],
  limits: { ...IMAGE_LIMITS },
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
  document.querySelectorAll("[data-i18n-title]").forEach((node) => {
    node.title = t(node.dataset.i18nTitle);
  });
  // aria-label 是可访问名，会盖过 title：写在 HTML 里的中文常量必须跟着语言走。
  document.querySelectorAll("[data-i18n-aria]").forEach((node) => {
    node.setAttribute("aria-label", t(node.dataset.i18nAria));
  });
  const toggle = $("#lang");
  if (toggle) toggle.textContent = lang === "zh" ? "English" : "中文";
}

/**
 * Apply a theme. `auto` removes the attribute so the stylesheet's
 * prefers-color-scheme block decides — the same block that already existed.
 */
function applyTheme(theme, { persist = true } = {}) {
  const value = theme === "light" || theme === "dark" ? theme : "auto";
  if (value === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = value;
  if (persist) {
    try {
      localStorage.setItem(THEME_KEY, value);
    } catch {
      /* private mode: the choice still holds for this page view */
    }
  }
  return value;
}

function setLang(next) {
  lang = next === "en" ? "en" : "zh";
  localStorage.setItem(LANG_KEY, lang);
  applyLang();
  renderAll();
  // The drawer's hint and thinking line are written by script, not `data-i18n`;
  // without this they keep the language they were last written in.
  if (window.oacp && window.oacp.agent && window.oacp.agent.relabel) window.oacp.agent.relabel();
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
    // 只有 agent 的回答走 Markdown。用户自己的输入与错误提示留在纯文本 ——
    // 否则人打一句「# 标题」会莫名其妙变成大标题，而错误文案本来就是服务端
    // 拼好的整句。
    const html = role === "agent" ? renderMarkdown(message.text) : esc(message.text);
    const cls = role === "agent" ? "bubble md" : "bubble";
    // 图与文分开渲染：文字要保留换行与转义，图要有自己的尺寸规则。src 先留空，
    // 由 hydrateAttachments 用带口令的请求填上 —— <img src> 带不了请求头。
    const files = (message.attachments || []).map(attachmentNode).join("");
    if (!html && !files) return "";
    return `<div class="msg ${role}">
      ${files ? `<div class="files">${files}</div>` : ""}
      ${html ? `<div class="${cls}">${html}</div>` : ""}
      <div class="meta">${esc(fmtTime(message.at))}</div>
    </div>`;
  });
  if (session && session.state === "working") {
    rows.push(`<div class="msg pending"><div class="pending">${esc(t("pending"))}</div></div>`);
  }
  const nearBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 120;
  chat.innerHTML = rows.join("");
  if (nearBottom || chat.scrollTop === 0) chat.scrollTop = chat.scrollHeight;
  // 图是异步到的：填完再贴一次底边，否则图一出来就把位置顶走。
  hydrateAttachments(chat, () => {
    if (nearBottom) chat.scrollTop = chat.scrollHeight;
  });
}

// 一条附件要么是张图（src 由 hydrateAttachments 后填），要么是一枚说明为什么
// 没有图的 chip。后者也占一行：agent 回了一个 PDF 这件事本身是要让人看见的，
// 静默丢掉才是真的丢信息。
function attachmentNode(file) {
  if (file.skipped) {
    const why = t(`attach.skip.${file.skipped}`, { name: file.name });
    // 原因要看得见，不能只藏在 title 里：触屏没有悬停，键盘也到不了这个 span。
    // 原因文案自带 {name}，所以文件名不会丢。
    return `<span class="attachment-skip" title="${esc(file.name)}">${esc(why)}</span>`;
  }
  // 唯一的未转义属性插值：id 现在是 int，但不该让 id 的形状决定这里安不安全。
  return `<img class="sent" data-attach="${esc(file.id)}" alt="${esc(file.name)}" title="${esc(file.name)}" />`;
}

function renderComposer() {
  const form = $("#composer");
  form.hidden = !state.agentId;
  const input = $("#input");
  if (form.hidden) return;
  const working = Boolean(state.session && state.session.state === "working");
  // 闸门只写在 blockedNow / takesImagesNow 里，粘贴走同一对判据 ——
  // 两处各写一遍，就会出现「按钮是灰的、粘贴却进得去」。
  const blocked = blockedNow();
  // 「卡片声明了输入类型，且其中没有 image/*」才关掉入口。卡片没声明（null）
  // 时照常允许 —— 没声明不等于不支持，让 agent 自己去拒绝。
  const takesImages = takesImagesNow();
  input.disabled = blocked;
  input.placeholder = working ? t("composer.busy") : t("composer.placeholder");
  const attach = $("#attach");
  attach.disabled = blocked || !takesImages;
  const attachLabel = takesImages ? t("attach.add") : t("attach.unsupported");
  attach.title = attachLabel;
  // aria-label 是可访问名，会盖过 title：只改 title 的话，卡片只收文本时
  // 屏幕阅读器听到的仍然是「添加图片」。
  attach.setAttribute("aria-label", attachLabel);
  attach.classList.toggle("blocked", !takesImages);
  renderAttachments();
  $("#send").disabled = blocked || (!input.value.trim() && !state.pending.length);
}

/* ------------------------------------------------------------------ 图片 */

function renderAttachments() {
  const strip = $("#attach-strip");
  if (!strip) return;
  strip.innerHTML = state.pending
    .map(
      (item, index) => `<figure class="chip" title="${esc(item.name)}">
        <img src="${esc(item.preview)}" alt="${esc(item.name)}" />
        <button type="button" data-detach="${index}" aria-label="${esc(t("attach.remove"))}">×</button>
        <figcaption>${esc(shortName(item.name))}</figcaption>
      </figure>`,
    )
    .join("");
  strip.hidden = state.pending.length === 0;
}

/**
 * 让附件条里的名字不重样。
 *
 * `pastedName` 精确到秒、序号只在同一次粘贴内递增，所以同一秒里按两次
 * Ctrl+V 会拿到同一个名字。服务端按内容 hash 落库，两张图都不会丢；
 * 但附件条上并排两个 `screenshot-…`，人分不清哪张是哪张。
 */
function uniqueName(name) {
  const taken = new Set(state.pending.map((item) => item.name));
  if (!taken.has(name)) return name;
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : "";
  let index = 2;
  while (taken.has(`${stem}-${index}${ext}`)) index += 1;
  return `${stem}-${index}${ext}`;
}

function shortName(name) {
  const text = String(name || "");
  if (text.length <= 18) return text;
  // 头尾都留。粘贴来的图名字只差时间戳（screenshot-20261112-092940.png），
  // 只留头部会让附件条上四张图长得一模一样 —— 命名本来就是为了分得清，
  // 显示层不能又把它截成同一个样子。尾部通常才是真正区分的那一段。
  // 尾部要留住完整的时间（`HHMMSS` 6 位）加扩展名 —— 取 9 位会砍掉小时的十位，
  // `092940` 与 `192940` 于是渲染成同一个 `screen…92940.png`，正好毁掉命名的目的。
  return `${text.slice(0, 6)}…${text.slice(-10)}`;
}

function sizeLabel(bytes) {
  // 上限来自服务端配置，可能是 500 KB：一律说成 "1 MB" 会和它自己的报错互相矛盾。
  if (bytes >= 1024 * 1024) {
    // 整数就写整数，其余保留一位：四舍五入会把 1.6 MB 的上限说成 "2 MB"，
    // 于是拒收 1.8 MB 的报错自己打自己。
    const mb = bytes / (1024 * 1024);
    return `${Number.isInteger(mb) ? mb : mb.toFixed(1)} MB`;
  }
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(t("attach.failed", { name: file.name })));
    reader.onload = () => {
      const result = String(reader.result || "");
      const comma = result.indexOf(",");
      if (comma < 0) return reject(new Error(t("attach.failed", { name: file.name })));
      // data URL 前半段是声明，真正要发给服务端的是逗号之后的 base64。
      resolve({ data: result.slice(comma + 1), preview: result });
    };
    reader.readAsDataURL(file);
  });
}

/* ------------------------------------------- 超限适配（只缩，不换内容） */

/**
 * 只在超限时动手：没超的一个字节都不碰。
 *
 * 三条护栏，少一条就成了「花里胡哨且悄悄改内容」：
 *   - 只缩尺寸、只做同格式重编码（PNG→PNG 保住 alpha，JPEG→JPEG 保住底色）；
 *   - GIF 不动：canvas 只出一帧，压缩动图等于把内容换掉；
 *   - 压不下来就把原图交回去，让服务端的 413 说真话，而不是前端假装成功。
 */
async function fitToLimit(file, maxBytes) {
  if (file.size <= maxBytes) return { file, shrunk: null };
  // 超过上限一个数量级的文件不尝试解码：`createImageBitmap` 的像素内存随边长平方
  // 增长，几十 MB 的 PNG（或一张只有 2 MB、却是 20000×20000 的纯色图）足以把标签页
  // 顶死，而它本来也压不回上限里 —— 直接交回原图，让「太大」这句话由 413 说。
  if (file.size > maxBytes * 8) return { file, shrunk: null };
  // 动图一律不压：GIF、APNG、动态 WebP。后两种的 MIME 就是 image/png 与
  // image/webp —— 只按 GIF 判，它们会被压成一张只剩第一帧的静止图，还被
  // 告知「已压缩」。静默换掉内容比拒收更糟，所以按 chunk 判真身。
  if (await isAnimatedImage(file)) {
    throw new Error(t("attach.gifTooBig", { name: file.name, max: sizeLabel(maxBytes) }));
  }
  const bitmap = await loadBitmap(file);
  if (!bitmap) return { file, shrunk: null }; // 解不开：原样交给服务端
  try {
    const plan = fitPlan(bitmap.width, bitmap.height);
    // 尺寸不超、字节超（高噪点的 PNG）：仍按原尺寸重编码一次；变不小就交回原图。
    if (!plan.length) plan.push({ width: bitmap.width, height: bitmap.height });
    for (const step of plan) {
      const blob = await drawToBlob(bitmap, step, file.type);
      if (!blob || blob.size > maxBytes) continue;
      return {
        file: new File([blob], file.name, { type: file.type }),
        shrunk: { width: step.width, height: step.height, to: blob.size },
      };
    }
    return { file, shrunk: null };
  } finally {
    if (bitmap.close) bitmap.close();
    if (bitmap.__revoke) URL.revokeObjectURL(bitmap.__revoke);
  }
}

function loadBitmap(file) {
  // EXIF 方向：手机截图/照片不声明就会转 90°，压出来是一张躺着的图。
  if (typeof createImageBitmap === "function") {
    return createImageBitmap(file, { imageOrientation: "from-image" }).catch(() => loadViaImg(file));
  }
  return Promise.resolve().then(() => loadViaImg(file));
}

function loadViaImg(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      // 把 URL 挂到对象上，由 fitToLimit 的 finally 统一回收。
      img.__revoke = url;
      resolve(img);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      resolve(null);
    };
    img.src = url;
  });
}

function drawToBlob(bitmap, step, mime) {
  return new Promise((resolve) => {
    try {
      const canvas = document.createElement("canvas");
      canvas.width = step.width;
      canvas.height = step.height;
      const context = canvas.getContext("2d");
      if (!context) return resolve(null);
      context.drawImage(bitmap, 0, 0, step.width, step.height);
      // 只做同格式重编码：PNG 无损、不带质量参数，JPEG/WebP 用接近原图的档位。
      const quality = { "image/jpeg": 0.92, "image/webp": 0.9 }[mime];
      // 编码器不支持目标格式时会静默退回 PNG。那种 blob 的字节和 type 对不上，
      // 服务端嗅出来就会 415 —— 所以宁可放弃这一档。
      canvas.toBlob((blob) => resolve(blob && blob.type === mime ? blob : null), mime, quality);
    } catch {
      resolve(null);
    }
  });
}

/* ------------------------------------------------------------- 粘贴入口 */

/**
 * 粘贴只在「没有更该收这件事的地方」接管。
 *
 * 监听挂在 document 上：paste 只派发给焦点元素、再按树冒泡。用户「刚截完图、
 * 手还在键盘上」时焦点是 body，事件根本不经过 #composer —— 挂在 composer 上
 * 会漏掉最自然的那一次粘贴。
 */
function pasteAllowed(target) {
  const node = target && target.nodeType === 1 ? target : null;
  const tag = node ? String(node.tagName || "").toLowerCase() : "";
  const editing = Boolean(node) && (node.isContentEditable || tag === "input" || tag === "textarea");
  // 别的输入框（搜索、添加智能体、改名）优先：不抢它们的粘贴。
  if (editing && node.id !== "input") return false;
  // 弹窗开着时粘贴该落在弹窗自己的字段里；主输入框此刻多半也被盖住了。
  const root = $("#modal-root");
  return !root || !root.querySelector(".modal-backdrop");
}

/** 与 renderComposer 同一判据：卡片只收文本时，粘贴也不该进得去。 */
function takesImagesNow() {
  const agent = currentAgent();
  return !agent || agent.acceptsImages !== false;
}

/** 与 renderComposer 同一判据：忙碌/工作中，粘贴也不该进得去。 */
function blockedNow() {
  return Boolean(state.busy || (state.session && state.session.state === "working"));
}

async function addFiles(fileList) {
  const limits = state.limits;
  // 三扇门共用这一道闸门：选文件、Ctrl+V、ui_attach。判据原本散在调用方，
  // 而 ui_attach 正是绕过它的那一条 —— 在首页（没选智能体）它照样把图塞进
  // state.pending 并回报「已附加」，可附件条根本没渲染，下次 route() 就把
  // 那张图静默丢了。闸门放在这里，三扇门才真的只有一套判据。
  if (!state.agentId) {
    toast(t("empty.noAgent"), "error");
    return 0;
  }
  if (!takesImagesNow()) {
    toast(t("attach.unsupported"), "error");
    return 0;
  }
  if (blockedNow()) {
    toast(t("composer.busy"), "error");
    return 0;
  }
  // 比数组身份，而不是 agentId：同一 agent 下换会话时 agentId 不变，
  // 但 route() 会整体替换 state.pending —— 那时读出来的图也不能落进去。
  const scope = state.pending;
  let added = 0;
  for (const original of Array.from(fileList || [])) {
    // 字节说了算 —— 三扇门（选文件 / Ctrl+V / ui_attach）在这里归一成同一条规则。
    // 只听声明会让「没有扩展名的截图」（type 为空或 octet-stream）被当成不支持的
    // 类型拒掉，也会放一个改了扩展名的 PDF 过去、直到服务端重新嗅一遍才 415。
    // 只有「字节读不了」才退回声明 —— 那时除了声明没有别的东西可依据。
    const sniffed = (await sniffImageType(original)) ?? String(original.type || "").toLowerCase();
    // 先看类型，再看名额：满了 4 张时混选一个 .txt，该说的是「不支持的类型」。
    if (!IMAGE_TYPES.includes(sniffed)) {
      toast(t("attach.type"), "error");
      continue;
    }
    if (state.pending.length >= limits.maxPerMessage) {
      toast(t("attach.tooMany", { n: limits.maxPerMessage }), "error");
      break;
    }
    // 超限的图先就地适配，适配不下来才报错。放在名额判断之后：名额已经满了，
    // 就不该白压一张图再把它丢掉。
    // 声明与字节不符时以字节为准：压缩要按真实格式重编码（PNG 免得把 alpha 压成
    // 黑底），base64 也要带上正确的 mime，否则服务端照样 415。
    let file = original.type === sniffed ? original : new File([original], original.name, { type: sniffed });
    try {
      const fitted = await fitToLimit(file, limits.maxBytes);
      // 作用域已经换了：前面攒下的都随旧数组一起丢了，不能再算作「加上了」，
      // 否则 ui_attach 会把一次没落地的附件报成成功。
      if (state.pending !== scope) return 0;
      file = fitted.file;
      if (fitted.shrunk) {
        toast(
          t("attach.shrunk", {
            name: original.name,
            max: sizeLabel(limits.maxBytes),
            size: t("attach.shrunkSize", {
              width: fitted.shrunk.width,
              height: fitted.shrunk.height,
              size: sizeLabel(fitted.shrunk.to),
            }),
          }),
          "warn",
        );
      }
    } catch (error) {
      toast(error.message, "error");
      continue;
    }
    // 适配过仍然超限（或者压不下来交回了原图）：如实拒收。
    if (file.size > limits.maxBytes) {
      toast(t("attach.tooBig", { name: file.name, max: sizeLabel(limits.maxBytes) }), "error");
      continue;
    }
    try {
      const { data, preview } = await readAsBase64(file);
      if (state.pending !== scope) return 0;
      // 读文件是异步的：等的这段时间里另一次 addFiles 可能把名额填满了，
      // 所以名额要在这之后再判一次，否则前端会发出服务端必然拒收的那种消息。
      if (state.pending.length >= limits.maxPerMessage) {
        toast(t("attach.tooMany", { n: limits.maxPerMessage }), "error");
        break;
      }
      const name = uniqueName(file.name);
      state.pending.push({ name, mime: file.type, size: file.size, data, preview });
      added += 1;
    } catch (error) {
      toast(error.message, "error");
    }
  }
  renderComposer();
  // 返回真实加进去的张数：助手层的 ui_attach 靠它区分「加上了」与「被拒了」，
  // 否则会把一次拒收谎报成成功。
  return added;
}

// 已取回的图按附件 id 缓存成 Promise<blob URL>：切会话再切回来不必重新下载，
// renderChat 在轮询期间反复重跑时，重叠的两次也不会对同一张图发两次请求。
// 缓存有上限、并且在换口令时整体作废 —— 长开的页面不该把看过的图全攒着，
// 旧口令取回的 blob 也不该在换了口令之后继续显示。
const ATTACHMENT_URLS = new Map(); // id -> { promise, url }
const ATTACHMENT_CACHE_MAX = 48;

function dropAttachmentCache() {
  for (const record of ATTACHMENT_URLS.values()) {
    record.promise.then((url) => URL.revokeObjectURL(url)).catch(() => {});
  }
  ATTACHMENT_URLS.clear();
}

// 图上还引用着这个 URL 时不能吊销：驱逐是为了省内存，不是为了让正在看的图
// 变成「已消失」。全都在用时这一轮就不驱逐。
function attachmentInUse(url) {
  return Array.from(document.querySelectorAll("img[src]")).some((node) => node.src === url);
}

function evictOldestAttachment(skipId) {
  for (const [id, record] of ATTACHMENT_URLS) {
    if (id === skipId) continue;
    // 还没取回来：URL 还不存在，等它 resolve 再吊销的话，正 await 的 <img>
    // 刚拿到就被撤掉，图就碎了。在飞的记录一律不动。
    if (!record.url || attachmentInUse(record.url)) continue;
    ATTACHMENT_URLS.delete(id);
    record.promise.then((url) => URL.revokeObjectURL(url)).catch(() => {});
    return;
  }
}

function attachmentUrl(id) {
  const cached = ATTACHMENT_URLS.get(id);
  if (cached) return cached.promise;
  const record = { promise: null, url: null };
  record.promise = (async () => {
    const token = localStorage.getItem(TOKEN_KEY) || "";
    const headers = token ? { "x-panel-token": token } : {};
    const response = await fetch(`${API}/attachments/${encodeURIComponent(id)}`, { headers });
    if (response.status === 401) {
      // 和 api() 一样：口令缺失/过期要弹出输口令的窗口。否则整屏图会静默
      // 变成「已消失」，而人既看不出是口令问题，也没有入口去修。
      openTokenDialog();
      throw new Error(t("token.required"));
    }
    if (!response.ok) throw new Error("gone");
    const url = URL.createObjectURL(await response.blob());
    record.url = url;
    // 一次只试一个：没有可删的（图都还在屏上）就让缓存暂时超一点上限，
    // 不能在这里 while —— evictOldestAttachment 什么都不删时 size 永远不变。
    if (ATTACHMENT_URLS.size >= ATTACHMENT_CACHE_MAX) evictOldestAttachment(id);
    return url;
  })();
  // 失败不留缓存，下一帧还能重试（比如口令刚填完、网络刚恢复）。
  record.promise.catch(() => {
    // 只删「还是这一条」的记录：同一个 id 可能已经被驱逐、又被重新取过一次，
    // 按 key 直接删会把那条新记录的 blob URL 漏成永久泄漏。
    if (ATTACHMENT_URLS.get(id) === record) ATTACHMENT_URLS.delete(id);
  });
  ATTACHMENT_URLS.set(id, record);
  return record.promise;
}

async function hydrateAttachments(root, done) {
  const nodes = Array.from(root.querySelectorAll("img[data-attach]"));
  if (!nodes.length) {
    if (done) done();
    return;
  }
  await Promise.all(
    nodes.map(async (node) => {
      try {
        node.src = await attachmentUrl(node.dataset.attach);
        // 等解码完再算「是否贴底」：此刻图才有尺寸，早一步读 scrollHeight
        // 读到的还是零高度，图一显示就把视口顶走了。
        await node.decode?.().catch(() => {});
      } catch {
        node.replaceWith(attachmentPlaceholder(node.alt));
      }
    }),
  );
  if (done) done();
}

function attachmentPlaceholder(name) {
  const node = document.createElement("span");
  node.className = "attachment-gone";
  node.textContent = name || "image";
  return node;
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
  // 只有真的换了会话才清空待发图片。go() 对同一段路由也会调用 route()，
  // 点一下「我本来就在的这段会话」不该把选好的图弄丢。
  const target = sessionId && sessionId !== "new" ? sessionId : null;
  const current = state.session ? state.session.id : null;
  if (agentId !== state.agentId || target !== current) state.pending = [];

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
    if (summary.images) state.limits = { ...IMAGE_LIMITS, ...summary.images };
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
  const files = state.pending.map((item) => ({ mime: item.mime, name: item.name, data: item.data }));
  if ((!text && !files.length) || state.busy || !state.agentId) return;
  if (state.session && state.session.state === "working") return;

  state.busy = true;
  renderComposer();
  try {
    const creating = !state.session || state.draft;
    const body = files.length ? { text, attachments: files } : { text };
    const data = creating
      ? await api(`${API}/agents/${encodeURIComponent(state.agentId)}/sessions`, {
          method: "POST",
          body,
        })
      : await api(`${API}/sessions/${encodeURIComponent(state.session.id)}/messages`, {
          method: "POST",
          body,
        });

    input.value = "";
    autosize(input);
    state.pending = [];
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
  // Another dialog is about to take `#modal-root`. If a confirmation was pending
  // there, settle it now as unanswered: leaving it armed would fire the model's
  // canceller later, when an unrelated dialog is closed.
  const pending = confirmCanceller;
  confirmCanceller = null;
  if (pending) pending();
  root.innerHTML = `<div class="modal-backdrop" data-close="1">
    <div class="modal" role="dialog" aria-modal="true">${html}</div>
  </div>`;
  document.body.style.overflow = "hidden";
  const focusable = root.querySelector("input, textarea, button");
  if (focusable) setTimeout(() => focusable.focus(), 0);
}

//: Who to tell when the dialog goes away without a decision (backdrop, Escape,
//: the close button). Without this a gated delete would look cancelled to the
//: user and still be pending to the model — for up to its 120s timeout.
let confirmCanceller = null;

function closeModal() {
  const cancel = confirmCanceller;
  confirmCanceller = null;
  $("#modal-root").innerHTML = "";
  document.body.style.overflow = "";
  if (cancel) cancel();
}

function confirmDialog({ title, sub, ok, onConfirm, onCancel }) {
  openModal(`
    <h2>${esc(title)}</h2>
    <p class="sub">${esc(sub)}</p>
    <footer>
      <button class="btn ghost" data-close="1" type="button">${esc(t("cancel"))}</button>
      <button class="btn primary" id="confirm-ok" type="button">${esc(ok)}</button>
    </footer>`);
  // Armed after mounting: `openModal` settles (and clears) the previous one.
  confirmCanceller = onCancel || null;
  $("#confirm-ok").addEventListener("click", () => {
    confirmCanceller = null;
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
    // 旧口令取回的 blob 不能带进新口令。
    dropAttachmentCache();
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

  // 可选类型只在 IMAGE_TYPES 声明一次，选择器的 accept 由它派生。
  $("#attach-input").accept = IMAGE_TYPES.join(",");
  $("#attach").addEventListener("click", () => $("#attach-input").click());
  $("#attach-input").addEventListener("change", (event) => {
    const picker = event.target;
    addFiles(picker.files).finally(() => {
      // 清空 value：同一个文件再选一次也要能触发 change。
      picker.value = "";
    });
  });

  // Ctrl+V 粘一张截图。挂在 document 上，不是挂在 #composer 上 —— 理由见 pasteAllowed。
  document.addEventListener("paste", (event) => {
    const clipboard = event.clipboardData;
    if (!clipboard) return;
    // 剪贴板只扫一次：纯文字粘贴一根手指都不碰，完整交还浏览器。
    // 抽好的列表往下传，pastedFiles 不再自己扫一遍 —— 两次 getAsFile()
    // 万一给出不同的东西，「有没有文件」与「收了什么」就会各说各话。
    const raw = pasteFiles(clipboard);
    if (!raw.length) return;
    if (event.target && event.target.id === "agent-input") {
      // 内置助手不收图（服务端也不收）；静默忽略等于「贴了，什么也没发生」。
      toast(t("attach.drawerNoImages"), "warn");
      return;
    }
    if (!pasteAllowed(event.target)) return;
    // 不 preventDefault：图片进附件条，文字照常落进 textarea。浏览器的原生粘贴
    // 一行都不用接管，也就没有「接管错了把文字吃掉」这一类 bug。
    pastedFiles(raw)
      .then((files) => {
        // 剪贴板里有文件、却没有一张是我们能收的（BMP/TIFF/PDF…）。
        // 静默丢弃正是这个仓库反复在修的那种失败，必须说一句话。
        if (!files.length) {
          toast(t("attach.type"), "error");
          return;
        }
        // 截图混着一个 PDF 一起贴进来：图收下了，PDF 不能悄悄没掉。
        // 选文件那条路是逐个报的，两条入口该说同样的话。
        if (files.length < raw.length) toast(t("attach.type"), "warn");
        // 准入闸门（有没有智能体、收不收图、忙不忙）统在 addFiles 里，不在这里
        // 再抄一遍 —— 抄两处，迟早一处改了另一处忘了，而两边一旦不一致，
        // 就会出现「按钮是灰的、Ctrl+V 却进得去」这种说不清的事。
        return addFiles(files);
      })
      .catch((error) => {
        // 只进 console 等于「贴了没反应」—— 正是本文件反复在修的那种静默丢弃。
        console.warn("paste failed", error);
        toast(t("error.generic"), "error");
      });
  });

  const input = $("#input");
  input.addEventListener("input", () => {
    autosize(input);
    $("#send").disabled = state.busy || (!input.value.trim() && !state.pending.length);
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
    const detach = event.target.closest("[data-detach]");
    if (detach) {
      state.pending.splice(Number(detach.dataset.detach), 1);
      renderComposer();
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
  try {
    const storedTheme = localStorage.getItem(THEME_KEY);
    if (storedTheme) applyTheme(storedTheme, { persist: false });
  } catch {
    /* no storage: follow the system preference */
  }
  bindDom();
  await loadSummary();
  await loadAgents().catch((error) => toast(error.message, "error"));
  await route();
  startAgent();
}

/* ------------------------------------------------- 内置助手（agent native）
 *
 * The drawer is the only module that may touch the page from the agent side, so
 * it gets one explicit façade instead of reaching into this file's closures.
 * Everything here already existed; nothing is exported from app.js itself, which
 * keeps the page a page rather than a library.
 */

window.oacp = {
  api,
  go,
  state,
  t,
  toast,
  addFiles,
  setLang,
  setTheme: applyTheme,
  renderAll,
  openModal,
  closeModal,
  confirmDialog,
  lang: () => lang,
  panelToken: () => localStorage.getItem(TOKEN_KEY) || "",
  // The assistant removes agents through the same endpoint the page does, so it
  // needs the same per-agent manage token; without it a network-exposed panel
  // answers 401 to the assistant while a manual removal succeeds.
  manageToken: (id) => {
    try {
      const tokens = JSON.parse(localStorage.getItem(MANAGE_KEY) || "{}");
      return tokens[id] || "";
    } catch {
      return "";
    }
  },
};

async function startAgent() {
  let panel = null;
  try {
    // Imported lazily. A static import would make a parse or evaluation error in the
    // assistant's modules abort *this* module before `boot()` ever runs — a blank
    // panel, which is the one failure "never a precondition" promises not to happen.
    const { createAgentPanel } = await import("./agent/panel.js");
    panel = createAgentPanel({ host: window.oacp });
    panel.bind();
  } catch (error) {
    // Absent, not broken: the page must still be the page. But a module that fails to
    // import or start is a bug, not "feature disabled", and silence would hide it.
    console.warn("assistant panel unavailable", error);
    return;
  }
  window.oacp.agent = panel;
  // Deliberately not awaited — connecting must never delay first paint, and a
  // server that is not there must not surface as an error on an unrelated page.
  panel.boot().catch((error) => console.warn("assistant panel failed to start", error));
}

boot();
