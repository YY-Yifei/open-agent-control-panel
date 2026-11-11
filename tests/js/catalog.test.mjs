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
    clientInfo: () => ({ viewport: "800x600" }),
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
  assert.equal(catalog.declarations.length, 23);
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
  assert.deepEqual(sizes, { read: 17, act: 20, all: 22, delegate: 23 });
  // The delegate capability is the one that can make one agent drive another.
  const all = createCatalog({ api, ui, policy: "all" }).names;
  assert.ok(!all.includes("panel_send_message"));
  assert.ok(POLICY_TIERS.read.length === 1);
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
