/* Open Agent Control Panel — vanilla ES module front end.
 * No build step, no framework: hash routing, fetch, and template strings.
 */

const API = "/api/panel";
const TOKEN_KEY = "oacp.panelToken";
const MANAGE_KEY = "oacp.manageTokens";

const state = {
  q: "",
  summary: null,
  route: { name: "agents", id: null },
  lastAgentsJson: "",
  lastTasksJson: "",
  refreshTimer: null,
};

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
  return date.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function relTime(value) {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function stateLabel(value) {
  return String(value || "unknown").replace(/-/g, " ");
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
  const detail = payload?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => item?.msg || JSON.stringify(item)).join("; ");
  }
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
  if (token) headers["X-Panel-Token"] = token;

  const response = await fetch(path, init);
  if (response.status === 401 && !options.noAuthPrompt) {
    openTokenDialog();
    throw new ApiError(401, "A panel token is required for this action.");
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
  $("#search").placeholder = options.searchPlaceholder || "Search agents…";
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
    setChrome("Task", "A single A2A task and its event log", { search: false, register: false, nav: "tasks" });
    await renderTaskDetail(id, view);
    return;
  }
  if (name === "tasks") {
    setChrome("Tasks", "Every task this panel submitted", { nav: "tasks", searchPlaceholder: "Search tasks…" });
    await renderTasks(view);
    return;
  }
  if (name === "agents" && id) {
    setChrome("Agent", "Agent record, endpoints and recent tasks", { search: false, register: false, nav: "agents" });
    await renderAgentDetail(id, view);
    return;
  }
  setChrome("Agents", "Agents registered through the discover protocol", { nav: "agents" });
  await renderAgents(view);
}

/* ------------------------------------------------------------------- layout */

function errorPanel(error) {
  return `<div class="alert">${esc(error.message || String(error))}</div>`;
}

function skeleton(message = "Loading…") {
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
      banner.innerHTML = `<strong>Local mode</strong><span>Bound to a loopback address, so <code>http</code> and private card URLs are accepted and A2A calls may target private hosts. This deployment does not claim ADP Core compliance — start with <code>--strict</code> to restore the https-only checks.</span>`;
    }
  }
}

function summaryCards(summary) {
  const agents = summary.agents || {};
  const tasks = summary.tasks || {};
  const active = tasks.active ?? 0;
  return `<div class="stats-grid">
    <div class="stat"><div class="label">Agents</div><div class="value">${agents.total ?? 0}</div>
      <div class="hint">${agents.ok ?? 0} ok · ${agents.down ?? 0} down</div></div>
    <div class="stat"><div class="label">Tasks</div><div class="value">${tasks.total ?? 0}</div>
      <div class="hint">${active} in flight</div></div>
    <div class="stat"><div class="label">Probing</div><div class="value">${summary.probe?.enabled ? "on" : "off"}</div>
      <div class="hint">every ${summary.probe?.interval ?? "—"}s</div></div>
    <div class="stat"><div class="label">Card mode</div><div class="value">${esc(summary.cardValidation || "relaxed")}</div>
      <div class="hint">${summary.allowPrivateAgents ? "private targets allowed" : "public targets only"}</div></div>
  </div>`;
}

/* ------------------------------------------------------------------- agents */

function agentCard(agent) {
  const tags = (agent.tags || []).slice(0, 8)
    .map((tag) => `<span class="chip plain">${esc(tag)}</span>`).join("");
  return `<article class="agent-card" data-agent="${esc(agent.id)}">
    <header>
      <div>
        <h3><a href="#/agents/${esc(agent.id)}">${esc(agent.name || "Unnamed agent")}</a></h3>
        <div class="url">${esc(agent.url)}</div>
      </div>
      ${badge(agent.status)}
    </header>
    <p class="desc">${esc(agent.description || "No description provided.")}</p>
    ${tags ? `<div class="chips">${tags}</div>` : ""}
    <div class="agent-meta">
      <span><b>${agent.taskCount ?? 0}</b> tasks</span>
      ${agent.activeTaskCount ? `<span><b>${agent.activeTaskCount}</b> active</span>` : ""}
      <span>last seen ${esc(relTime(agent.lastOkAt))}</span>
      ${agent.failures ? `<span style="color:var(--warn)">${agent.failures} failed probes</span>` : ""}
    </div>
    <footer>
      <button class="btn sm" data-act="send" data-id="${esc(agent.id)}">Send task</button>
      <a class="btn sm ghost" href="#/agents/${esc(agent.id)}">Details</a>
      <span class="spacer"></span>
      <span class="muted small">${(agent.skills || []).length} skills</span>
    </footer>
  </article>`;
}

async function renderAgents(view) {
  view.innerHTML = state.summary ? summaryCards(state.summary) + skeleton("Loading agents…") : skeleton("Loading agents…");
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
        ? emptyState("No agent matches that search", `Nothing registered matches “${state.q}”.`)
        : emptyState(
          "No agents yet",
          "Register an agent by its AgentCard URL. The card must be reachable over public HTTPS — the registry fetches it itself.",
          `<button class="btn primary" data-act="register">Register the first agent</button>`,
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
  view.innerHTML = skeleton("Loading agent…");
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
    : `<p class="muted small">The card declares no skills.</p>`;

  const interfaceRows = interfaces.length
    ? interfaces.map((item) => `
        <div class="row" style="justify-content:space-between;align-items:flex-start">
          <div>
            <div class="mono">${esc(item.url)}</div>
            <div class="muted small">${esc(item.protocolBinding || "JSONRPC")}${item.protocolVersion ? ` · ${esc(item.protocolVersion)}` : ""}</div>
          </div>
          ${endpoint && endpoint.url === item.url ? `<span class="chip teal">selected</span>` : ""}
        </div>`).join("")
    : `<p class="muted small">No usable A2A endpoint in this card.</p>`;

  const taskRows = tasks.length
    ? `<div class="table-wrap"><table><thead><tr>
         <th>Task</th><th>State</th><th>Updated</th><th></th></tr></thead><tbody>
         ${tasks.map((task) => `<tr class="clickable" data-task="${esc(task.id)}">
           <td><div class="cell-title">${esc(task.title)}</div>
             <div class="cell-sub mono">${esc(shortId(task.remoteTaskId || task.id))}</div></td>
           <td>${badge(task.state)}</td>
           <td class="cell-sub">${esc(fmtTime(task.updatedAt))}</td>
           <td><div class="cell-actions"><a class="btn sm ghost" href="#/tasks/${esc(task.id)}">Open</a></div></td>
         </tr>`).join("")}
         </tbody></table></div>`
    : `<p class="muted small">No tasks have been submitted to this agent yet.</p>`;

  view.innerHTML = `
    <div class="detail-head">
      <div>
        <h2>${esc(card.name || agent.url)} ${badge(agent.status)}</h2>
        <div class="url">${esc(agent.url)}</div>
        <div class="muted small" style="margin-top:6px">
          id <span class="mono">${esc(agent.id)}</span> · first seen ${esc(fmtTime(agent.firstSeenAt))}
          · last ok ${esc(fmtTime(agent.lastOkAt))}
        </div>
      </div>
      <div class="detail-actions">
        <button class="btn primary" data-act="send" data-id="${esc(agent.id)}">Send task</button>
        <button class="btn" data-act="refresh-agent" data-id="${esc(agent.id)}">Re-fetch card</button>
        <button class="btn danger" data-act="delete-agent" data-id="${esc(agent.id)}">Unregister</button>
      </div>
    </div>

    <div class="stats-grid">
      <div class="stat"><div class="label">Tasks</div><div class="value">${stats.taskCount ?? 0}</div>
        <div class="hint">${stats.activeTaskCount ?? 0} active</div></div>
      <div class="stat"><div class="label">Probe failures</div><div class="value">${agent.failures ?? 0}</div>
        <div class="hint">down at 3 in a row</div></div>
      <div class="stat"><div class="label">Skills</div><div class="value">${skills.length}</div>
        <div class="hint">${tags.length} tags</div></div>
      <div class="stat"><div class="label">Card</div><div class="value" style="font-size:14px;word-break:break-all">${esc(shortId(agent.cardHash))}</div>
        <div class="hint">changed ${agent.changedAt ? esc(relTime(agent.changedAt)) : "never"}</div></div>
    </div>

    <div class="split">
      <div class="panel">
        <h3>Endpoints</h3>
        <div class="stack">${interfaceRows}</div>
        <h3 style="margin-top:18px">Description</h3>
        <p class="muted" style="margin:0">${esc(card.description || "No description provided.")}</p>
        ${tags.length ? `<div class="chips" style="margin-top:10px">${tags.map((tag) => `<span class="chip plain">${esc(tag)}</span>`).join("")}</div>` : ""}
      </div>
      <div class="panel">
        <h3>Record</h3>
        <dl class="kv">
          ${kvRow("Status", badge(agent.status))}
          ${kvRow("First seen", esc(fmtTime(agent.firstSeenAt)))}
          ${kvRow("Checked", esc(fmtTime(agent.checkedAt)))}
          ${kvRow("Last ok", esc(fmtTime(agent.lastOkAt)))}
          ${kvRow("Changed", agent.changedAt ? esc(fmtTime(agent.changedAt)) : "<span class='muted'>never</span>")}
          ${kvRow("Previous hash", agent.prevHash ? `<span class="mono">${esc(shortId(agent.prevHash))}</span>` : "<span class='muted'>—</span>")}
          ${kvRow("Version", esc(card.version || "—"))}
        </dl>
      </div>
    </div>

    <div class="panel">
      <h3>Skills</h3>
      ${skillsHtml}
    </div>

    <div class="panel">
      <h3>Recent tasks</h3>
      ${taskRows}
    </div>

    <div class="panel">
      <h3>Raw AgentCard</h3>
      <details>
        <summary class="muted small">Show the JSON exactly as fetched</summary>
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
    <td><div class="cell-actions"><a class="btn sm ghost" href="#/tasks/${esc(task.id)}">Open</a></div></td>
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
        <option value="">All states</option>
        ${["active", "submitted", "working", "input-required", "completed", "failed", "canceled", "replied", "error"]
          .map((value) => `<option value="${value}" ${state.taskState === value ? "selected" : ""}>${stateLabel(value)}</option>`).join("")}
      </select>
    </div>`;

    const body = data.items.length
      ? `<div class="table-wrap"><table><thead><tr>
          <th>Task</th><th>Agent</th><th>State</th><th>Created</th><th>Updated</th><th></th>
         </tr></thead><tbody>${data.items.map(taskRow).join("")}</tbody></table></div>`
      : emptyState("No tasks yet", "Open an agent and send it a message to create the first one.");

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
  view.innerHTML = skeleton("Loading task…");
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
          agent <a href="#/agents/${esc(task.agentId)}"><strong>${esc(task.agentName)}</strong></a>
          · local id <span class="mono">${esc(task.id)}</span>
          ${task.remoteTaskId ? `· remote id <span class="mono">${esc(task.remoteTaskId)}</span>` : ""}
        </div>
        <div class="muted small">endpoint <span class="mono">${esc(task.endpoint || "—")}</span></div>
      </div>
      <div class="detail-actions">
        <button class="btn" data-act="refresh-task" data-id="${esc(task.id)}">Refresh from agent</button>
        <button class="btn" data-act="cancel-task" data-id="${esc(task.id)}" ${task.remoteTaskId && active ? "" : "disabled"}>Cancel</button>
        <button class="btn danger" data-act="delete-task" data-id="${esc(task.id)}">Forget</button>
      </div>
    </div>

    ${task.error ? `<div class="alert"><strong>Last error:</strong> ${esc(task.error)}</div>` : ""}

    <div class="panel">
      <h3>Prompt</h3>
      <div class="message-box">${esc(task.inputText)}</div>
    </div>

    <div class="panel">
      <h3>Latest agent output</h3>
      ${task.text
        ? `<div class="message-box">${esc(task.text)}</div>`
        : `<div class="message-box empty">The agent has not produced text yet.</div>`}
      <div class="muted small" style="margin-top:8px">
        ${task.artifactCount ? `${task.artifactCount} artifact(s)` : "no artifacts"} · updated ${esc(fmtTime(task.updatedAt))}
        · synced ${esc(task.syncedAt ? fmtTime(task.syncedAt) : "never")}
      </div>
    </div>

    <div class="panel">
      <h3>Follow-up message</h3>
      <form id="follow-form" class="stack">
        <div class="field" style="margin:0">
          <textarea id="follow-text" placeholder="Send another user message into this task/context…"></textarea>
          <span class="hint">Sent with <span class="mono">taskId</span> and <span class="mono">contextId</span>, so the agent can continue the same conversation.</span>
        </div>
        <div class="row" style="justify-content:flex-end">
          <button class="btn primary" type="submit">Send message</button>
        </div>
      </form>
    </div>

    <div class="split">
      <div class="panel">
        <h3>Event log</h3>
        ${events.length ? `<ul class="timeline">${events.map(eventLine).join("")}</ul>` : `<p class="muted small">No events recorded.</p>`}
      </div>
      <div class="panel">
        <h3>Details</h3>
        <dl class="kv">
          ${kvRow("Local id", `<span class="mono">${esc(task.id)}</span>`)}
          ${kvRow("Remote id", task.remoteTaskId ? `<span class="mono">${esc(task.remoteTaskId)}</span>` : "<span class='muted'>—</span>")}
          ${kvRow("Context", task.contextId ? `<span class="mono">${esc(task.contextId)}</span>` : "<span class='muted'>—</span>")}
          ${kvRow("Created", esc(fmtTime(task.createdAt)))}
          ${kvRow("Updated", esc(fmtTime(task.updatedAt)))}
          ${kvRow("Messages", String(task.messageCount ?? 0))}
          ${kvRow("Endpoint", `<span class="mono">${esc(task.endpoint || "—")}</span>`)}
        </dl>
      </div>
    </div>

    <div class="panel">
      <h3>Raw payloads</h3>
      <details>
        <summary class="muted small">Request</summary>
        <pre class="json" style="margin-top:12px">${esc(JSON.stringify(task.request ?? {}, null, 2))}</pre>
      </details>
      <details style="margin-top:10px">
        <summary class="muted small">Latest A2A response</summary>
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
        toast("Message sent", "ok");
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
    <h2>Register an agent</h2>
    <p class="sub">The registry fetches the AgentCard itself. The URL must be public HTTPS and
      end in a valid A2A card — registration proves only that this URL serves this card right now.</p>
    <div id="register-error"></div>
    <form id="register-form">
      <div class="field">
        <label for="register-url">AgentCard URL</label>
        <input id="register-url" type="url" required spellcheck="false"
               placeholder="https://agent.example.com/.well-known/agent-card.json" />
        <span class="hint">Also accepted: the agent's base URL, if it serves the card at the well-known path.</span>
      </div>
      <footer>
        <button type="button" class="btn ghost" data-act="close">Cancel</button>
        <button type="submit" class="btn primary">Fetch and register</button>
      </footer>
    </form>`);

  $("#register-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = $("#register-url").value.trim();
    const errorBox = $("#register-error");
    errorBox.innerHTML = "";
    const submit = event.target.querySelector("button[type=submit]");
    submit.disabled = true;
    submit.textContent = "Fetching…";
    try {
      const result = await api("/v1/agents", { method: "POST", body: { url }, noAuthPrompt: true });
      if (result.manageToken) {
        rememberManageToken(result.id, result.manageToken);
        toast("Registered. The manage token was saved in this browser.", "ok");
      } else {
        toast("This agent was already registered.", "ok");
      }
      closeModal();
      navigate(`#/agents/${result.id}`);
      route();
    } catch (error) {
      const problems = error.payload?.detail?.problems;
      errorBox.innerHTML = `<div class="alert error-box">
        <strong>${esc(error.message)}</strong>
        ${Array.isArray(problems) ? `<ul style="margin:8px 0 0;padding-left:18px">${problems.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}
      </div>`;
      submit.disabled = false;
      submit.textContent = "Fetch and register";
    }
  });
}

function openSendDialog(agentId, agentName) {
  openModal(`
    <h2>Send a task</h2>
    <p class="sub">This calls A2A <span class="mono">message/send</span> on <strong>${esc(agentName || agentId)}</strong>
      and records the resulting task.</p>
    <div id="send-error"></div>
    <form id="send-form">
      <div class="field">
        <label for="send-text">Message</label>
        <textarea id="send-text" required placeholder="Describe the task for the agent…"></textarea>
      </div>
      <div class="field">
        <label for="send-context">Context id <span class="muted">(optional)</span></label>
        <input id="send-context" spellcheck="false" placeholder="continue an existing conversation" />
      </div>
      <footer>
        <button type="button" class="btn ghost" data-act="close">Cancel</button>
        <button type="submit" class="btn primary">Send</button>
      </footer>
    </form>`);

  $("#send-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = $("#send-text").value.trim();
    if (!text) return;
    const contextId = $("#send-context").value.trim();
    const submit = event.target.querySelector("button[type=submit]");
    submit.disabled = true;
    submit.textContent = "Sending…";
    try {
      const result = await api(`${API}/agents/${encodeURIComponent(agentId)}/tasks`, {
        method: "POST",
        body: { text, contextId: contextId || null },
      });
      closeModal();
      if (result.task.error) {
        toast(`The agent call failed: ${result.task.error}`, "error");
      } else {
        toast("Task submitted", "ok");
      }
      navigate(`#/tasks/${result.task.id}`);
      route();
    } catch (error) {
      $("#send-error").innerHTML = `<div class="alert error-box">${esc(error.message)}</div>`;
      submit.disabled = false;
      submit.textContent = "Send";
    }
  });
}

function openDeleteAgentDialog(agentId, agentName) {
  const stored = manageTokens()[agentId] || "";
  openModal(`
    <h2>Unregister this agent?</h2>
    <p class="sub">Deletion is immediate and leaves no tombstone. The same URL can be registered
      again by anyone, and the next registrant gets a new manage token.</p>
    <div id="delete-error"></div>
    <form id="delete-form">
      <div class="field">
        <label for="delete-token">Manage token</label>
        <input id="delete-token" spellcheck="false" value="${esc(stored)}"
               placeholder="the token returned at first registration" />
        <span class="hint">${stored
          ? "Found in this browser's local storage."
          : "Only the first registrant has it. Without it the entry cannot be removed through this panel."}</span>
      </div>
      <footer>
        <button type="button" class="btn ghost" data-act="close">Cancel</button>
        <button type="submit" class="btn danger">Unregister</button>
      </footer>
    </form>`);

  $("#delete-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = $("#delete-token").value.trim();
    if (!token) return;
    try {
      await api(`${API}/agents/${encodeURIComponent(agentId)}`, {
        method: "DELETE",
        body: { token },
        noAuthPrompt: true,
      });
      forgetManageToken(agentId);
      closeModal();
      toast(`${agentName || "Agent"} unregistered`, "ok");
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
    <h2 id="token-modal">Panel token</h2>
    <p class="sub">This panel is configured with an access token. It is stored only in this browser.</p>
    <form id="token-form">
      <div class="field">
        <label for="token-input">Token</label>
        <input id="token-input" type="password" autocomplete="off" spellcheck="false" />
      </div>
      <footer>
        <button type="button" class="btn ghost" data-act="close">Cancel</button>
        <button type="submit" class="btn primary">Save</button>
      </footer>
    </form>`);

  $("#token-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const token = $("#token-input").value.trim();
    if (!token) return;
    localStorage.setItem(TOKEN_KEY, token);
    closeModal();
    toast("Token saved", "ok");
    route();
  });
}

/* ------------------------------------------------------------------ actions */

async function refreshAgent(id) {
  try {
    await api(`${API}/agents/${encodeURIComponent(id)}/refresh`, { method: "POST" });
    toast("Card re-fetched", "ok");
    route();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function refreshTask(id) {
  try {
    const result = await api(`${API}/tasks/${encodeURIComponent(id)}/refresh`, { method: "POST" });
    if (result.task.error) toast(result.task.error, "error");
    else toast(`State: ${result.task.state}`, "ok");
    route();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function cancelTask(id) {
  try {
    const result = await api(`${API}/tasks/${encodeURIComponent(id)}/cancel`, { method: "POST" });
    if (result.task.error) toast(result.task.error, "error");
    else toast(`Cancellation requested (state: ${result.task.state})`, "ok");
    route();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function deleteTask(id) {
  try {
    await api(`${API}/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("Task removed from the ledger", "ok");
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
  route();
});

// Also run when the module is evaluated after DOMContentLoaded already fired.
if (document.readyState !== "loading") {
  route();
}
