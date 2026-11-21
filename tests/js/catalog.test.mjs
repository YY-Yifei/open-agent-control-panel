import test from "node:test";
import assert from "node:assert/strict";

import { createCatalog, validateArguments, POLICY_TIERS } from "../../src/oacp/web/agent/catalog.js";

const NAME = /^[a-z][a-z0-9_]{0,63}$/;

function fakeHost(overrides = {}) {
  const calls = [];
  const api = async (path, init) => {
    calls.push({ path, init });
    if (overrides.api) return overrides.api(path, init);
    return { items: [], counts: { agents: 0, sessions: 0, tasks: 0 } };
  };
  const ui = {
    setTheme: (theme) => calls.push({ setTheme: theme }),
    setLang: (lang) => calls.push({ setLang: lang }),
    toast: (text) => calls.push({ toast: text }),
    draft: (text) => calls.push({ draft: text }),
    modal: ({ title }) => calls.push({ modal: title }),
    notify: async () => calls.push({ notify: true }),
    open: async ({ kind, id }) => {
      calls.push({ open: `${kind}:${id}` });
      return id;
    },
    download: async (id) => {
      calls.push({ download: id });
      return `file-${id}.png`;
    },
    attach: async (id) => {
      calls.push({ attach: id });
      return `file-${id}.png`;
    },
    preview: async (id) => {
      calls.push({ preview: id });
      return `file-${id}.txt`;
    },
    clientInfo: () => ({ viewport: "800x600" }),
    // 布局接缝。默认返回一个「什么都正常」的快照；`overrides.layout` 可以换成
    // 一个夹过值的版本，`null` 表示这个宿主根本没有布局（其它宿主确实没有）。
    layout: (patch) => {
      calls.push({ layout: patch });
      if (overrides.layout === null) return null;
      if (overrides.layout) return overrides.layout(patch);
      return {
        side: 268,
        drawer: 400,
        reading: "comfortable",
        readingWidth: 760,
        docked: true,
        adjusted: [],
        wanted: { side: 268, drawer: 400, reading: "comfortable", docked: true },
      };
    },
    confirm: async ({ title }) => {
      calls.push({ confirm: title });
      return overrides.approve;
    },
  };
  return { api, ui, calls };
}

test("every declaration is a legal name with an object schema, and none repeat", () => {
  const { api, ui } = fakeHost();
  const catalog = createCatalog({ api, ui, policy: "delegate" });
  assert.equal(catalog.declarations.length, 28);
  const names = catalog.declarations.map((item) => item.name);
  assert.equal(new Set(names).size, names.length, "duplicate function name");
  for (const declaration of catalog.declarations) {
    assert.match(declaration.name, NAME);
    assert.equal(declaration.parameters.type, "object", declaration.name);
    assert.ok(Number.isInteger(declaration.timeoutMs) && declaration.timeoutMs >= 1, declaration.name);
    assert.ok(declaration.description.length > 0, declaration.name);
  }
});

test("the policy decides what is even declared", () => {
  const { api, ui } = fakeHost();
  const sizes = {};
  for (const policy of ["read", "act", "all", "delegate"]) {
    sizes[policy] = createCatalog({ api, ui, policy }).declarations.length;
  }
  assert.deepEqual(sizes, { read: 21, act: 25, all: 27, delegate: 28 });
  // Inspection is `read` by construction, not by policy: the server opens the
  // file with mode=ro + query_only, so a write fails inside SQLite.
  const read = createCatalog({ api, ui, policy: "read" }).names;
  assert.ok(read.includes("panel_db_schema"));
  assert.ok(read.includes("panel_db_query"));
  // The delegate capability is the one that can make one agent drive another.
  const all = createCatalog({ api, ui, policy: "all" }).names;
  assert.ok(!all.includes("panel_send_message"));
  assert.ok(POLICY_TIERS.read.length === 1);
});

test("ui_layout reports what the page actually did, including what it clamped", async () => {
  const { api, ui } = fakeHost({
    layout: () => ({
      side: 480,
      drawer: 340,
      reading: "wide",
      readingWidth: 1100,
      docked: false,
      adjusted: ["drawer 400px → 340px", "docked → overlay (the window is too narrow to dock)"],
      wanted: { side: 480, drawer: 400, reading: "wide", docked: true },
    }),
  });
  const catalog = createCatalog({ api, ui, policy: "read" });

  // 布局是 read 档：`--agent-policy read` 下也要拿得到（它不动数据）。
  assert.ok(catalog.names.includes("ui_layout"));

  const result = await catalog.execute({ name: "ui_layout", arguments: {}, deadlineMs: 5000 });
  assert.equal(result.status, "ok");
  const text = result.parts[0].text;
  assert.match(text, /reading=wide/);
  assert.match(text, /sidebar=480px/);
  assert.match(text, /overlay/);
  // 夹过的值必须说出来：模型要 400 拿到 340，若回它「drawer=400」，
  // 它下一句话就是错的。
  assert.match(text, /adjusted: drawer 400px → 340px/);
});

test("ui_layout: out-of-range widths and unknown presets never reach the body", async () => {
  const { api, ui, calls } = fakeHost();
  const catalog = createCatalog({ api, ui, policy: "read" });
  // schema 的 min/max 就是 layout.js 的边界（不是第二份数字），所以这里同时
  // 在钉「两边是同一个来源」：模型不可能请求一个控制器收不到的宽度。
  for (const args of [{ drawer: 700 }, { drawer: 300 }, { sidebar: 100 }, { sidebar: 481 }, { reading: "narrow" }]) {
    const result = await catalog.execute({ name: "ui_layout", arguments: args, deadlineMs: 5000 });
    assert.equal(result.reason, "ARGUMENTS_INVALID", JSON.stringify(args));
  }
  assert.equal(calls.find((item) => item.layout), undefined, "被拒的调用不该碰过布局");
});

test("ui_layout says a 'full' column fills the area instead of 'nullpx'", async () => {
  const { api, ui } = fakeHost({
    layout: () => ({
      side: 268,
      drawer: 400,
      reading: "full",
      readingWidth: null,
      docked: true,
      adjusted: [],
      wanted: { side: 268, drawer: 400, reading: "full", docked: true },
    }),
  });
  const catalog = createCatalog({ api, ui, policy: "read" });
  const result = await catalog.execute({ name: "ui_layout", arguments: {}, deadlineMs: 5000 });
  assert.equal(result.status, "ok");
  assert.match(result.parts[0].text, /fills the main area/);
  // `nullpx` 就是这条分支曾经会吐出来的东西。
  assert.doesNotMatch(result.parts[0].text, /nullpx/);
});

test("ui_layout refuses reset-with-fields and set-without-fields", async () => {
  const { api, ui, calls } = fakeHost();
  const catalog = createCatalog({ api, ui, policy: "read" });

  // 复位是「全部还原」，带字段一定是模型理解错了：静默丢掉字段等于告诉它
  // 「复位 + reading=wide」两件事都做了。
  const withFields = await catalog.execute({
    name: "ui_layout",
    arguments: { action: "reset", reading: "wide" },
    deadlineMs: 5000,
  });
  assert.equal(withFields.status, "error");
  assert.match(withFields.parts[0].text, /reset ignores the other fields/);

  // set 但一个字段都没给：别的宿主只会回现状，不会抛 —— 所以判在这里。
  const empty = await catalog.execute({ name: "ui_layout", arguments: { action: "set" }, deadlineMs: 5000 });
  assert.equal(empty.status, "error");
  assert.match(empty.parts[0].text, /needs at least one of/);

  assert.equal(calls.find((item) => item.layout), undefined, "两次拒绝都不该碰过布局");
});

test("ui_layout: fields without an action mean set, not a silent no-op", async () => {
  const { api, ui, calls } = fakeHost();
  const catalog = createCatalog({ api, ui, policy: "read" });
  const result = await catalog.execute({
    name: "ui_layout",
    arguments: { reading: "wide" },
    deadlineMs: 5000,
  });
  assert.equal(result.status, "ok");
  const call = calls.find((item) => item.layout);
  assert.equal(call.layout.action, "set", "只带字段就该当成 set，否则模型什么也改不了");
  assert.equal(call.layout.reading, "wide");
});

test("ui_layout refuses to look like it honoured a field it was told to ignore", async () => {
  const { api, ui, calls } = fakeHost();
  const catalog = createCatalog({ api, ui, policy: "read" });
  const result = await catalog.execute({
    name: "ui_layout",
    arguments: { action: "get", reading: "wide" },
    deadlineMs: 5000,
  });
  assert.equal(result.status, "error");
  assert.match(result.parts[0].text, /action=get only reads/);
  assert.equal(calls.find((item) => item.layout), undefined, "报错了就不该已经动过布局");
});

test("ui_layout says the page has no layout instead of failing with a TypeError", async () => {
  const { api, ui } = fakeHost({ layout: null });
  const catalog = createCatalog({ api, ui, policy: "read" });
  const result = await catalog.execute({ name: "ui_layout", arguments: {}, deadlineMs: 5000 });
  assert.equal(result.status, "error");
  // 「这个页面没有布局」必须是**说人话**的文案。`reason` 在有错误文案时一律是
  // EXECUTION_FAILED（这个目录的统一约定），所以判据在文案上：不能是
  // TypeError 那句 `ui.layout is not a function`。
  assert.match(result.parts[0].text, /this page has no layout to read/);
  assert.doesNotMatch(result.parts[0].text, /is not a function/);
});

test("ui_preview is a read: looking changes nothing", async () => {
  const { api, ui, calls } = fakeHost();
  const catalog = createCatalog({ api, ui, policy: "read" });
  // Opening a lightbox is the one way a model can *show* something without
  // touching it: the read policy keeps it, unlike ui_attach.
  assert.ok(catalog.names.includes("ui_preview"));

  const result = await catalog.execute({
    name: "ui_preview",
    arguments: { attachment_id: 7 },
    deadlineMs: 5000,
  });
  assert.deepEqual(calls.find((call) => call.preview), { preview: 7 });
  assert.equal(result.status, "ok");
  assert.match(result.parts[0].text, /opened file-7\.txt in the preview/);
});

test("ui_preview says it failed when the page refuses", async () => {
  const { api } = fakeHost();
  const ui = {
    preview: async () => {
      throw new Error("that attachment is not in the conversation that is open");
    },
  };
  const catalog = createCatalog({ api, ui, policy: "read" });
  const result = await catalog.execute({
    name: "ui_preview",
    arguments: { attachment_id: 3 },
    deadlineMs: 5000,
  });
  // 失败必须如实回到模型手上：报成「已打开」会让人去找一个根本不存在的视图。
  assert.equal(result.status, "error");
  assert.match(result.parts[0].text, /not in the conversation/);
});

test("ui_attach stays out of the read policy — staging a file is an act", () => {
  const { api, ui } = fakeHost();
  // The whole point of a separate name: `--agent-policy read` promises the page
  // is read-only, and widening `ui_draft` with an attachment argument would have
  // broken that promise without changing a single declaration count.
  assert.ok(!createCatalog({ api, ui, policy: "read" }).names.includes("ui_attach"));
  assert.ok(createCatalog({ api, ui, policy: "act" }).names.includes("ui_attach"));
});

test("ui_draft says whether it wrote or appended", async () => {
  const { api, ui } = fakeHost();
  const appended = createCatalog({ api, ui: { ...ui, draft: () => "appended" }, policy: "read" });
  const fresh = createCatalog({ api, ui, policy: "read" });
  const wrote = await appended.execute({ name: "ui_draft", arguments: { text: "hi" }, deadlineMs: 5000 });
  const made = await fresh.execute({ name: "ui_draft", arguments: { text: "hi" }, deadlineMs: 5000 });
  assert.equal(wrote.status, "ok");
  assert.match(wrote.parts[0].text, /appended/);
  assert.equal(made.status, "ok");
  assert.match(made.parts[0].text, /draft written/);
});

test("ui_draft defaults to append and only replaces when asked", async () => {
  const seen = [];
  const { api, ui } = fakeHost();
  const catalog = createCatalog({
    api,
    ui: {
      ...ui,
      draft: (_text, mode) => {
        seen.push(mode);
        return "appended";
      },
    },
    policy: "read",
  });
  await catalog.execute({ name: "ui_draft", arguments: { text: "hi" }, deadlineMs: 5000 });
  // Omitting the argument must never clobber what the person typed.
  assert.deepEqual(seen, ["append"]);
  await catalog.execute({
    name: "ui_draft",
    arguments: { text: "hi", mode: "replace" },
    deadlineMs: 5000,
  });
  assert.deepEqual(seen, ["append", "replace"]);
});

test("ui_draft reports a replacement as a replacement", async () => {
  const { api, ui } = fakeHost();
  const catalog = createCatalog({
    api,
    ui: { ...ui, draft: (_text, mode) => (mode === "replace" ? "replaced" : "appended") },
    policy: "read",
  });
  const result = await catalog.execute({
    name: "ui_draft",
    arguments: { text: "short", mode: "replace" },
    deadlineMs: 5000,
  });
  assert.equal(result.status, "ok");
  assert.match(result.parts[0].text, /replaced/);
  assert.doesNotMatch(result.parts[0].text, /appended/);
});

test("ui_draft refuses a mode it does not define", async () => {
  const { api, ui } = fakeHost();
  const catalog = createCatalog({ api, ui, policy: "read" });
  const result = await catalog.execute({
    name: "ui_draft",
    arguments: { text: "hi", mode: "overwrite" },
    deadlineMs: 5000,
  });
  assert.equal(result.status, "error");
});

test("ui_attach passes the id through and reports that nothing was sent", async () => {
  const { api, ui, calls } = fakeHost();
  const catalog = createCatalog({ api, ui, policy: "act" });
  const result = await catalog.execute({ name: "ui_attach", arguments: { attachment_id: 7 }, deadlineMs: 5000 });
  assert.equal(result.status, "ok");
  assert.deepEqual(calls.filter((call) => call.attach !== undefined), [{ attach: 7 }]);
  assert.match(result.parts[0].text, /still has to send it/);
});

test("ui_attach refuses a non-positive id before it touches the page", async () => {
  const { api, ui, calls } = fakeHost();
  const catalog = createCatalog({ api, ui, policy: "act" });
  const result = await catalog.execute({ name: "ui_attach", arguments: { attachment_id: 0 }, deadlineMs: 5000 });
  assert.equal(result.status, "error");
  assert.equal(calls.filter((call) => call.attach !== undefined).length, 0);
});

test("unknown function, missing arguments, and bad arguments are three different answers", async () => {
  const { api, ui } = fakeHost();
  const catalog = createCatalog({ api, ui });

  assert.equal((await catalog.execute({ name: "nope", arguments: {} })).reason, "FUNCTION_UNKNOWN");
  assert.equal(
    (await catalog.execute({ name: "ui_theme", arguments: undefined })).reason,
    "ARGUMENTS_UNPARSABLE",
  );
  const invalid = await catalog.execute({ name: "ui_theme", arguments: { theme: "blue" } });
  assert.equal(invalid.status, "error");
  assert.equal(invalid.reason, "ARGUMENTS_INVALID");
  assert.match(invalid.parts[0].text, /theme/);
});

test("validation covers what the schemas actually use", () => {
  const schema = {
    type: "object",
    required: ["a"],
    properties: {
      a: { type: "string", enum: ["x", "y"], maxLength: 3 },
      b: { type: "integer", minimum: 1, maximum: 5 },
    },
  };
  assert.deepEqual(validateArguments(schema, { a: "x", b: 3 }), []);
  assert.equal(validateArguments(schema, {}).length, 1);
  assert.equal(validateArguments(schema, { a: "z" }).length, 1);
  assert.equal(validateArguments(schema, { a: "x", b: 9 }).length, 1);
  assert.equal(validateArguments(schema, { a: "x", b: 1.5 }).length, 1);
});

test("a successful call returns parts, an elapsed time, and the real effect", async () => {
  const { api, ui, calls } = fakeHost();
  const catalog = createCatalog({ api, ui });
  const result = await catalog.execute({ name: "ui_theme", arguments: { theme: "dark" }, deadlineMs: 5000 });
  assert.equal(result.status, "ok");
  assert.equal(result.parts[0].text, "theme=dark");
  assert.ok(calls.some((call) => call.setTheme === "dark"));
  assert.ok(Number.isInteger(result.elapsedMs));
});

test("a destructive call does nothing unless a human approves", async () => {
  // Refused: the answer must be `rejected`, and no DELETE may reach the API.
  {
    const { api, ui, calls } = fakeHost({ approve: false, api: async () => ({ session: { title: "t" } }) });
    const catalog = createCatalog({ api, ui, policy: "all" });
    const result = await catalog.execute({ name: "panel_delete_session", arguments: { id: "s_1" }, deadlineMs: 120000 });
    assert.equal(result.status, "rejected");
    assert.equal(result.reason, "denied");
    assert.ok(!calls.some((call) => call.init && call.init.method === "DELETE"));
  }
  // Approved: the dialog was shown, and then the delete happened exactly once.
  {
    const { api, ui, calls } = fakeHost({ approve: true, api: async (path, init) => (init && init.method === "DELETE" ? null : { session: { title: "t" } }) });
    const catalog = createCatalog({ api, ui, policy: "all" });
    const result = await catalog.execute({ name: "panel_delete_session", arguments: { id: "s_1" }, deadlineMs: 120000 });
    assert.equal(result.status, "ok");
    assert.ok(calls.some((call) => typeof call.confirm === "string"));
    assert.equal(calls.filter((call) => call.init && call.init.method === "DELETE").length, 1);
  }
});

test("a big result is cut, and says that it was cut", async () => {
  // Enough real messages to exceed the cap on their own. A long *title* would be
  // trimmed by the formatter and would prove nothing about the result budget.
  const messages = Array.from({ length: 40 }, (_, index) => ({
    id: index + 1,
    role: index % 2 ? "agent" : "user",
    text: "y".repeat(300),
    attachments: [],
  }));
  const { api, ui } = fakeHost({ api: async () => ({ session: { id: "s_1", title: "t", messages } }) });
  const catalog = createCatalog({ api, ui });
  const result = await catalog.execute({ name: "panel_get_session", arguments: { id: "s_1", limit: 60 } });
  assert.equal(result.status, "ok");
  const text = result.parts[0].text;
  assert.ok(text.length <= 4100, `expected a capped result, got ${text.length}`);
  assert.match(text, /not shown/);
});

test("a failing API call is an error the model can act on, not a crash", async () => {
  const { api, ui } = fakeHost({ api: async () => { throw new Error("boom"); } });
  const catalog = createCatalog({ api, ui });
  const result = await catalog.execute({ name: "panel_overview", arguments: {} });
  assert.equal(result.status, "error");
  assert.equal(result.parts[0].text, "boom");
});

test("panel_db_schema asks for the map, and scopes it to one table when asked", async () => {
  const { api, ui, calls } = fakeHost({ api: async () => ({ text: "agents (1 rows): id*" }) });
  const catalog = createCatalog({ api, ui });

  await catalog.execute({ name: "panel_db_schema", arguments: {}, deadlineMs: 5000 });
  await catalog.execute({ name: "panel_db_schema", arguments: { table: "tasks" }, deadlineMs: 5000 });

  assert.equal(calls[0].path, "/api/panel/db/schema");
  // A table name reaches the server verbatim, so it has to be encoded: the
  // schema allows any string up to 60 characters, not a known-safe alphabet.
  assert.equal(calls[1].path, "/api/panel/db/schema?table=tasks");

  // "tasks" survives encodeURIComponent unchanged, so asserting on it proves
  // nothing. This one would break out of the query string without the call.
  await catalog.execute({ name: "panel_db_schema", arguments: { table: "a b&c=d?" }, deadlineMs: 5000 });
  assert.equal(calls[2].path, "/api/panel/db/schema?table=a%20b%26c%3Dd%3F");
});

test("panel_db_query POSTs the statement and keeps the server's text intact", async () => {
  const { api, ui, calls } = fakeHost({
    api: async () => ({ text: "sql ok  2 rows  3ms\nstate  n\ncompleted  2", ok: true }),
  });
  const catalog = createCatalog({ api, ui });

  const result = await catalog.execute({
    name: "panel_db_query",
    arguments: { sql: "SELECT state, count(*) n FROM tasks GROUP BY 1" },
    deadlineMs: 5000,
  });

  assert.equal(calls[0].path, "/api/panel/db/query");
  assert.equal(calls[0].init.method, "POST");
  // No limit supplied -> no limit sent. The server owns the default (100); a
  // literal here as well would be a second source of truth that drifts silently.
  assert.deepEqual(calls[0].init.body, { sql: "SELECT state, count(*) n FROM tasks GROUP BY 1" });
  assert.equal(result.status, "ok");
  assert.match(result.parts[0].text, /^sql ok/);

  await catalog.execute({
    name: "panel_db_query",
    arguments: { sql: "SELECT 1", limit: 5 },
    deadlineMs: 5000,
  });
  assert.deepEqual(calls[1].init.body, { sql: "SELECT 1", limit: 5 });
});

test("an empty table name is refused instead of meaning 'every table'", async () => {
  // `""` is falsy, so without minLength it reached the body as "no table given" and
  // the caller got a bigger answer than it asked for — and no way to tell.
  const { api, ui } = fakeHost();
  const catalog = createCatalog({ api, ui });
  const result = await catalog.execute({
    name: "panel_db_schema",
    arguments: { table: "" },
    deadlineMs: 5000,
  });
  assert.equal(result.status, "error");
  assert.equal(result.reason, "ARGUMENTS_INVALID");
});

test("a schema that could not be produced fails, like a bad statement does", async () => {
  // The server reports `no such table: task` with ok:false. Surfacing it as a
  // success would let the model read a non-answer as a map.
  const { api, ui } = fakeHost({
    api: async () => ({ text: "no such table: task; did you mean tasks?", ok: false }),
  });
  const catalog = createCatalog({ api, ui });
  const result = await catalog.execute({
    name: "panel_db_schema",
    arguments: { table: "task" },
    deadlineMs: 5000,
  });
  // ARGUMENTS_INVALID is impossible here (the name is a legal string), so what
  // matters is that it is not reported as ok.
  assert.equal(result.status, "error");
  assert.equal(result.reason, "SCHEMA_FAILED");
  assert.match(result.parts[0].text, /did you mean tasks/);
});

test("a timeout is a timeout, not a syntax mistake", async () => {
  // Telling the model "SQL_FAILED" for a statement that only ran too long sends it
  // off rewriting correct SQL; the reason has to say which it was.
  const { api, ui } = fakeHost({
    api: async () => ({ text: "SQL_TIMEOUT: over 3000ms — narrow the window with WHERE", ok: false }),
  });
  const catalog = createCatalog({ api, ui });
  const result = await catalog.execute({
    name: "panel_db_query",
    arguments: { sql: "SELECT * FROM tasks" },
    deadlineMs: 5000,
  });
  assert.equal(result.status, "error");
  assert.equal(result.reason, "SQL_TIMEOUT");
});

test("a wrong statement fails, and its hint survives the error path", async () => {
  // The server answers 200 with ok:false so the hint is not mangled on the way
  // out; the catalog then re-raises it. Two things have to hold at once: the model
  // sees a failure (not a result it might report as an answer), and it still gets
  // the "did you mean" that lets it recover in the same turn.
  const { api, ui } = fakeHost({
    api: async () => ({
      text: "SQL_ERROR: no such column: agentname\nhint: did you mean agent_name (tasks)?",
      ok: false,
    }),
  });
  const catalog = createCatalog({ api, ui });

  const result = await catalog.execute({
    name: "panel_db_query",
    arguments: { sql: "SELECT agentname FROM tasks" },
    deadlineMs: 5000,
  });

  // A statement that failed is a failure: a flat `ok` would leave the operator
  // reading `· ok` in the log for a query that never ran, and the model without a
  // reason to retry.
  assert.equal(result.status, "error");
  assert.equal(result.reason, "SQL_FAILED");
  // The hint survives, because it is the whole point of the round trip.
  assert.match(result.parts[0].text, /no such column/);
  assert.match(result.parts[0].text, /did you mean agent_name/);
});

test("an unknown argument is refused instead of being silently ignored", async () => {
  // The schema says additionalProperties: false. Without enforcement a typo in an
  // optional argument ("colour" for "theme") would run the capability with its
  // default behaviour and report success the model never asked for.
  const { api, ui } = fakeHost({});
  const catalog = createCatalog({ api, ui });

  const result = await catalog.execute({
    name: "ui_theme",
    arguments: { theme: "dark", colour: "red" },
  });

  assert.equal(result.status, "error");
  assert.equal(result.reason, "ARGUMENTS_INVALID");
  assert.match(result.parts[0].text, /unexpected/);
});

test("a call that overruns its deadline is reported as rejected, not late ok", async () => {
  // A body that ignores the signal must not keep the invoke pending: the server
  // stopped waiting, and a later "ok" would be a success nobody asked for.
  const { ui } = fakeHost({});
  let land;
  const api = () => new Promise((resolve) => {
    land = resolve;
  });
  const catalog = createCatalog({ api, ui });

  const started = Date.now();
  const result = await catalog.execute({ name: "panel_list_agents", arguments: {}, deadlineMs: 200 });
  const elapsed = Date.now() - started;

  assert.equal(result.status, "rejected");
  assert.equal(result.reason, "busy");
  assert.ok(elapsed < 1500, `expected a prompt refusal, took ${elapsed}ms`);

  land({ items: [] }); // the flight lands after we stopped caring
});

test("a delete that overruns its deadline reports an unknown outcome, not 'did not run'", async () => {
  // The DELETE may already have reached the server. Answering `rejected` ("did not
  // run") hands the model a sentence it will repeat to the person: "nothing was
  // deleted" — while something was.
  let land;
  const api = async (path, init) => {
    if (init && init.method === "DELETE") {
      return new Promise((resolve) => {
        land = resolve;
      });
    }
    return { agent: { name: "Geo Agent" } };
  };
  // `approve: true` matters: without it the gate refuses and the call is
  // `rejected/denied`, which would test the wrong path entirely.
  const { ui } = fakeHost({ api, approve: true });
  const catalog = createCatalog({ api, ui, policy: "all" });

  const result = await catalog.execute({
    name: "panel_delete_agent",
    arguments: { id: "a_1" },
    deadlineMs: 300,
  });

  assert.equal(result.status, "error");
  assert.equal(result.reason, "OUTCOME_UNKNOWN");
  assert.match(result.parts[0].text, /可能已经生效/);

  land({}); // it lands after we stopped waiting
});

test("a delete whose confirmation was never answered is 'busy', not 'unknown'", async () => {
  // The mirror of the test above. Nothing was dispatched — the human never
  // answered the gate — so saying the delete "may already have taken effect"
  // would be as false as saying it ran, and would send the model off to verify
  // something that never happened.
  const api = async () => ({ agent: { name: "Geo Agent" } });
  const { ui } = fakeHost({ api, approve: true });
  ui.confirm = () => new Promise(() => {}); // never answered
  const catalog = createCatalog({ api, ui, policy: "all" });

  const result = await catalog.execute({
    name: "panel_delete_agent",
    arguments: { id: "a_1" },
    deadlineMs: 200,
  });

  assert.equal(result.status, "rejected");
  assert.equal(result.reason, "busy");
  assert.equal(result.parts, undefined, "a refusal carries no reassuring text");
});
