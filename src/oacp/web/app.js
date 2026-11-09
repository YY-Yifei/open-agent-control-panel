/* Open Agent Control Panel — vanilla ES module front end.
 * No build step, no framework: hash routing, fetch, and template strings.
 *
 * Every user-facing string goes through the tiny i18n layer below (DICT + t()).
 * Chinese is the default; English is kept in full so the panel can be switched
 * back from the sidebar without losing anything. Machine values (task states,
 * agent statuses, ids, urls, JSON payloads) are never translated — only the
 * labels around them.
 */

const API = "/api/panel";
const TOKEN_KEY = "oacp.panelToken";
const MANAGE_KEY = "oacp.manageTokens";
const LANG_KEY = "oacp.lang";
const LANGS = ["zh", "en"];

const state = {
  q: "",
  summary: null,
  route: { name: "agents", id: null },
  lastAgentsJson: "",
  lastTasksJson: "",
  refreshTimer: null,
};

/* ---------------------------------------------------------------------- i18n */

const DICT = {
  zh: {
    "app.title": "Open Agent Control Panel · 智能体控制面板",

    "brand.name": "智能体控制面板",
    "brand.sub": "ADP Core v1",
    "nav.main": "主导航",
    "nav.agents": "智能体",
    "nav.tasks": "任务",
    "toolbar.searchAgents": "搜索智能体…",
    "toolbar.searchTasks": "搜索任务…",
    "toolbar.register": "注册智能体",
    "lang.toggle": "English",
    "lang.toggleTitle": "切换界面语言",

    "page.agents.title": "智能体",
    "page.agents.sub": "通过 discover 协议注册的智能体",
    "page.agent.title": "智能体",
    "page.agent.sub": "智能体记录、端点与最近任务",
    "page.tasks.title": "任务",
    "page.tasks.sub": "本面板提交过的全部任务",
    "page.task.title": "任务",
    "page.task.sub": "单个 A2A 任务及其事件日志",

    "common.cancel": "取消",
    "common.never": "从未",
    "common.loading": "加载中…",
    "time.justNow": "刚刚",
    "time.minutesAgo": "{n} 分钟前",
    "time.hoursAgo": "{n} 小时前",
    "time.daysAgo": "{n} 天前",

    "banner.local.title": "本地模式",
    "banner.local.body":
      "已绑定回环地址，因此接受 <code>http</code> 与私有卡片地址，A2A 调用也可以指向内网主机。"
      + "该部署不声明 ADP Core 合规 —— 用 <code>--strict</code> 启动可恢复仅限 https 的校验。",

    "stats.agents": "智能体",
    "stats.tasks": "任务",
    "stats.probing": "探测",
    "stats.cardMode": "卡片校验",
    "stats.agents.hint": "{ok} 正常 · {down} 异常",
    "stats.tasks.hint": "{n} 进行中",
    "stats.probe.on": "开",
    "stats.probe.off": "关",
    "stats.probe.every": "每 {n} 秒",
    "stats.private.allowed": "允许内网目标",
    "stats.private.publicOnly": "仅限公网目标",
    "validation.strict": "严格",
    "validation.relaxed": "宽松",

    "agents.loading": "加载智能体…",
    "agents.unnamed": "未命名智能体",
    "agents.noDescription": "未提供描述。",
    "agents.taskCount": "{n} 个任务",
    "agents.active": "{n} 进行中",
    "agents.lastSeen": "最近活跃 {when}",
    "agents.failures": "{n} 次探测失败",
    "agents.send": "发送任务",
    "agents.details": "详情",
    "agents.remove": "移除",
    "agents.skillCount": "{n} 个技能",
    "agents.empty.search.title": "没有匹配的智能体",
    "agents.empty.search.hint": "没有已注册的智能体匹配“{q}”。",
    "agents.empty.title": "还没有智能体",
    "agents.empty.hint":
      "用它的 AgentCard URL 注册一个智能体。卡片必须能通过公网 HTTPS 访问 —— 注册表会自己去抓取它。",
    "agents.empty.action": "注册第一个智能体",

    "agentDetail.loading": "加载智能体…",
    "agentDetail.noSkills": "该卡片没有声明任何技能。",
    "agentDetail.noEndpoint": "该卡片中没有可用的 A2A 端点。",
    "agentDetail.selected": "已选用",
    "agentDetail.metaId": "id",
    "agentDetail.firstSeen": "首次发现",
    "agentDetail.lastOk": "最近成功",
    "agentDetail.send": "发送任务",
    "agentDetail.refresh": "重新抓取卡片",
    "agentDetail.unregister": "移除",
    "agentDetail.stat.tasks": "任务",
    "agentDetail.stat.tasks.hint": "{n} 进行中",
    "agentDetail.stat.probeFailures": "探测失败",
    "agentDetail.stat.probeFailures.hint": "连续 3 次失败即判定为异常",
    "agentDetail.stat.skills": "技能",
    "agentDetail.stat.skills.hint": "{n} 个标签",
    "agentDetail.stat.card": "卡片",
    "agentDetail.stat.card.hint": "变更于 {when}",
    "agentDetail.panel.endpoints": "端点",
    "agentDetail.panel.description": "描述",
    "agentDetail.panel.record": "记录",
    "agentDetail.panel.skills": "技能",
    "agentDetail.panel.recentTasks": "最近任务",
    "agentDetail.panel.raw": "原始 AgentCard",
    "agentDetail.raw.summary": "查看抓取到的原始 JSON",
    "agentDetail.table.task": "任务",
    "agentDetail.table.state": "状态",
    "agentDetail.table.updated": "更新时间",
    "agentDetail.table.open": "打开",
    "agentDetail.noTasks": "还没有向该智能体提交过任务。",
    "agentDetail.record.status": "状态",
    "agentDetail.record.firstSeen": "首次发现",
    "agentDetail.record.checked": "最近检查",
    "agentDetail.record.lastOk": "最近成功",
    "agentDetail.record.changed": "卡片变更",
    "agentDetail.record.prevHash": "上一个哈希",
    "agentDetail.record.version": "版本",

    "tasks.allStates": "全部状态",
    "tasks.table.task": "任务",
    "tasks.table.agent": "智能体",
    "tasks.table.state": "状态",
    "tasks.table.created": "创建时间",
    "tasks.table.updated": "更新时间",
    "tasks.table.open": "打开",
    "tasks.empty.title": "还没有任务",
    "tasks.empty.hint": "打开一个智能体并给它发条消息，就能创建第一个任务。",

    "taskDetail.loading": "加载任务…",
    "taskDetail.agent": "智能体",
    "taskDetail.localId": "本地 id",
    "taskDetail.remoteId": "远端 id",
    "taskDetail.endpoint": "端点",
    "taskDetail.refresh": "从智能体刷新",
    "taskDetail.cancel": "取消",
    "taskDetail.forget": "删除记录",
    "taskDetail.lastError": "最近错误：",
    "taskDetail.panel.prompt": "提示词",
    "taskDetail.panel.output": "智能体最新输出",
    "taskDetail.noOutput": "智能体还没有产出文本。",
    "taskDetail.artifacts": "{n} 个产出物",
    "taskDetail.noArtifacts": "无产出物",
    "taskDetail.updated": "更新于 {when}",
    "taskDetail.synced": "同步于 {when}",
    "taskDetail.panel.follow": "追加消息",
    "taskDetail.follow.placeholder": "向该任务／上下文再发一条用户消息…",
    "taskDetail.follow.hint":
      "发送时会带上 <span class=\"mono\">taskId</span> 与 <span class=\"mono\">contextId</span>，"
      + "因此智能体可以继续同一段对话。",
    "taskDetail.follow.send": "发送消息",
    "taskDetail.panel.events": "事件日志",
    "taskDetail.events.empty": "暂无事件记录。",
    "taskDetail.panel.details": "详情",
    "taskDetail.detail.localId": "本地 id",
    "taskDetail.detail.remoteId": "远端 id",
    "taskDetail.detail.context": "上下文",
    "taskDetail.detail.created": "创建时间",
    "taskDetail.detail.updated": "更新时间",
    "taskDetail.detail.messages": "消息数",
    "taskDetail.detail.endpoint": "端点",
    "taskDetail.panel.raw": "原始报文",
    "taskDetail.raw.request": "请求",
    "taskDetail.raw.response": "最新的 A2A 响应",

    "register.title": "注册智能体",
    "register.sub":
      "注册表会自己去抓取 AgentCard。URL 必须是公网 HTTPS，并返回合法的 A2A 卡片 —— "
      + "注册只证明这个 URL 此刻提供的就是这张卡片。",
    "register.label": "AgentCard URL",
    "register.placeholder": "https://agent.example.com/.well-known/agent-card.json",
    "register.hint": "也可以直接填智能体的基础 URL，只要它在 well-known 路径上提供卡片。",
    "register.submit": "抓取并注册",
    "register.fetching": "抓取中…",
    "register.ok": "已注册。manage token 已保存在此浏览器中。",
    "register.exists": "该智能体此前已注册过。",

    "send.title": "发送任务",
    "send.sub":
      "这会在 <strong>{name}</strong> 上调用 A2A "
      + "<span class=\"mono\">message/send</span>，并记录产生的任务。",
    "send.label": "消息",
    "send.placeholder": "描述要交给这个智能体的任务…",
    "send.contextLabel": "上下文 id（可选）",
    "send.contextPlaceholder": "继续一段已有的对话",
    "send.submit": "发送",
    "send.sending": "发送中…",
    "send.failed": "智能体调用失败：{error}",
    "send.ok": "任务已提交",

    "deleteAgent.title": "移除这个智能体？",
    "deleteAgent.sub":
      "移除立即生效，且不留墓碑记录。同一 URL 任何人都可以重新注册，下一个注册者会拿到新的 manage token。"
      + "该智能体在本面板的任务记录会保留。",
    "deleteAgent.label": "manage token",
    "deleteAgent.placeholder": "首次注册时返回的 token",
    "deleteAgent.hint.stored": "已在此浏览器的本地存储中找到。",
    "deleteAgent.hint.missing": "只有首个注册者持有它。若 token 在别人手里，就需要操作员权限才能移除。",
    "deleteAgent.hint.blocked":
      "本面板既没有这个智能体的 manage token，也没有操作员权限（未配置面板口令，且不是回环绑定的本地模式），"
      + "因此无法移除它。用 OACP_PANEL_TOKEN 启动面板，或绑定 127.0.0.1 后再试。",
    "deleteAgent.operator.note": "将以操作员身份移除：本面板有权直接移除该条目，不需要 manage token。",
    "deleteAgent.operator.reason.panel-token": "面板口令已通过验证。",
    "deleteAgent.operator.reason.local-mode": "本地模式：面板绑定回环地址，只有本机可达。",
    "deleteAgent.force": "以操作员身份移除",
    "deleteAgent.submit": "注销",
    "deleteAgent.ok": "{name} 已移除",
    "deleteAgent.okFallback": "智能体已移除",

    "token.title": "面板令牌",
    "token.sub": "本面板配置了访问令牌，它只保存在此浏览器中。",
    "token.label": "令牌",
    "token.save": "保存",
    "token.ok": "令牌已保存",
    "token.required": "该操作需要面板令牌。",

    "action.refreshed": "卡片已重新抓取",
    "action.state": "状态：{state}",
    "action.cancelRequested": "已请求取消（状态：{state}）",
    "action.taskRemoved": "任务已从记录中移除",
    "action.messageSent": "消息已发送",

    "state.ok": "正常",
    "state.down": "异常",
    "state.unknown": "未知",
    "state.active": "进行中",
    "state.submitted": "已提交",
    "state.working": "执行中",
    "state.input-required": "待补充输入",
    "state.auth-required": "待授权",
    "state.completed": "已完成",
    "state.failed": "失败",
    "state.canceled": "已取消",
    "state.replied": "已回复",
    "state.error": "错误",
  },

  en: {
    "app.title": "Open Agent Control Panel",

    "brand.name": "Agent Control Panel",
    "brand.sub": "ADP Core v1",
    "nav.main": "Main",
    "nav.agents": "Agents",
    "nav.tasks": "Tasks",
    "toolbar.searchAgents": "Search agents…",
    "toolbar.searchTasks": "Search tasks…",
    "toolbar.register": "Register agent",
    "lang.toggle": "中文",
    "lang.toggleTitle": "Switch interface language",

    "page.agents.title": "Agents",
    "page.agents.sub": "Agents registered through the discover protocol",
    "page.agent.title": "Agent",
    "page.agent.sub": "Agent record, endpoints and recent tasks",
    "page.tasks.title": "Tasks",
    "page.tasks.sub": "Every task this panel submitted",
    "page.task.title": "Task",
    "page.task.sub": "A single A2A task and its event log",

    "common.cancel": "Cancel",
    "common.never": "never",
    "common.loading": "Loading…",
    "time.justNow": "just now",
    "time.minutesAgo": "{n}m ago",
    "time.hoursAgo": "{n}h ago",
    "time.daysAgo": "{n}d ago",

    "banner.local.title": "Local mode",
    "banner.local.body":
      "Bound to a loopback address, so <code>http</code> and private card URLs are accepted and "
      + "A2A calls may target private hosts. This deployment does not claim ADP Core compliance — "
      + "start with <code>--strict</code> to restore the https-only checks.",

    "stats.agents": "Agents",
    "stats.tasks": "Tasks",
    "stats.probing": "Probing",
    "stats.cardMode": "Card mode",
    "stats.agents.hint": "{ok} ok · {down} down",
    "stats.tasks.hint": "{n} in flight",
    "stats.probe.on": "on",
    "stats.probe.off": "off",
    "stats.probe.every": "every {n}s",
    "stats.private.allowed": "private targets allowed",
    "stats.private.publicOnly": "public targets only",
    "validation.strict": "strict",
    "validation.relaxed": "relaxed",

    "agents.loading": "Loading agents…",
    "agents.unnamed": "Unnamed agent",
    "agents.noDescription": "No description provided.",
    "agents.taskCount": "{n} tasks",
    "agents.active": "{n} active",
    "agents.lastSeen": "last seen {when}",
    "agents.failures": "{n} failed probes",
    "agents.send": "Send task",
    "agents.details": "Details",
    "agents.remove": "Remove",
    "agents.skillCount": "{n} skills",
    "agents.empty.search.title": "No agent matches that search",
    "agents.empty.search.hint": "Nothing registered matches “{q}”.",
    "agents.empty.title": "No agents yet",
    "agents.empty.hint":
      "Register an agent by its AgentCard URL. The card must be reachable over public HTTPS — "
      + "the registry fetches it itself.",
    "agents.empty.action": "Register the first agent",

    "agentDetail.loading": "Loading agent…",
    "agentDetail.noSkills": "The card declares no skills.",
    "agentDetail.noEndpoint": "No usable A2A endpoint in this card.",
    "agentDetail.selected": "selected",
    "agentDetail.metaId": "id",
    "agentDetail.firstSeen": "first seen",
    "agentDetail.lastOk": "last ok",
    "agentDetail.send": "Send task",
    "agentDetail.refresh": "Re-fetch card",
    "agentDetail.unregister": "Remove",
    "agentDetail.stat.tasks": "Tasks",
    "agentDetail.stat.tasks.hint": "{n} active",
    "agentDetail.stat.probeFailures": "Probe failures",
    "agentDetail.stat.probeFailures.hint": "down at 3 in a row",
    "agentDetail.stat.skills": "Skills",
    "agentDetail.stat.skills.hint": "{n} tags",
    "agentDetail.stat.card": "Card",
    "agentDetail.stat.card.hint": "changed {when}",
    "agentDetail.panel.endpoints": "Endpoints",
    "agentDetail.panel.description": "Description",
    "agentDetail.panel.record": "Record",
    "agentDetail.panel.skills": "Skills",
    "agentDetail.panel.recentTasks": "Recent tasks",
    "agentDetail.panel.raw": "Raw AgentCard",
    "agentDetail.raw.summary": "Show the JSON exactly as fetched",
    "agentDetail.table.task": "Task",
    "agentDetail.table.state": "State",
    "agentDetail.table.updated": "Updated",
    "agentDetail.table.open": "Open",
    "agentDetail.noTasks": "No tasks have been submitted to this agent yet.",
    "agentDetail.record.status": "Status",
    "agentDetail.record.firstSeen": "First seen",
    "agentDetail.record.checked": "Checked",
    "agentDetail.record.lastOk": "Last ok",
    "agentDetail.record.changed": "Changed",
    "agentDetail.record.prevHash": "Previous hash",
    "agentDetail.record.version": "Version",

    "tasks.allStates": "All states",
    "tasks.table.task": "Task",
    "tasks.table.agent": "Agent",
    "tasks.table.state": "State",
    "tasks.table.created": "Created",
    "tasks.table.updated": "Updated",
    "tasks.table.open": "Open",
    "tasks.empty.title": "No tasks yet",
    "tasks.empty.hint": "Open an agent and send it a message to create the first one.",

    "taskDetail.loading": "Loading task…",
    "taskDetail.agent": "agent",
    "taskDetail.localId": "local id",
    "taskDetail.remoteId": "remote id",
    "taskDetail.endpoint": "endpoint",
    "taskDetail.refresh": "Refresh from agent",
    "taskDetail.cancel": "Cancel",
    "taskDetail.forget": "Forget",
    "taskDetail.lastError": "Last error:",
    "taskDetail.panel.prompt": "Prompt",
    "taskDetail.panel.output": "Latest agent output",
    "taskDetail.noOutput": "The agent has not produced text yet.",
    "taskDetail.artifacts": "{n} artifact(s)",
    "taskDetail.noArtifacts": "no artifacts",
    "taskDetail.updated": "updated {when}",
    "taskDetail.synced": "synced {when}",
    "taskDetail.panel.follow": "Follow-up message",
    "taskDetail.follow.placeholder": "Send another user message into this task/context…",
    "taskDetail.follow.hint":
      "Sent with <span class=\"mono\">taskId</span> and <span class=\"mono\">contextId</span>, "
      + "so the agent can continue the same conversation.",
    "taskDetail.follow.send": "Send message",
    "taskDetail.panel.events": "Event log",
    "taskDetail.events.empty": "No events recorded.",
    "taskDetail.panel.details": "Details",
    "taskDetail.detail.localId": "Local id",
    "taskDetail.detail.remoteId": "Remote id",
    "taskDetail.detail.context": "Context",
    "taskDetail.detail.created": "Created",
    "taskDetail.detail.updated": "Updated",
    "taskDetail.detail.messages": "Messages",
    "taskDetail.detail.endpoint": "Endpoint",
    "taskDetail.panel.raw": "Raw payloads",
    "taskDetail.raw.request": "Request",
    "taskDetail.raw.response": "Latest A2A response",

    "register.title": "Register an agent",
    "register.sub":
      "The registry fetches the AgentCard itself. The URL must be public HTTPS and end in a valid "
      + "A2A card — registration proves only that this URL serves this card right now.",
    "register.label": "AgentCard URL",
    "register.placeholder": "https://agent.example.com/.well-known/agent-card.json",
    "register.hint":
      "Also accepted: the agent's base URL, if it serves the card at the well-known path.",
    "register.submit": "Fetch and register",
    "register.fetching": "Fetching…",
    "register.ok": "Registered. The manage token was saved in this browser.",
    "register.exists": "This agent was already registered.",

    "send.title": "Send a task",
    "send.sub":
      "This calls A2A <span class=\"mono\">message/send</span> on <strong>{name}</strong> "
      + "and records the resulting task.",
    "send.label": "Message",
    "send.placeholder": "Describe the task for the agent…",
    "send.contextLabel": "Context id (optional)",
    "send.contextPlaceholder": "continue an existing conversation",
    "send.submit": "Send",
    "send.sending": "Sending…",
    "send.failed": "The agent call failed: {error}",
    "send.ok": "Task submitted",

    "deleteAgent.title": "Remove this agent?",
    "deleteAgent.sub":
      "Removal is immediate and leaves no tombstone. The same URL can be registered again by "
      + "anyone, and the next registrant gets a new manage token. The tasks this agent already "
      + "ran stay in the ledger.",
    "deleteAgent.label": "Manage token",
    "deleteAgent.placeholder": "the token returned at first registration",
    "deleteAgent.hint.stored": "Found in this browser's local storage.",
    "deleteAgent.hint.missing":
      "Only the first registrant has it. If somebody else holds it, removing the entry needs "
      + "operator authority.",
    "deleteAgent.hint.blocked":
      "This panel has neither the manage token for the agent nor operator authority (no panel "
      + "token is configured and this is not a loopback deployment), so it cannot remove it. "
      + "Start the panel with OACP_PANEL_TOKEN, or bind 127.0.0.1, and try again.",
    "deleteAgent.operator.note":
      "Removing as the operator: this panel may delete the entry without a manage token.",
    "deleteAgent.operator.reason.panel-token": "The panel token was verified.",
    "deleteAgent.operator.reason.local-mode":
      "Local mode: the panel is bound to loopback, reachable from this machine only.",
    "deleteAgent.force": "Remove as operator",
    "deleteAgent.submit": "Unregister",
    "deleteAgent.ok": "{name} removed",
    "deleteAgent.okFallback": "Agent removed",

    "token.title": "Panel token",
    "token.sub":
      "This panel is configured with an access token. It is stored only in this browser.",
    "token.label": "Token",
    "token.save": "Save",
    "token.ok": "Token saved",
    "token.required": "A panel token is required for this action.",

    "action.refreshed": "Card re-fetched",
    "action.state": "State: {state}",
    "action.cancelRequested": "Cancellation requested (state: {state})",
    "action.taskRemoved": "Task removed from the ledger",
    "action.messageSent": "Message sent",

    "state.ok": "ok",
    "state.down": "down",
    "state.unknown": "unknown",
    "state.active": "active",
    "state.submitted": "submitted",
    "state.working": "working",
    "state.input-required": "input required",
    "state.auth-required": "auth required",
    "state.completed": "completed",
    "state.failed": "failed",
    "state.canceled": "canceled",
    "state.replied": "replied",
    "state.error": "error",
  },
};

/* --------------------------------------------------------- protocol errors */

/* The ADP surface (`/v1`) is a contract: its `code`/`message` pair belongs to the
   spec and stays English on the wire. The panel translates the code for display,
   so a failed registration reads the same language as the rest of the page. */
const PROTOCOL_ERRORS = {
  BAD_URL: { zh: "地址不合法", en: "The URL is not acceptable" },
  BAD_REQUEST: { zh: "请求参数不合法", en: "The request is not acceptable" },
  UNAUTHORIZED: { zh: "未授权：口令不匹配", en: "Not authorized: the token does not match" },
  NOT_FOUND: { zh: "找不到该对象", en: "Not found" },
  CARD_UNREACHABLE: { zh: "拿不到智能体卡片：地址不可达", en: "The agent card could not be fetched" },
  CARD_TIMEOUT: { zh: "抓取智能体卡片超时", en: "Fetching the agent card timed out" },
  TLS_INVALID: { zh: "TLS 证书无效", en: "The TLS certificate is not valid" },
  NOT_AN_AGENT_CARD: { zh: "该地址返回的不是合法的 AgentCard", en: "The document is not a valid AgentCard" },
  RATE_LIMITED: { zh: "操作太频繁，请稍后再试", en: "Too many requests, try again later" },
};

/* Card-validation reasons, as produced by `validate_card()`: `path: reason`.
   Same rule as above — the contract keeps English, the panel shows Chinese.
   Unknown reasons fall through verbatim, so a new check never renders blank. */
const PROBLEM_REASONS = {
  "must be a JSON object": "必须是 JSON 对象",
  "must be a non-empty string": "必须是非空字符串",
  "must be a string": "必须是字符串",
  "must be an object": "必须是对象",
  "must be a list": "必须是数组",
  "must be a non-empty list": "必须是非空数组",
  "must be a list of strings": "必须是字符串数组",
  "is required": "是必填项",
  "an A2A endpoint is required (supportedInterfaces[].url or url)": "缺少 A2A 端点（supportedInterfaces[].url 或 url）",
};

function localized(entry) {
  if (!entry) return "";
  return entry[lang] ?? entry.zh ?? "";
}

function protocolLabel(code) {
  return localized(PROTOCOL_ERRORS[String(code || "")]);
}

/* "name: must be a non-empty string" -> "name：必须是非空字符串" (zh only) */
function problemText(problem) {
  const raw = String(problem ?? "");
  if (lang !== "zh") return raw;
  const at = raw.indexOf(": ");
  if (at < 0) return raw;
  const field = raw.slice(0, at);
  const reason = raw.slice(at + 2);
  const duplicate = /^duplicate skill id (.+)$/.exec(reason);
  if (duplicate) return `${field}：技能 id 重复（${duplicate[1]}）`;
  const mapped = PROBLEM_REASONS[reason];
  return mapped ? `${field}：${mapped}` : raw;
}

function detectLang() {
  try {
    const stored = localStorage.getItem(LANG_KEY);
    if (LANGS.includes(stored)) return stored;
  } catch {
    /* private mode without storage: fall through to the default */
  }
  // Chinese is the default for this panel regardless of navigator.language.
  return "zh";
}

let lang = detectLang();

function t(key, vars) {
  const table = DICT[lang] || DICT.zh;
  const raw = table[key] ?? DICT.zh[key] ?? key;
  if (!vars) return raw;
  return String(raw).replace(/\{(\w+)\}/g, (match, name) => (
    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match
  ));
}

/* Machine values look the same in both languages; only their label is translated. */
function stateLabel(value) {
  const key = String(value || "unknown");
  const table = DICT[lang] || DICT.zh;
  const label = table[`state.${key}`];
  if (label !== undefined) return label;
  return lang === "zh" ? key : key.replace(/-/g, " ");
}

function validationLabel(value) {
  const key = String(value || "relaxed");
  const table = DICT[lang] || DICT.zh;
  return table[`validation.${key}`] ?? key;
}

function applyLangChrome() {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  document.title = t("app.title");
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((node) => {
    node.placeholder = t(node.dataset.i18nPh);
  });
  document.querySelectorAll("[data-i18n-aria]").forEach((node) => {
    node.setAttribute("aria-label", t(node.dataset.i18nAria));
  });
  const toggle = $("#lang-toggle");
  if (toggle) {
    toggle.textContent = t("lang.toggle");
    toggle.title = t("lang.toggleTitle");
  }
}

function setLang(next) {
  if (!LANGS.includes(next) || next === lang) return;
  lang = next;
  try {
    localStorage.setItem(LANG_KEY, lang);
  } catch {
    /* storage may be unavailable; the switch still applies for this session */
  }
  closeModal();
  applyLangChrome();
  route();
}

function toggleLang() {
  setLang(lang === "zh" ? "en" : "zh");
}

/* ------------------------------------------------------------------ helpers */

const $ = (selector, root = document) => root.querySelector(selector);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

function fmtTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString(lang === "zh" ? "zh-CN" : undefined, {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function relTime(value) {
  if (!value) return t("common.never");
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 45) return t("time.justNow");
  if (seconds < 3600) return t("time.minutesAgo", { n: Math.round(seconds / 60) });
  if (seconds < 86400) return t("time.hoursAgo", { n: Math.round(seconds / 3600) });
  return t("time.daysAgo", { n: Math.round(seconds / 86400) });
}

function badge(value) {
  const key = String(value || "unknown");
  return `<span class="badge ${esc(key)}">${esc(stateLabel(key))}</span>`;
}

function shortId(value) {
  const text = String(value || "");
  return text.length > 18 ? `${text.slice(0, 10)}…${text.slice(-4)}` : text;
}

function toast(message, kind = "ok") {
  const root = $("#toast-root");
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  root.append(node);
  setTimeout(() => node.remove(), 4200);
}

class ApiError extends Error {
  constructor(status, message, payload) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

function detailText(payload) {
  // Panel endpoints answer in the language the UI asked for (Accept-Language),
  // so a string `detail` is already final.
  const detail = payload?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => item?.msg || JSON.stringify(item)).join("; ");
  }
  // A protocol error: prefer the localized label for its stable code.
  const label = protocolLabel(payload?.code);
  if (label) return label;
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return payload?.message || "";
}

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

function manageTokens() {
  try {
    return JSON.parse(localStorage.getItem(MANAGE_KEY) || "{}");
  } catch {
    return {};
  }
}

function rememberManageToken(agentId, token) {
  const map = manageTokens();
  map[agentId] = token;
  localStorage.setItem(MANAGE_KEY, JSON.stringify(map));
}

function forgetManageToken(agentId) {
  const map = manageTokens();
  delete map[agentId];
  localStorage.setItem(MANAGE_KEY, JSON.stringify(map));
}

/* ---------------------------------------------------------------- API layer */

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const body = options.body;
  const init = { ...options, headers };
  if (body !== undefined && typeof body !== "string") {
    headers["content-type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const token = getToken();
  // Lets the panel's own endpoints answer in the language the UI is showing.
  headers["accept-language"] = lang === "zh" ? "zh-CN,zh;q=0.9" : "en";

  const response = await fetch(path, init);
  if (response.status === 401 && !options.noAuthPrompt) {
    openTokenDialog();
    throw new ApiError(401, t("token.required"));
  }
  if (!response.ok) {
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    throw new ApiError(
      response.status,
      detailText(payload) || `${response.status} ${response.statusText}`,
      payload,
    );
  }
  if (response.status === 204) return null;
  return response.json();
}

/* ------------------------------------------------------------------ routing */

function parseRoute() {
  const hash = location.hash.replace(/^#\/?/, "") || "agents";
  const [name, id] = hash.split("/");
  return { name: name || "agents", id: id ? decodeURIComponent(id) : null };
}

function setChrome(title, subtitle, options = {}) {
  $("#page-title").textContent = title;
  $("#page-subtitle").textContent = subtitle;
  $("#search").placeholder = options.searchPlaceholder || t("toolbar.searchAgents");
  $("#search").classList.toggle("hidden", options.search === false);
  $("#register-btn").classList.toggle("hidden", options.register === false);
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.nav === options.nav);
  });
}

function navigate(hash) {
  if (location.hash === hash) {
    route();
  } else {
    location.hash = hash;
  }
}

async function route() {
  clearInterval(state.refreshTimer);
  state.route = parseRoute();
  const { name, id } = state.route;
  const view = $("#view");

  if (name === "tasks" && id) {
    setChrome(t("page.task.title"), t("page.task.sub"), { search: false, register: false, nav: "tasks" });
    await renderTaskDetail(id, view);
    return;
  }
  if (name === "tasks") {
    setChrome(t("page.tasks.title"), t("page.tasks.sub"), { nav: "tasks", searchPlaceholder: t("toolbar.searchTasks") });
    await renderTasks(view);
    return;
  }
  if (name === "agents" && id) {
    setChrome(t("page.agent.title"), t("page.agent.sub"), { search: false, register: false, nav: "agents" });
    await renderAgentDetail(id, view);
    return;
  }
  setChrome(t("page.agents.title"), t("page.agents.sub"), { nav: "agents" });
  await renderAgents(view);
}

/* ------------------------------------------------------------------- layout */

function errorPanel(error) {
  return `<div class="alert">${esc(error.message || String(error))}</div>`;
}

function skeleton(message = t("common.loading")) {
  return `<div class="empty"><strong>${esc(message)}</strong></div>`;
}

function emptyState(title, hint, action = "") {
  return `<div class="empty"><strong>${esc(title)}</strong><span>${esc(hint)}</span>${action}</div>`;
}

function applySummary(summary) {
  state.summary = summary;
  const total = summary?.agents?.total ?? 0;
  const tasks = summary?.tasks?.total ?? 0;
  $("#nav-agent-count").textContent = total;
  $("#nav-task-count").textContent = tasks;
  $("#endpoint-info").textContent = summary?.apiBase || "—";
  $("#endpoint-info").title = summary?.publicUrl || "";
  $("#app-version").textContent = summary ? `v${summary.version}` : "—";

  // The relaxed checks are a deployment fact, so they belong on screen and not
  // only in the log: whoever opens the panel should know https is not required.
  const banner = $("#mode-banner");
  if (banner) {
    banner.hidden = !summary?.localMode;
    if (summary?.localMode) {
      banner.innerHTML = `<strong>${esc(t("banner.local.title"))}</strong><span>${t("banner.local.body")}</span>`;
    }
  }
}

function summaryCards(summary) {
  const agents = summary.agents || {};
  const tasks = summary.tasks || {};
  const active = tasks.active ?? 0;
  return `<div class="stats-grid">
    <div class="stat"><div class="label">${esc(t("stats.agents"))}</div><div class="value">${agents.total ?? 0}</div>
      <div class="hint">${esc(t("stats.agents.hint", { ok: agents.ok ?? 0, down: agents.down ?? 0 }))}</div></div>
    <div class="stat"><div class="label">${esc(t("stats.tasks"))}</div><div class="value">${tasks.total ?? 0}</div>
      <div class="hint">${esc(t("stats.tasks.hint", { n: active }))}</div></div>
    <div class="stat"><div class="label">${esc(t("stats.probing"))}</div><div class="value">${esc(summary.probe?.enabled ? t("stats.probe.on") : t("stats.probe.off"))}</div>
      <div class="hint">${esc(t("stats.probe.every", { n: summary.probe?.interval ?? "—" }))}</div></div>
    <div class="stat"><div class="label">${esc(t("stats.cardMode"))}</div><div class="value">${esc(validationLabel(summary.cardValidation))}</div>
      <div class="hint">${esc(summary.allowPrivateAgents ? t("stats.private.allowed") : t("stats.private.publicOnly"))}</div></div>
  </div>`;
}

/* ------------------------------------------------------------------- agents */

function agentCard(agent) {
  const tags = (agent.tags || []).slice(0, 8)
    .map((tag) => `<span class="chip plain">${esc(tag)}</span>`).join("");
  return `<article class="agent-card" data-agent="${esc(agent.id)}">
    <header>
      <div>
        <h3><a href="#/agents/${esc(agent.id)}">${esc(agent.name || t("agents.unnamed"))}</a></h3>
        <div class="url">${esc(agent.url)}</div>
      </div>
      ${badge(agent.status)}
    </header>
    <p class="desc">${esc(agent.description || t("agents.noDescription"))}</p>
    ${tags ? `<div class="chips">${tags}</div>` : ""}
    <div class="agent-meta">
      <span>${esc(t("agents.taskCount", { n: agent.taskCount ?? 0 }))}</span>
      ${agent.activeTaskCount ? `<span>${esc(t("agents.active", { n: agent.activeTaskCount }))}</span>` : ""}
      <span>${esc(t("agents.lastSeen", { when: relTime(agent.lastOkAt) }))}</span>
      ${agent.failures ? `<span style="color:var(--warn)">${esc(t("agents.failures", { n: agent.failures }))}</span>` : ""}
    </div>
    <footer>
      <button class="btn sm" data-act="send" data-id="${esc(agent.id)}">${esc(t("agents.send"))}</button>
      <a class="btn sm ghost" href="#/agents/${esc(agent.id)}">${esc(t("agents.details"))}</a>
      <button class="btn sm danger" data-act="delete-agent" data-id="${esc(agent.id)}"
              data-name="${esc(agent.name || "")}">${esc(t("agents.remove"))}</button>
      <span class="spacer"></span>
      <span class="muted small">${esc(t("agents.skillCount", { n: (agent.skills || []).length }))}</span>
    </footer>
  </article>`;
}

async function renderAgents(view) {
  view.innerHTML = state.summary
    ? summaryCards(state.summary) + skeleton(t("agents.loading"))
    : skeleton(t("agents.loading"));
  try {
    const query = state.q ? `&q=${encodeURIComponent(state.q)}` : "";
    const [summary, data] = await Promise.all([
      api(`${API}/summary`),
      api(`${API}/agents?limit=100${query}`),
    ]);
    applySummary(summary);
    state.lastAgentsJson = JSON.stringify(data.items);

    let list;
    if (!data.items.length) {
      list = state.q
        ? emptyState(t("agents.empty.search.title"), t("agents.empty.search.hint", { q: state.q }))
        : emptyState(
          t("agents.empty.title"),
          t("agents.empty.hint"),
          `<button class="btn primary" data-act="register">${esc(t("agents.empty.action"))}</button>`,
        );
    } else {
      list = `<div class="agent-grid">${data.items.map(agentCard).join("")}</div>`;
    }
    view.innerHTML = summaryCards(summary) + list;
  } catch (error) {
    view.innerHTML = errorPanel(error);
  }
  state.refreshTimer = setInterval(() => quietAgentsRefresh(), 6000);
}

async function quietAgentsRefresh() {
  if (document.visibilityState !== "visible" || $("#modal-root").children.length) return;
  if (state.route.name !== "agents" || state.route.id) return;
  try {
    const query = state.q ? `&q=${encodeURIComponent(state.q)}` : "";
    const data = await api(`${API}/agents?limit=100${query}`);
    const serialized = JSON.stringify(data.items);
    if (serialized === state.lastAgentsJson) return;
    state.lastAgentsJson = serialized;
    const grid = $(".agent-grid");
    if (grid) {
      grid.innerHTML = data.items.map(agentCard).join("");
    } else {
      await renderAgents($("#view"));
    }
    api(`${API}/summary`).then(applySummary).catch(() => {});
  } catch {
    /* a quiet refresh must never disturb the view */
  }
}

/* ------------------------------------------------------------- agent detail */

function kvRow(label, value) {
  return `<dt>${esc(label)}</dt><dd>${value}</dd>`;
}

async function renderAgentDetail(id, view) {
  view.innerHTML = skeleton(t("agentDetail.loading"));
  let data;
  try {
    data = await api(`${API}/agents/${encodeURIComponent(id)}`);
  } catch (error) {
    view.innerHTML = errorPanel(error);
    return;
  }

  const { agent, interfaces = [], endpoint, stats = {}, tasks = [] } = data;
  const card = agent.card || {};
  const skills = Array.isArray(card.skills) ? card.skills : [];
  const tags = (skills.flatMap((skill) => (Array.isArray(skill.tags) ? skill.tags : [])))
    .filter((tag, index, all) => all.indexOf(tag) === index);

  const skillsHtml = skills.length
    ? `<div class="stack">${skills.map((skill) => `
        <div>
          <div class="row">
            <strong>${esc(skill.name || skill.id)}</strong>
            <span class="chip teal mono">${esc(skill.id || "")}</span>
          </div>
          <div class="muted small">${esc(skill.description || "")}</div>
          ${(skill.tags || []).length ? `<div class="chips" style="margin-top:6px">${skill.tags.map((tag) => `<span class="chip plain">${esc(tag)}</span>`).join("")}</div>` : ""}
        </div>`).join("")}</div>`
    : `<p class="muted small">${esc(t("agentDetail.noSkills"))}</p>`;

  const interfaceRows = interfaces.length
    ? interfaces.map((item) => `
        <div class="row" style="justify-content:space-between;align-items:flex-start">
          <div>
            <div class="mono">${esc(item.url)}</div>
            <div class="muted small">${esc(item.protocolBinding || "JSONRPC")}${item.protocolVersion ? ` · ${esc(item.protocolVersion)}` : ""}</div>
          </div>
          ${endpoint && endpoint.url === item.url ? `<span class="chip teal">${esc(t("agentDetail.selected"))}</span>` : ""}
        </div>`).join("")
    : `<p class="muted small">${esc(t("agentDetail.noEndpoint"))}</p>`;

  const taskRows = tasks.length
    ? `<div class="table-wrap"><table><thead><tr>
         <th>${esc(t("agentDetail.table.task"))}</th><th>${esc(t("agentDetail.table.state"))}</th>
         <th>${esc(t("agentDetail.table.updated"))}</th><th></th></tr></thead><tbody>
         ${tasks.map((task) => `<tr class="clickable" data-task="${esc(task.id)}">
           <td><div class="cell-title">${esc(task.title)}</div>
             <div class="cell-sub mono">${esc(shortId(task.remoteTaskId || task.id))}</div></td>
           <td>${badge(task.state)}</td>
           <td class="cell-sub">${esc(fmtTime(task.updatedAt))}</td>
           <td><div class="cell-actions"><a class="btn sm ghost" href="#/tasks/${esc(task.id)}">${esc(t("agentDetail.table.open"))}</a></div></td>
         </tr>`).join("")}
         </tbody></table></div>`
    : `<p class="muted small">${esc(t("agentDetail.noTasks"))}</p>`;

  view.innerHTML = `
    <div class="detail-head">
      <div>
        <h2>${esc(card.name || agent.url)} ${badge(agent.status)}</h2>
        <div class="url">${esc(agent.url)}</div>
        <div class="muted small" style="margin-top:6px">
          ${esc(t("agentDetail.metaId"))} <span class="mono">${esc(agent.id)}</span>
          · ${esc(t("agentDetail.firstSeen"))} ${esc(fmtTime(agent.firstSeenAt))}
          · ${esc(t("agentDetail.lastOk"))} ${esc(fmtTime(agent.lastOkAt))}
        </div>
      </div>
      <div class="detail-actions">
        <button class="btn primary" data-act="send" data-id="${esc(agent.id)}">${esc(t("agentDetail.send"))}</button>
        <button class="btn" data-act="refresh-agent" data-id="${esc(agent.id)}">${esc(t("agentDetail.refresh"))}</button>
        <button class="btn danger" data-act="delete-agent" data-id="${esc(agent.id)}"
              data-name="${esc(agent.name || "")}">${esc(t("agentDetail.unregister"))}</button>
      </div>
    </div>

    <div class="stats-grid">
      <div class="stat"><div class="label">${esc(t("agentDetail.stat.tasks"))}</div><div class="value">${stats.taskCount ?? 0}</div>
        <div class="hint">${esc(t("agentDetail.stat.tasks.hint", { n: stats.activeTaskCount ?? 0 }))}</div></div>
      <div class="stat"><div class="label">${esc(t("agentDetail.stat.probeFailures"))}</div><div class="value">${agent.failures ?? 0}</div>
        <div class="hint">${esc(t("agentDetail.stat.probeFailures.hint"))}</div></div>
      <div class="stat"><div class="label">${esc(t("agentDetail.stat.skills"))}</div><div class="value">${skills.length}</div>
        <div class="hint">${esc(t("agentDetail.stat.skills.hint", { n: tags.length }))}</div></div>
      <div class="stat"><div class="label">${esc(t("agentDetail.stat.card"))}</div><div class="value" style="font-size:14px;word-break:break-all">${esc(shortId(agent.cardHash))}</div>
        <div class="hint">${esc(t("agentDetail.stat.card.hint", { when: agent.changedAt ? relTime(agent.changedAt) : t("common.never") }))}</div></div>
    </div>

    <div class="split">
      <div class="panel">
        <h3>${esc(t("agentDetail.panel.endpoints"))}</h3>
        <div class="stack">${interfaceRows}</div>
        <h3 style="margin-top:18px">${esc(t("agentDetail.panel.description"))}</h3>
        <p class="muted" style="margin:0">${esc(card.description || t("agents.noDescription"))}</p>
        ${tags.length ? `<div class="chips" style="margin-top:10px">${tags.map((tag) => `<span class="chip plain">${esc(tag)}</span>`).join("")}</div>` : ""}
      </div>
      <div class="panel">
        <h3>${esc(t("agentDetail.panel.record"))}</h3>
        <dl class="kv">
          ${kvRow(t("agentDetail.record.status"), badge(agent.status))}
          ${kvRow(t("agentDetail.record.firstSeen"), esc(fmtTime(agent.firstSeenAt)))}
          ${kvRow(t("agentDetail.record.checked"), esc(fmtTime(agent.checkedAt)))}
          ${kvRow(t("agentDetail.record.lastOk"), esc(fmtTime(agent.lastOkAt)))}
          ${kvRow(t("agentDetail.record.changed"), agent.changedAt ? esc(fmtTime(agent.changedAt)) : `<span class='muted'>${esc(t("common.never"))}</span>`)}
          ${kvRow(t("agentDetail.record.prevHash"), agent.prevHash ? `<span class="mono">${esc(shortId(agent.prevHash))}</span>` : "<span class='muted'>—</span>")}
          ${kvRow(t("agentDetail.record.version"), esc(card.version || "—"))}
        </dl>
      </div>
    </div>

    <div class="panel">
      <h3>${esc(t("agentDetail.panel.skills"))}</h3>
      ${skillsHtml}
    </div>

    <div class="panel">
      <h3>${esc(t("agentDetail.panel.recentTasks"))}</h3>
      ${taskRows}
    </div>

    <div class="panel">
      <h3>${esc(t("agentDetail.panel.raw"))}</h3>
      <details>
        <summary class="muted small">${esc(t("agentDetail.raw.summary"))}</summary>
        <pre class="json" style="margin-top:12px">${esc(JSON.stringify(card, null, 2))}</pre>
      </details>
    </div>`;
}

/* -------------------------------------------------------------------- tasks */

function taskRow(task) {
  return `<tr class="clickable" data-task="${esc(task.id)}">
    <td>
      <div class="cell-title">${esc(task.title)}</div>
      <div class="cell-sub mono">${esc(shortId(task.remoteTaskId || task.id))}</div>
    </td>
    <td>
      <div>${esc(task.agentName)}</div>
      <div class="cell-sub">${esc(task.agentUrl)}</div>
    </td>
    <td>${badge(task.state)}</td>
    <td class="cell-sub">${esc(fmtTime(task.createdAt))}</td>
    <td class="cell-sub">${esc(fmtTime(task.updatedAt))}</td>
    <td><div class="cell-actions"><a class="btn sm ghost" href="#/tasks/${esc(task.id)}">${esc(t("tasks.table.open"))}</a></div></td>
  </tr>`;
}

async function renderTasks(view) {
  const query = new URLSearchParams({ limit: "100" });
  if (state.q) query.set("q", state.q);
  if (state.taskState) query.set("state", state.taskState);

  try {
    const data = await api(`${API}/tasks?${query}`);
    state.lastTasksJson = JSON.stringify(data.items);
    const filter = `<div class="row" style="justify-content:flex-end">
      <select id="state-filter" class="btn sm" style="padding:6px 10px">
        <option value="">${esc(t("tasks.allStates"))}</option>
        ${["active", "submitted", "working", "input-required", "completed", "failed", "canceled", "replied", "error"]
          .map((value) => `<option value="${value}" ${state.taskState === value ? "selected" : ""}>${esc(stateLabel(value))}</option>`).join("")}
      </select>
    </div>`;

    const body = data.items.length
      ? `<div class="table-wrap"><table><thead><tr>
          <th>${esc(t("tasks.table.task"))}</th><th>${esc(t("tasks.table.agent"))}</th>
          <th>${esc(t("tasks.table.state"))}</th><th>${esc(t("tasks.table.created"))}</th>
          <th>${esc(t("tasks.table.updated"))}</th><th></th>
         </tr></thead><tbody>${data.items.map(taskRow).join("")}</tbody></table></div>`
      : emptyState(t("tasks.empty.title"), t("tasks.empty.hint"));

    view.innerHTML = filter + body;
    const select = $("#state-filter");
    if (select) {
      select.addEventListener("change", () => {
        state.taskState = select.value || undefined;
        renderTasks(view);
      });
    }
  } catch (error) {
    view.innerHTML = errorPanel(error);
  }
  state.refreshTimer = setInterval(() => quietTasksRefresh(), 6000);
}

async function quietTasksRefresh() {
  if (document.visibilityState !== "visible" || $("#modal-root").children.length) return;
  if (state.route.name !== "tasks" || state.route.id) return;
  try {
    const query = new URLSearchParams({ limit: "100" });
    if (state.q) query.set("q", state.q);
    if (state.taskState) query.set("state", state.taskState);
    const data = await api(`${API}/tasks?${query}`);
    const serialized = JSON.stringify(data.items);
    if (serialized === state.lastTasksJson) return;
    state.lastTasksJson = serialized;
    await renderTasks($("#view"));
  } catch {
    /* ignore */
  }
}

function eventLine(event) {
  const payload = event.payload || {};
  const summary = payload.error || payload.text || payload.state || "";
  return `<li>
    <span class="when">${esc(fmtTime(event.at))}</span>
    <span class="what"><span class="chip plain">${esc(event.kind)}</span></span>
    <span class="muted truncate" title="${esc(summary)}">${esc(summary)}</span>
  </li>`;
}

async function renderTaskDetail(id, view) {
  view.innerHTML = skeleton(t("taskDetail.loading"));
  let data;
  try {
    data = await api(`${API}/tasks/${encodeURIComponent(id)}`);
  } catch (error) {
    view.innerHTML = errorPanel(error);
    return;
  }

  const task = data.task;
  const active = ["submitted", "working", "input-required", "auth-required"].includes(task.state);
  const events = task.events || [];

  view.innerHTML = `
    <div class="detail-head">
      <div>
        <h2>${esc(task.title)} ${badge(task.state)}</h2>
        <div class="muted small" style="margin-top:6px">
          ${esc(t("taskDetail.agent"))} <a href="#/agents/${esc(task.agentId)}"><strong>${esc(task.agentName)}</strong></a>
          · ${esc(t("taskDetail.localId"))} <span class="mono">${esc(task.id)}</span>
          ${task.remoteTaskId ? `· ${esc(t("taskDetail.remoteId"))} <span class="mono">${esc(task.remoteTaskId)}</span>` : ""}
        </div>
        <div class="muted small">${esc(t("taskDetail.endpoint"))} <span class="mono">${esc(task.endpoint || "—")}</span></div>
      </div>
      <div class="detail-actions">
        <button class="btn" data-act="refresh-task" data-id="${esc(task.id)}">${esc(t("taskDetail.refresh"))}</button>
        <button class="btn" data-act="cancel-task" data-id="${esc(task.id)}" ${task.remoteTaskId && active ? "" : "disabled"}>${esc(t("taskDetail.cancel"))}</button>
        <button class="btn danger" data-act="delete-task" data-id="${esc(task.id)}">${esc(t("taskDetail.forget"))}</button>
      </div>
    </div>

    ${task.error ? `<div class="alert"><strong>${esc(t("taskDetail.lastError"))}</strong> ${esc(task.error)}</div>` : ""}

    <div class="panel">
      <h3>${esc(t("taskDetail.panel.prompt"))}</h3>
      <div class="message-box">${esc(task.inputText)}</div>
    </div>

    <div class="panel">
      <h3>${esc(t("taskDetail.panel.output"))}</h3>
      ${task.text
        ? `<div class="message-box">${esc(task.text)}</div>`
        : `<div class="message-box empty">${esc(t("taskDetail.noOutput"))}</div>`}
      <div class="muted small" style="margin-top:8px">
        ${esc(task.artifactCount ? t("taskDetail.artifacts", { n: task.artifactCount }) : t("taskDetail.noArtifacts"))}
        · ${esc(t("taskDetail.updated", { when: fmtTime(task.updatedAt) }))}
        · ${esc(t("taskDetail.synced", { when: task.syncedAt ? fmtTime(task.syncedAt) : t("common.never") }))}
      </div>
    </div>

    <div class="panel">
      <h3>${esc(t("taskDetail.panel.follow"))}</h3>
      <form id="follow-form" class="stack">
        <div class="field" style="margin:0">
          <textarea id="follow-text" placeholder="${esc(t("taskDetail.follow.placeholder"))}"></textarea>
          <span class="hint">${t("taskDetail.follow.hint")}</span>
        </div>
        <div class="row" style="justify-content:flex-end">
          <button class="btn primary" type="submit">${esc(t("taskDetail.follow.send"))}</button>
        </div>
      </form>
    </div>

    <div class="split">
      <div class="panel">
        <h3>${esc(t("taskDetail.panel.events"))}</h3>
        ${events.length ? `<ul class="timeline">${events.map(eventLine).join("")}</ul>` : `<p class="muted small">${esc(t("taskDetail.events.empty"))}</p>`}
      </div>
      <div class="panel">
        <h3>${esc(t("taskDetail.panel.details"))}</h3>
        <dl class="kv">
          ${kvRow(t("taskDetail.detail.localId"), `<span class="mono">${esc(task.id)}</span>`)}
          ${kvRow(t("taskDetail.detail.remoteId"), task.remoteTaskId ? `<span class="mono">${esc(task.remoteTaskId)}</span>` : "<span class='muted'>—</span>")}
          ${kvRow(t("taskDetail.detail.context"), task.contextId ? `<span class="mono">${esc(task.contextId)}</span>` : "<span class='muted'>—</span>")}
          ${kvRow(t("taskDetail.detail.created"), esc(fmtTime(task.createdAt)))}
          ${kvRow(t("taskDetail.detail.updated"), esc(fmtTime(task.updatedAt)))}
          ${kvRow(t("taskDetail.detail.messages"), String(task.messageCount ?? 0))}
          ${kvRow(t("taskDetail.detail.endpoint"), `<span class="mono">${esc(task.endpoint || "—")}</span>`)}
        </dl>
      </div>
    </div>

    <div class="panel">
      <h3>${esc(t("taskDetail.panel.raw"))}</h3>
      <details>
        <summary class="muted small">${esc(t("taskDetail.raw.request"))}</summary>
        <pre class="json" style="margin-top:12px">${esc(JSON.stringify(task.request ?? {}, null, 2))}</pre>
      </details>
      <details style="margin-top:10px">
        <summary class="muted small">${esc(t("taskDetail.raw.response"))}</summary>
        <pre class="json" style="margin-top:12px">${esc(JSON.stringify(task.response ?? null, null, 2))}</pre>
      </details>
    </div>`;

  const form = $("#follow-form");
  if (form) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = $("#follow-text").value.trim();
      if (!text) return;
      const button = form.querySelector("button[type=submit]");
      button.disabled = true;
      try {
        await api(`${API}/tasks/${encodeURIComponent(task.id)}/messages`, {
          method: "POST",
          body: { text },
        });
        toast(t("action.messageSent"), "ok");
        await renderTaskDetail(id, view);
      } catch (error) {
        toast(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });
  }
}

/* ------------------------------------------------------------------- modals */

function openModal(html, options = {}) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-backdrop" data-close="1">
    <div class="modal ${options.wide ? "wide" : ""}" role="dialog" aria-modal="true">
      ${html}
    </div>
  </div>`;
  const backdrop = root.firstElementChild;
  backdrop.addEventListener("click", (event) => {
    if (event.target.dataset.close) closeModal();
  });
  document.addEventListener("keydown", escapeClose);
  const first = root.querySelector("input, textarea, select, button");
  if (first) setTimeout(() => first.focus(), 30);
}

function closeModal() {
  $("#modal-root").innerHTML = "";
  document.removeEventListener("keydown", escapeClose);
}

function escapeClose(event) {
  if (event.key === "Escape") closeModal();
}

function openRegisterDialog() {
  openModal(`
    <h2>${esc(t("register.title"))}</h2>
    <p class="sub">${esc(t("register.sub"))}</p>
    <div id="register-error"></div>
    <form id="register-form">
      <div class="field">
        <label for="register-url">${esc(t("register.label"))}</label>
        <input id="register-url" type="url" required spellcheck="false"
               placeholder="${esc(t("register.placeholder"))}" />
        <span class="hint">${esc(t("register.hint"))}</span>
      </div>
      <footer>
        <button type="button" class="btn ghost" data-act="close">${esc(t("common.cancel"))}</button>
        <button type="submit" class="btn primary">${esc(t("register.submit"))}</button>
      </footer>
    </form>`);

  $("#register-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = $("#register-url").value.trim();
    const errorBox = $("#register-error");
    errorBox.innerHTML = "";
    const submit = event.target.querySelector("button[type=submit]");
    submit.disabled = true;
    submit.textContent = t("register.fetching");
    try {
      const result = await api("/v1/agents", { method: "POST", body: { url }, noAuthPrompt: true });
      if (result.manageToken) {
        rememberManageToken(result.id, result.manageToken);
        toast(t("register.ok"), "ok");
      } else {
        toast(t("register.exists"), "ok");
      }
      closeModal();
      navigate(`#/agents/${result.id}`);
      route();
    } catch (error) {
      const problems = error.payload?.detail?.problems;
      // The contract's own message stays available, just demoted: Chinese first,
      // the wire text underneath for whoever has to diagnose it.
      const wire = error.payload?.message;
      const raw = typeof wire === "string" && wire && wire !== error.message ? wire : "";
      errorBox.innerHTML = `<div class="alert error-box">
        <strong>${esc(error.message)}</strong>
        ${Array.isArray(problems) ? `<ul style="margin:8px 0 0;padding-left:18px">${problems.map((item) => `<li>${esc(problemText(item))}</li>`).join("")}</ul>` : ""}
        ${raw ? `<div class="muted mono" style="margin-top:8px;font-size:12px">${esc(raw)}</div>` : ""}
      </div>`;
      submit.disabled = false;
      submit.textContent = t("register.submit");
    }
  });
}

function openSendDialog(agentId, agentName) {
  openModal(`
    <h2>${esc(t("send.title"))}</h2>
    <p class="sub">${t("send.sub", { name: esc(agentName || agentId) })}</p>
    <div id="send-error"></div>
    <form id="send-form">
      <div class="field">
        <label for="send-text">${esc(t("send.label"))}</label>
        <textarea id="send-text" required placeholder="${esc(t("send.placeholder"))}"></textarea>
      </div>
      <div class="field">
        <label for="send-context">${esc(t("send.contextLabel"))}</label>
        <input id="send-context" spellcheck="false" placeholder="${esc(t("send.contextPlaceholder"))}" />
      </div>
      <footer>
        <button type="button" class="btn ghost" data-act="close">${esc(t("common.cancel"))}</button>
        <button type="submit" class="btn primary">${esc(t("send.submit"))}</button>
      </footer>
    </form>`);

  $("#send-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = $("#send-text").value.trim();
    if (!text) return;
    const contextId = $("#send-context").value.trim();
    const submit = event.target.querySelector("button[type=submit]");
    submit.disabled = true;
    submit.textContent = t("send.sending");
    try {
      const result = await api(`${API}/agents/${encodeURIComponent(agentId)}/tasks`, {
        method: "POST",
        body: { text, contextId: contextId || null },
      });
      closeModal();
      if (result.task.error) {
        toast(t("send.failed", { error: result.task.error }), "error");
      } else {
        toast(t("send.ok"), "ok");
      }
      navigate(`#/tasks/${result.task.id}`);
      route();
    } catch (error) {
      $("#send-error").innerHTML = `<div class="alert error-box">${esc(error.message)}</div>`;
      submit.disabled = false;
      submit.textContent = t("send.submit");
    }
  });
}

function removalPolicy() {
  // Who may remove an entry without a manage token is the panel's call, not the
  // UI's: it answers with {operator, reason} in the summary.
  return state.summary?.removal || { operator: false, reason: "manage-token" };
}

function openDeleteAgentDialog(agentId, agentName) {
  const stored = manageTokens()[agentId] || "";
  const policy = removalPolicy();
  // Ownership always wins: if this browser holds the manage token, the contract's
  // own path is used and operator authority is irrelevant.
  const byOperator = !stored && policy.operator;
  const reasonKey = `deleteAgent.operator.reason.${policy.reason}`;
  const reason = t(reasonKey) === reasonKey ? "" : t(reasonKey);
  const blocked = !stored && !policy.operator;

  const body = byOperator
    ? `<div class="alert">${esc(t("deleteAgent.operator.note"))}${reason ? " " + esc(reason) : ""}</div>`
    : `<div class="field">
        <label for="delete-token">${esc(t("deleteAgent.label"))}</label>
        <input id="delete-token" spellcheck="false" value="${esc(stored)}"
               placeholder="${esc(t("deleteAgent.placeholder"))}" />
        <span class="hint">${esc(stored ? t("deleteAgent.hint.stored") : t("deleteAgent.hint.missing"))}</span>
      </div>
      ${blocked ? `<div class="alert">${esc(t("deleteAgent.hint.blocked"))}</div>` : ""}`;

  openModal(`
    <h2>${esc(t("deleteAgent.title"))}</h2>
    <p class="sub">${esc(t("deleteAgent.sub"))}</p>
    <div id="delete-error"></div>
    <form id="delete-form">
      ${body}
      <footer>
        <button type="button" class="btn ghost" data-act="close">${esc(t("common.cancel"))}</button>
        <button type="submit" class="btn danger">${esc(byOperator ? t("deleteAgent.force") : t("deleteAgent.submit"))}</button>
      </footer>
    </form>`);

  $("#delete-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#delete-token");
    const token = input ? input.value.trim() : "";
    try {
      // A token goes on the wire only when there is one; an empty body is what
      // asks the panel to decide on its operator authority.
      await api(`${API}/agents/${encodeURIComponent(agentId)}`, {
        method: "DELETE",
        body: token ? { token } : undefined,
        noAuthPrompt: true,
      });
      forgetManageToken(agentId);
      closeModal();
      toast(agentName ? t("deleteAgent.ok", { name: agentName }) : t("deleteAgent.okFallback"), "ok");
      navigate("#/agents");
      route();
    } catch (error) {
      $("#delete-error").innerHTML = `<div class="alert error-box">${esc(error.message)}</div>`;
    }
  });
}

function openTokenDialog() {
  if ($("#token-modal")) return;
  openModal(`
    <h2 id="token-modal">${esc(t("token.title"))}</h2>
    <p class="sub">${esc(t("token.sub"))}</p>
    <form id="token-form">
      <div class="field">
        <label for="token-input">${esc(t("token.label"))}</label>
        <input id="token-input" type="password" autocomplete="off" spellcheck="false" />
      </div>
      <footer>
        <button type="button" class="btn ghost" data-act="close">${esc(t("common.cancel"))}</button>
        <button type="submit" class="btn primary">${esc(t("token.save"))}</button>
      </footer>
    </form>`);

  $("#token-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const token = $("#token-input").value.trim();
    if (!token) return;
    localStorage.setItem(TOKEN_KEY, token);
    closeModal();
    toast(t("token.ok"), "ok");
    route();
  });
}

/* ------------------------------------------------------------------ actions */

async function refreshAgent(id) {
  try {
    await api(`${API}/agents/${encodeURIComponent(id)}/refresh`, { method: "POST" });
    toast(t("action.refreshed"), "ok");
    route();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function refreshTask(id) {
  try {
    const result = await api(`${API}/tasks/${encodeURIComponent(id)}/refresh`, { method: "POST" });
    if (result.task.error) toast(result.task.error, "error");
    else toast(t("action.state", { state: stateLabel(result.task.state) }), "ok");
    route();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function cancelTask(id) {
  try {
    const result = await api(`${API}/tasks/${encodeURIComponent(id)}/cancel`, { method: "POST" });
    if (result.task.error) toast(result.task.error, "error");
    else toast(t("action.cancelRequested", { state: stateLabel(result.task.state) }), "ok");
    route();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function deleteTask(id) {
  try {
    await api(`${API}/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast(t("action.taskRemoved"), "ok");
    navigate("#/tasks");
    route();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* --------------------------------------------------------------------- boot */

document.addEventListener("click", (event) => {
  const action = event.target.closest("[data-act]");
  if (action) {
    const { act, id } = action.dataset;
    if (act === "register") openRegisterDialog();
    if (act === "close") closeModal();
    if (act === "toggle-lang") toggleLang();
    if (act === "send") {
      const agentId = id || state.route.id;
      if (agentId) openSendDialog(agentId, action.dataset.name || "");
    }
    if (act === "refresh-agent") refreshAgent(id);
    if (act === "delete-agent") openDeleteAgentDialog(id, action.dataset.name || "");
    if (act === "refresh-task") refreshTask(id);
    if (act === "cancel-task") cancelTask(id);
    if (act === "delete-task") deleteTask(id);
    return;
  }

  const row = event.target.closest("tr[data-task]");
  if (row && !event.target.closest("a, button")) {
    navigate(`#/tasks/${row.dataset.task}`);
  }
});

$("#register-btn").addEventListener("click", openRegisterDialog);

applyLangChrome();

let searchTimer = null;
$("#search").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  const value = event.target.value.trim();
  searchTimer = setTimeout(() => {
    state.q = value;
    route();
  }, 220);
});

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", () => {
  applyLangChrome();
  route();
});

// Also run when the module is evaluated after DOMContentLoaded already fired.
if (document.readyState !== "loading") {
  route();
}
