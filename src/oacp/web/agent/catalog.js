/**
 * The panel's capability catalog — what the assistant may do, and the code that
 * does it.
 *
 * Two rules shape this file:
 *
 * 1. **A capability is a real function with a real schema**, not a hint the model
 *    emits for the page to interpret. `ui_theme("dark")` is validated against
 *    `{type:"string", enum:["light","dark","auto"]}` before anything changes; a
 *    protocol that let model *text* become a command would put "switch the theme"
 *    and "delete this conversation" in the same trust class.
 * 2. **Bodies never touch the DOM directly.** They call an injected `ui` / `api`
 *    seam, which is what lets all of this run in Node without a browser.
 *
 * Grouping is by tier, and the tier is also the policy: with `--agent-policy read`
 * the write and destroy declarations are never sent at all. A model cannot
 * hallucinate-call a name it never saw.
 */

/** Result text larger than this is cut before it reaches the model. */
export const MAX_RESULT_CHARS = 4000;

/** Which tiers each policy exposes. */
export const POLICY_TIERS = Object.freeze({
  read: ["read"],
  act: ["read", "act"],
  all: ["read", "act", "destroy"],
  delegate: ["read", "act", "destroy", "delegate"],
});

export const TIER_READ = "read";
export const TIER_ACT = "act";
export const TIER_DESTROY = "destroy";
export const TIER_DELEGATE = "delegate";

// ------------------------------------------------------------------ formatting

/** Trim a value to one readable line. */
function line(value, max = 140) {
  const text = String(value === null || value === undefined ? "" : value)
    .replace(/\s+/g, " ")
    .trim();
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

/** Cut a result down to the budget, and say so rather than pretending it fit. */
export function clip(text, max = MAX_RESULT_CHARS) {
  const value = String(text === null || text === undefined ? "" : text);
  if (value.length <= max) return value;
  // Already clipped by a formatter: keep its note. Slicing it again would report
  // the length of the note itself as the number of characters omitted.
  const note = /\n… \+\d+ chars not shown$/.exec(value);
  if (note && value.length - note[0].length <= max) return value;
  return `${value.slice(0, max)}\n… +${value.length - max} chars not shown`;
}

function stamp(value) {
  const text = String(value || "");
  return text.replace("T", " ").replace(/\..*$/, "").replace(/Z$/, "");
}

function formatSession(item) {
  const bits = [`  ${item.id}  "${line(item.title, 40)}"  ${item.state}`];
  bits.push(`    agent=${item.agentName || item.agentId} messages=${item.messageCount}`);
  bits.push(`    updated=${stamp(item.updatedAt)}`);
  return bits.join("\n");
}

function formatTask(item) {
  const bits = [`  ${item.id}  "${line(item.title, 40)}"  ${item.state}`];
  bits.push(`    agent=${item.agentName || item.agentId} created=${stamp(item.createdAt)}`);
  if (item.sessionId) bits.push(`    session=${item.sessionId}`);
  return bits.join("\n");
}

function formatMessages(messages, limit) {
  const slice = messages.slice(-limit);
  const body = slice.map((message) => {
    const who = message.role === "user" ? "user" : "agent";
    const files = (message.attachments || []).length
      ? ` [+${message.attachments.length} image(s)]`
      : "";
    return `  ${who}: ${line(message.text, 320)}${files}`;
  });
  if (messages.length > slice.length) {
    body.unshift(`  … ${messages.length - slice.length} earlier message(s) not shown`);
  }
  return body.join("\n");
}

// --------------------------------------------------------------- arg validation

function typeMatches(type, value) {
  switch (type) {
    case "object":
      return value !== null && typeof value === "object" && !Array.isArray(value);
    case "array":
      return Array.isArray(value);
    case "string":
      return typeof value === "string";
    case "integer":
      return Number.isInteger(value);
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "boolean":
      return typeof value === "boolean";
    default:
      return true;
  }
}

function checkSchema(schema, value, path, problems) {
  const where = path || "(root)";
  if (schema.type && !typeMatches(schema.type, value)) {
    problems.push(`${where}: expected ${schema.type}`);
    return;
  }
  if (Array.isArray(schema.enum) && !schema.enum.includes(value)) {
    problems.push(`${where}: must be one of ${schema.enum.join("|")}`);
  }
  if (typeof value === "string") {
    if (Number.isInteger(schema.maxLength) && value.length > schema.maxLength) {
      problems.push(`${where}: longer than ${schema.maxLength}`);
    }
    if (Number.isInteger(schema.minLength) && value.length < schema.minLength) {
      problems.push(`${where}: shorter than ${schema.minLength}`);
    }
  }
  if (typeof value === "number") {
    if (typeof schema.minimum === "number" && value < schema.minimum) {
      problems.push(`${where}: below ${schema.minimum}`);
    }
    if (typeof schema.maximum === "number" && value > schema.maximum) {
      problems.push(`${where}: above ${schema.maximum}`);
    }
  }
  if (schema.properties || schema.type === "object") {
    const object = value && typeof value === "object" ? value : {};
    for (const key of Array.isArray(schema.required) ? schema.required : []) {
      // Own-property, not `in`: `in` walks the prototype chain, so a required key
      // spelled `constructor` would look present and the body would get undefined.
      if (!Object.prototype.hasOwnProperty.call(object, key)) {
        problems.push(`${where}.${key}: required`);
      }
    }
    for (const [key, child] of Object.entries(schema.properties || {})) {
      if (Object.prototype.hasOwnProperty.call(object, key) && object[key] !== undefined) {
        checkSchema(child, object[key], path ? `${path}.${key}` : key, problems);
      }
    }
    if (schema.additionalProperties === false) {
      // Otherwise a misspelled optional argument (`statuss` for `status`) is
      // dropped in silence and the capability answers as if it was never given.
      const known = schema.properties || {};
      for (const key of Object.keys(object)) {
        if (!Object.prototype.hasOwnProperty.call(known, key)) {
          problems.push(`${path ? `${path}.${key}` : key}: unexpected`);
        }
      }
    }
  }
  if (schema.type === "array" && Array.isArray(value) && schema.items) {
    value.forEach((item, index) => checkSchema(schema.items, item, `${path}[${index}]`, problems));
  }
}

/**
 * Check `value` against the subset of JSON Schema this catalog uses.
 *
 * A validator is not optional here. Without it an `enum` is decoration, and the
 * first malformed argument reaches the capability body — where "delete" would
 * have to defend itself with ad-hoc checks, in every capability, forever.
 */
export function validateArguments(schema, value) {
  const problems = [];
  if (schema && typeof schema === "object") checkSchema(schema, value, "", problems);
  return problems;
}

// ------------------------------------------------------------------- catalog

/**
 * Build the catalog.
 *
 * @param {object} options
 * @param {Function} options.api  `api(path, init)` — the panel's fetch wrapper
 * @param {object} options.ui     DOM seam: theme/lang/notify/toast/open/draft/
 *                                download/modal/clientInfo/confirm
 * @param {string} [options.policy] read | act | all | delegate
 */
export function createCatalog({ api, ui, policy = "act" } = {}) {
  const object = (properties = {}, required = []) => ({
    type: "object",
    additionalProperties: false,
    properties,
    required,
  });

  const entries = [
    // ----------------------------------------------------------- ui (browser only)
    {
      tier: TIER_READ,
      declaration: {
        name: "ui_theme",
        description: "切换面板主题（浅色 / 深色 / 跟随系统）。用户说「换个深色」「暗色模式」时用它。",
        timeoutMs: 5000,
        parameters: object({ theme: { type: "string", enum: ["light", "dark", "auto"], description: "light=浅色, dark=深色, auto=跟随系统" } }, ["theme"]),
      },
      run: async ({ theme }) => {
        ui.setTheme(theme);
        return `theme=${theme}`;
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "ui_lang",
        description: "切换界面语言（中 / 英）。用户要求换语言时用它。",
        timeoutMs: 5000,
        parameters: object({ lang: { type: "string", enum: ["zh", "en"], description: "zh=中文, en=English" } }, ["lang"]),
      },
      run: async ({ lang }) => {
        ui.setLang(lang);
        return `lang=${lang}`;
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "ui_notify",
        description: "发一条系统通知（浏览器通知）。需要用户离开页面也能看到结果时用它。",
        timeoutMs: 15000,
        parameters: object({ title: { type: "string", minLength: 1, maxLength: 120 }, body: { type: "string", maxLength: 400 } }, ["title"]),
      },
      run: async ({ title, body }) => {
        await ui.notify({ title, body: body || "" });
        return `notified: ${title}`;
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "ui_toast",
        description: "在页面右下角弹一条短提示。只是告知，不打断操作。",
        timeoutMs: 5000,
        parameters: object({
          text: { type: "string", minLength: 1, maxLength: 200 },
          level: { type: "string", enum: ["ok", "warn", "error"] },
        }, ["text"]),
      },
      run: async ({ text, level }) => {
        ui.toast(text, level || "ok");
        return `toast: ${text}`;
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "ui_open",
        description: "把用户带到面板上的某个智能体或会话。能显示的东西优先用它，而不是让用户自己找。",
        timeoutMs: 10000,
        parameters: object({
          kind: { type: "string", enum: ["agent", "session"], description: "agent=某智能体的会话列表, session=某个会话" },
          id: { type: "string", minLength: 1, maxLength: 120 },
        }, ["kind", "id"]),
      },
      run: async ({ kind, id }) => {
        const where = await ui.open({ kind, id });
        return `opened ${kind} ${where || id}`;
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "ui_draft",
        description:
          "把一段话写进输入框（不发送），让用户改完自己按发送。要用户确认或补充时用它。默认追加在用户已有草稿之后、绝不覆盖；要改写你自己刚写的那段（用户说「缩短它」「换个说法」），传 mode=replace。",
        timeoutMs: 5000,
        parameters: object({
          text: { type: "string", minLength: 1, maxLength: 20000 },
          mode: {
            type: "string",
            enum: ["append", "replace"],
            description: "append=接在原有草稿后面（默认，安全）；replace=整段替换输入框，只在改写你自己写的草稿时用",
          },
        }, ["text"]),
      },
      run: async ({ text, mode }) => {
        // Say which one happened: the model needs to know whether the person is
        // looking at a fresh draft, at their own text with ours appended, or at a
        // draft we replaced outright.
        const outcome = ui.draft(text, mode || "append");
        if (outcome === "replaced") {
          return "draft replaced the whole composer; the user still has to send it";
        }
        return outcome === "appended"
          ? "draft appended to the text already in the composer; the user still has to send it"
          : "draft written; the user still has to send it";
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "ui_download",
        description: "把会话里的一张图片附件下载到用户本地。参数是附件 id（从 panel_get_session 的 attachments 里拿）。",
        timeoutMs: 60000,
        parameters: object({ attachment_id: { type: "integer", minimum: 1 } }, ["attachment_id"]),
      },
      run: async ({ attachment_id }, ctx) => {
        ctx.markDispatched();
        const saved = await ui.download(attachment_id);
        return `downloaded attachment ${attachment_id}${saved ? ` as ${saved}` : ""}`;
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "ui_modal",
        description: "用弹窗展示较长的内容（比 toast 适合长文本）。",
        timeoutMs: 5000,
        parameters: object({
          title: { type: "string", minLength: 1, maxLength: 120 },
          body: { type: "string", minLength: 1, maxLength: 4000 },
        }, ["title", "body"]),
      },
      run: async ({ title, body }) => {
        ui.modal({ title, body });
        return `modal: ${title}`;
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "ui_client_info",
        description:
          "浏览器现在是什么状态：视口、时区、语言、主题、当前路由、是否在线，以及输入框里的草稿字数与待发附件张数（composer=；closed 表示当前页面没有输入框）。往输入框写东西（ui_draft / ui_attach）之前先看它，免得盖掉用户正在打的字。",
        timeoutMs: 5000,
        parameters: object({}, []),
      },
      run: async () => {
        const info = ui.clientInfo();
        return Object.entries(info)
          .map(([key, value]) => `${key}=${value}`)
          .join("\n");
      },
    },

    // ---------------------------------------------------------- ui: act
    {
      tier: TIER_ACT,
      declaration: {
        name: "ui_attach",
        description:
          "把会话里已有的一张图片放进输入框（不发送），用户自己改完再按发送。用户问「把那张图发给某人」时用它；附件 id 从 panel_get_session 的 attachments 取，不许编造。",
        timeoutMs: 30000,
        parameters: object({ attachment_id: { type: "integer", minimum: 1 } }, ["attachment_id"]),
      },
      run: async ({ attachment_id }, ctx) => {
        ctx.markDispatched();
        // Honour cancellation: a fetch that outlives the call would stage a file
        // for a turn that is already over.
        const name = await ui.attach(attachment_id, ctx.signal);
        return `attached ${name} to the composer; the user still has to send it`;
      },
    },

    // ------------------------------------------------------- panel: read
    {
      tier: TIER_READ,
      declaration: {
        name: "panel_overview",
        description: "面板统计：智能体 / 会话 / 任务的数量与状态分布。问「有多少」「整体情况」时先用它。",
        timeoutMs: 10000,
        parameters: object({}, []),
      },
      run: async () => {
        const data = await api("/api/panel/summary");
        const agents = data.agents || {};
        const tasks = data.tasks || {};
        const sessions = data.sessions || {};
        return clip(
          [
            `agents: total=${agents.total || 0} ok=${agents.ok || 0} down=${agents.down || 0}`,
            `sessions: total=${sessions.total || 0} active=${sessions.active || 0}`,
            `tasks: total=${tasks.total || 0} active=${tasks.active || 0}`,
            `panel: ${data.name || ""} ${data.version || ""} publicUrl=${data.publicUrl || ""}`,
          ].join("\n"),
        );
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "panel_search",
        description: "在智能体、会话、任务里搜索关键词（跨全部记录，不只是当前打开的）。找不到东西时用它。",
        timeoutMs: 15000,
        parameters: object({
          q: { type: "string", minLength: 1, maxLength: 100 },
          scope: { type: "string", enum: ["all", "agents", "sessions", "tasks"] },
        }, ["q"]),
      },
      run: async ({ q, scope }) => {
        const params = new URLSearchParams({ q });
        if (scope) params.set("scope", scope);
        const data = await api(`/api/panel/search?${params.toString()}`);
        const blocks = [`q="${data.q}" counts=${JSON.stringify(data.counts)}`];
        if (data.agents && data.agents.length) {
          blocks.push("agents:", ...data.agents.map((item) => `  ${item.id}  ${line(item.name, 50)}  ${item.status}`));
        }
        if (data.sessions && data.sessions.length) {
          blocks.push("sessions:", ...data.sessions.map(formatSession));
        }
        if (data.tasks && data.tasks.length) {
          blocks.push("tasks:", ...data.tasks.map(formatTask));
        }
        return clip(blocks.join("\n"));
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "panel_list_agents",
        description: "列出注册在面板里的智能体，可按关键词或状态过滤。",
        timeoutMs: 15000,
        parameters: object({
          q: { type: "string", maxLength: 100 },
          status: { type: "string", enum: ["ok", "down"] },
          limit: { type: "integer", minimum: 1, maximum: 50 },
          cursor: { type: "string", maxLength: 200, description: "上一页返回的 nextCursor，取下一页时原样传回" },
        }, []),
      },
      run: async ({ q, status, limit, cursor }) => {
        const params = new URLSearchParams();
        if (q) params.set("q", q);
        if (status) params.set("status", status);
        if (cursor) params.set("cursor", cursor);
        params.set("limit", String(limit || 20));
        const data = await api(`/api/panel/agents?${params.toString()}`);
        const items = data.items || [];
        const body = items.map((item) => {
          const extra = [`${item.status}`];
          if (item.taskCount !== undefined) extra.push(`tasks=${item.taskCount}`);
          if (item.sessionCount !== undefined) extra.push(`sessions=${item.sessionCount}`);
          return `  ${item.id}  ${line(item.name, 50)}  ${extra.join(" ")}`;
        });
        // Naming the cursor is the difference between "there is more" and a model
        // that can actually reach it — and between a truncated list read as complete.
        const more = data.nextCursor ? ` (more: pass cursor=${data.nextCursor})` : "";
        return clip([`agents: ${items.length}${more}`, ...body].join("\n"));
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "panel_get_agent",
        description: "一个智能体的详情：名称、描述、地址、状态、技能与最近任务。",
        timeoutMs: 15000,
        parameters: object({ id: { type: "string", minLength: 1, maxLength: 120 } }, ["id"]),
      },
      run: async ({ id }) => {
        const data = await api(`/api/panel/agents/${encodeURIComponent(id)}`);
        const agent = data.agent || {};
        const skills = (agent.skills || []).slice(0, 12).join(", ");
        const tasks = (data.tasks || []).slice(0, 8).map(formatTask);
        return clip(
          [
            `id=${agent.id} status=${agent.status}`,
            `name: ${line(agent.name, 120)}`,
            `url: ${agent.url || ""}`,
            agent.description ? `description: ${line(agent.description, 400)}` : "",
            skills ? `skills: ${skills}` : "",
            `tasks: total=${(data.stats || {}).taskCount || 0} active=${(data.stats || {}).activeTaskCount || 0}`,
            ...tasks,
          ]
            .filter(Boolean)
            .join("\n"),
        );
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "panel_list_sessions",
        description: "列出会话（对话），最新的在前。可按智能体过滤。",
        timeoutMs: 15000,
        parameters: object({
          agent_id: { type: "string", maxLength: 120 },
          limit: { type: "integer", minimum: 1, maximum: 50 },
        }, []),
      },
      run: async ({ agent_id, limit }) => {
        const params = new URLSearchParams();
        if (agent_id) params.set("agentId", agent_id);
        params.set("limit", String(limit || 20));
        const data = await api(`/api/panel/sessions?${params.toString()}`);
        const items = data.items || [];
        // `/api/panel/sessions` has no cursor, so the honest signal is "this was a
        // full page" — without it a truncated list reads as the whole ledger and the
        // model under-reports. `agent_id` is the way to narrow it.
        const wanted = Number(limit) || 20;
        const more = items.length >= wanted ? " (may be more: raise limit or pass agent_id)" : "";
        return clip([`sessions: ${items.length}${more}`, ...items.map(formatSession)].join("\n"));
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "panel_get_session",
        description: "一个会话的完整内容：标题、状态与消息（默认最近 20 条）。要看「说了什么」时用它。",
        timeoutMs: 15000,
        parameters: object({
          id: { type: "string", minLength: 1, maxLength: 120 },
          limit: { type: "integer", minimum: 1, maximum: 60 },
        }, ["id"]),
      },
      run: async ({ id, limit }) => {
        const data = await api(`/api/panel/sessions/${encodeURIComponent(id)}`);
        const session = data.session || {};
        const messages = session.messages || [];
        const files = messages
          .flatMap((message) => message.attachments || [])
          .slice(-10)
          .map(
            (file) =>
              `  attachment id=${file.id} ${line(file.name, 60)} ${file.mime} ${file.size}B${file.skipped ? ` skipped=${file.skipped}` : ""}`,
          );
        const head = [
          `session ${session.id} "${line(session.title, 60)}" state=${session.state}`,
          `agent=${session.agentName || session.agentId} messages=${session.messageCount} updated=${stamp(session.updatedAt)}`,
          // Attachment ids first: `ui_download` takes them, and they are small.
          ...(files.length ? ["attachments:", ...files] : []),
        ].join("\n");

        // `clip` keeps the head and drops the tail — and the tail is the *newest*
        // messages, which is exactly what this declaration promises. Shrink the
        // window until the block fits, rather than letting the cap discard the part
        // the model asked for.
        let block = "";
        const wanted = Math.min(Number(limit) || 20, messages.length);
        for (let count = wanted; count >= 1; count -= 1) {
          block = formatMessages(messages, count);
          if (head.length + block.length + 1 <= MAX_RESULT_CHARS) break;
        }
        return clip([head, block].join("\n"));
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "panel_list_tasks",
        description: "列出 A2A 任务（一次具体调用），最新在前。可按状态过滤。",
        timeoutMs: 15000,
        parameters: object({
          state: { type: "string", maxLength: 40, description: "如 submitted / working / completed / failed / cancelled" },
          limit: { type: "integer", minimum: 1, maximum: 50 },
          cursor: { type: "string", maxLength: 200, description: "上一页返回的 nextCursor，取下一页时原样传回" },
        }, []),
      },
      run: async ({ state, limit, cursor }) => {
        const params = new URLSearchParams();
        if (state) params.set("state", state);
        if (cursor) params.set("cursor", cursor);
        params.set("limit", String(limit || 20));
        const data = await api(`/api/panel/tasks?${params.toString()}`);
        const items = data.items || [];
        // A full page must not look like the whole ledger.
        const more = data.nextCursor ? ` (more: pass cursor=${data.nextCursor})` : "";
        return clip([`tasks: ${items.length}${more}`, ...items.map(formatTask)].join("\n"));
      },
    },
    {
      tier: TIER_READ,
      declaration: {
        name: "panel_get_task",
        description: "一个任务的详情：状态、输入、最新回复、错误与事件数。排查「为什么没回」时用它。",
        timeoutMs: 15000,
        parameters: object({ id: { type: "string", minLength: 1, maxLength: 120 } }, ["id"]),
      },
      run: async ({ id }) => {
        const data = await api(`/api/panel/tasks/${encodeURIComponent(id)}`);
        const task = data.task || {};
        return clip(
          [
            `task ${task.id} state=${task.state}`,
            `agent=${task.agentName || task.agentId} created=${stamp(task.createdAt)} updated=${stamp(task.updatedAt)}`,
            `title: ${line(task.title, 200)}`,
            `input: ${line(task.inputText, 500)}`,
            task.text ? `reply: ${line(task.text, 800)}` : "",
            task.error ? `error: ${line(task.error, 400)}` : "",
            `messages=${task.messageCount} artifacts=${task.artifactCount} pushState=${task.pushState || "-"}`,
          ]
            .filter(Boolean)
            .join("\n"),
        );
      },
    },

    // -------------------------------------------------------- panel: act
    {
      tier: TIER_ACT,
      declaration: {
        name: "panel_rename_session",
        description: "给一个会话改名。",
        timeoutMs: 15000,
        parameters: object({
          id: { type: "string", minLength: 1, maxLength: 120 },
          title: { type: "string", minLength: 1, maxLength: 80 },
        }, ["id", "title"]),
      },
      run: async ({ id, title }, ctx) => {
        ctx.markDispatched();
        const data = await api(`/api/panel/sessions/${encodeURIComponent(id)}`, {
          method: "PATCH",
          body: { title },
        });
        return `renamed ${id} -> "${line((data.session || {}).title || title, 60)}"`;
      },
    },
    {
      tier: TIER_ACT,
      declaration: {
        name: "panel_refresh_agent",
        description: "立刻重新拉一个智能体的卡片（不等后台探测）。怀疑信息过期时用它。",
        timeoutMs: 30000,
        parameters: object({ id: { type: "string", minLength: 1, maxLength: 120 } }, ["id"]),
      },
      run: async ({ id }, ctx) => {
        ctx.markDispatched();
        const data = await api(`/api/panel/agents/${encodeURIComponent(id)}/refresh`, { method: "POST" });
        const agent = data.agent || {};
        return `refreshed ${id}: status=${agent.status}`;
      },
    },
    {
      tier: TIER_ACT,
      declaration: {
        name: "panel_cancel_session",
        description: "停止一个会话正在跑的那一轮。",
        timeoutMs: 30000,
        parameters: object({ id: { type: "string", minLength: 1, maxLength: 120 } }, ["id"]),
      },
      run: async ({ id }, ctx) => {
        ctx.markDispatched();
        const data = await api(`/api/panel/sessions/${encodeURIComponent(id)}/cancel`, { method: "POST" });
        return `cancel requested for ${id}: state=${(data.session || {}).state}`;
      },
    },

    // ---------------------------------------------------- panel: destroy (gated)
    {
      tier: TIER_DESTROY,
      declaration: {
        name: "panel_delete_session",
        description: "删除一个会话。会先弹窗要求用户在面板上确认；用户不点，就什么都不会发生。",
        // The gate holds this call open while a person reads it, so the declared
        // timeout has to cover a human, not a request.
        timeoutMs: 120000,
        parameters: object({ id: { type: "string", minLength: 1, maxLength: 120 } }, ["id"]),
      },
      run: async ({ id }, ctx) => {
        let title = id;
        try {
          const data = await api(`/api/panel/sessions/${encodeURIComponent(id)}`);
          title = (data.session || {}).title || id;
        } catch {
          /* naming is a nicety; the confirmation must still be possible */
        }
        const approved = await ctx.confirm({
          title: "删除会话",
          sub: `确定删除会话「${line(title, 60)}」（${id}）？此操作不可撤销。`,
        });
        if (!approved) {
          throw ctx.rejected(ctx.signal.aborted ? "busy" : "denied");
        }
        ctx.markDispatched();
        await api(`/api/panel/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
        return `deleted session ${id}`;
      },
    },
    {
      tier: TIER_DESTROY,
      declaration: {
        name: "panel_delete_agent",
        description: "从面板移除一个智能体。会先弹窗要求用户在面板上确认；用户不点，就什么都不会发生。",
        timeoutMs: 120000,
        parameters: object({ id: { type: "string", minLength: 1, maxLength: 120 } }, ["id"]),
      },
      run: async ({ id }, ctx) => {
        let name = id;
        try {
          const data = await api(`/api/panel/agents/${encodeURIComponent(id)}`);
          name = (data.agent || {}).name || id;
        } catch {
          /* same as above: the human still gets to decide */
        }
        const approved = await ctx.confirm({
          title: "移除智能体",
          sub: `确定把「${line(name, 60)}」（${id}）从面板移除？已发生的任务记录会保留。`,
        });
        if (!approved) {
          throw ctx.rejected(ctx.signal.aborted ? "busy" : "denied");
        }
        ctx.markDispatched();
        await api(`/api/panel/agents/${encodeURIComponent(id)}`, {
          method: "DELETE",
          body: { token: (ui.manageToken && ui.manageToken(id)) || "" },
        });
        return `removed agent ${id}`;
      },
    },

    // ------------------------------------------------- panel: delegate (hidden)
    {
      tier: TIER_DELEGATE,
      declaration: {
        name: "panel_send_message",
        description: "以用户身份向某个会话发送一条消息。会让远端智能体开始工作，默认不启用。",
        timeoutMs: 60000,
        parameters: object({
          session_id: { type: "string", minLength: 1, maxLength: 120 },
          text: { type: "string", minLength: 1, maxLength: 8000 },
        }, ["session_id", "text"]),
      },
      run: async ({ session_id, text }, ctx) => {
        ctx.markDispatched();
        const data = await api(`/api/panel/sessions/${encodeURIComponent(session_id)}/messages`, {
          method: "POST",
          body: { text, attachments: [] },
        });
        const session = data.session || {};
        return clip(`sent to ${session_id}: state=${session.state}\n${formatMessages(session.messages || [], 4)}`);
      },
    },
  ];

  const allowed = new Set(POLICY_TIERS[policy] || POLICY_TIERS.act);
  const active = entries.filter((entry) => allowed.has(entry.tier));
  const byName = new Map(active.map((entry) => [entry.declaration.name, entry]));

  return {
    policy,
    names: active.map((entry) => entry.declaration.name),
    declarations: active.map((entry) => entry.declaration),
    has: (name) => byName.has(name),
    /**
     * Run one call and return a `FunctionResult`-shaped object.
     *
     * The four failure modes are kept apart on purpose — the model uses the
     * difference to decide "retry" vs "fix the arguments":
     *   error    executed and failed → change something and try again
     *   rejected not executed (denied / busy) → retry, or stop asking
     */
    async execute(call = {}) {
      const entry = call && call.name ? byName.get(call.name) : undefined;
      if (!entry) return { status: "error", reason: "FUNCTION_UNKNOWN" };
      let args = call.arguments;
      if (args === null || args === undefined) {
        // `argumentsRaw` is the required field on the wire and `arguments` is
        // optional, so a conforming peer may send only the raw JSON. Falling back
        // to it is not guessing; refusing only when neither parses is.
        try {
          args = call.argumentsRaw ? JSON.parse(call.argumentsRaw) : undefined;
        } catch {
          args = undefined;
        }
      }
      if (args === null || args === undefined) {
        return { status: "error", reason: "ARGUMENTS_UNPARSABLE" };
      }
      const problems = validateArguments(entry.declaration.parameters, args);
      if (problems.length) {
        return {
          status: "error",
          reason: "ARGUMENTS_INVALID",
          parts: [{ text: clip(problems.join("; "), 600) }],
        };
      }

      const budget = Math.max(50, Math.floor((Number(call.deadlineMs) || 15000) * 0.9));
      const controller = new AbortController();
      let canceled = false;
      if (call.signal) {
        // `client.js` aborts this on `aaas/cancelCall`. Without the bridge the
        // server's cancel reaches nobody: a held confirmation stays mounted and a
        // dispatched delete still lands after the server stopped waiting.
        if (call.signal.aborted) canceled = true;
        else
          call.signal.addEventListener(
            "abort",
            () => {
              canceled = true;
              controller.abort();
            },
            { once: true },
          );
      }
      // The timer has to settle the *invoke*, not merely abort the controller: a
      // body that ignores the signal (a fetch with no options) would otherwise
      // answer long after the server stopped waiting, and that late `ok` would be
      // read as a success the model can act on.
      let expire;
      let timedOut = false;
      let dispatched = false;
      const expired = new Promise((_, reject) => {
        expire = setTimeout(() => {
          timedOut = true;
          controller.abort();
          const error = new Error("aborted");
          error.rejected = true;
          error.reason = "busy";
          reject(error);
        }, budget);
      });
      const ctx = {
        signal: controller.signal,
        // Capability bodies call this immediately before a request that cannot be
        // taken back. A timeout after that point has an unknown outcome; a timeout
        // before it (a confirmation nobody answered) ran nothing at all.
        markDispatched: () => {
          dispatched = true;
        },
        confirm: ({ title, sub }) => ui.confirm({ title, sub, signal: controller.signal }),
        rejected: (reason) => {
          const error = new Error(`rejected:${reason}`);
          error.rejected = true;
          error.reason = reason;
          return error;
        },
      };

      const started = Date.now();
      try {
        const value = await Promise.race([entry.run(args, ctx), expired]);
        return { status: "ok", parts: [{ text: clip(value) }], elapsedMs: Date.now() - started };
      } catch (error) {
        // `error.name` is "Error" for ordinary throwables; passing it through would
        // hand the model a reason code of "Error" where it needs a stable one.
        const named = error && error.name && error.name !== "Error" ? error.name : "";
        const reason = (error && (error.reason || named)) || "";

        if ((canceled || (timedOut && dispatched)) && dispatched) {
          // The request may already have reached the server. Reporting `rejected`
          // ("did not run") would be a lie the model repeats to the person — and for
          // a send, a retry would post the same message twice.
          return {
            status: "error",
            reason: "OUTCOME_UNKNOWN",
            parts: [
              {
                text: "请求已发出，但未在期限内确认结果，可能已经生效。先重新读取确认，不要直接重试。",
              },
            ],
            elapsedMs: Date.now() - started,
          };
        }
        if ((error && error.rejected) || (reason === "AbortError" && controller.signal.aborted)) {
          // Timed out mid-gate: the action did **not** run, and the model must be
          // told that, not handed a success it can report to the user.
          return { status: "rejected", reason: (error && error.reason) || "busy", elapsedMs: Date.now() - started };
        }
        return {
          status: "error",
          reason: reason || "EXECUTION_FAILED",
          parts: [{ text: clip(String((error && error.message) || error), 600) }],
          elapsedMs: Date.now() - started,
        };
      } finally {
        clearTimeout(expire);
      }
    },
  };
}
