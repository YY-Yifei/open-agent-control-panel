/**
 * The panel's caller, driven against a **real** aaas server.
 *
 * `client.test.mjs` covers the protocol with a hand-written socket, which is fast
 * and lets it assert on exact frames. This file exists for the thing that a fake
 * socket cannot prove: that the framing we write is the framing a real server
 * reads. Two implementations of one protocol is the standing cost of putting the
 * caller in the page, and this is the check that keeps them honest.
 *
 * The Python side (`tests/test_agent_live.py`) starts `aaas.Server` on an
 * ephemeral port, passes the URL in argv, and asserts this script exits 0.
 */

import process from "node:process";

import { AgentClient } from "../../src/oacp/web/agent/client.js";
import { createCatalog } from "../../src/oacp/web/agent/catalog.js";

const url = process.argv[2];
if (!url) {
  console.error("usage: node live_client.mjs ws://host:port");
  process.exit(2);
}

const failures = [];
function check(condition, message) {
  if (!condition) failures.push(message);
}

const ui = {
  calls: [],
  setTheme(theme) {
    ui.calls.push(`theme:${theme}`);
  },
  setLang(lang) {
    ui.calls.push(`lang:${lang}`);
  },
  async notify(payload) {
    ui.calls.push(`notify:${payload.title}`);
  },
  toast(text) {
    ui.calls.push(`toast:${text}`);
  },
  open() {},
  setFilter() {},
  focus() {},
  draft() {},
  copy() {},
  scrollTo() {},
  download() {},
  async confirm() {
    return true;
  },
  clientInfo() {
    return {};
  },
};

const api = async () => ({});

const catalog = createCatalog({ api, ui, policy: "all" });

let finished;
const done = new Promise((resolve) => {
  finished = resolve;
});

let turnStatus = null;
let turnText = "";
const diagnostics = [];
const invoked = [];

const client = new AgentClient({
  url,
  instructions: "live test",
  handler: catalog,
  callbacks: {
    onStatus: ({ state }) => {
      if (state === "offline") finished();
    },
    onDiagnostic: (entry) => diagnostics.push(entry),
    onCall: (entry) => invoked.push(entry.name),
    onTurn: (turn) => {
      turnStatus = turn.status;
      turnText = turn.text;
      finished();
    },
  },
});

const timer = setTimeout(() => {
  failures.push("timeout: no turn.completed within 20s");
  finished();
}, 20_000);

try {
  const session = await client.connect();
  check(typeof session.sessionId === "string" && session.sessionId.length > 0, "sessionId must be a string");
  check(session.historyMode === "stateless", `historyMode should be stateless, got ${session.historyMode}`);
  check(session.features.includes("functions"), "features must include functions");
  check(session.limits && typeof session.limits.maxFrameBytes === "number", "limits.maxFrameBytes must be a number");

  const declared = await client.declare(catalog.declarations);
  check(
    declared.functions.length === catalog.names.length,
    `server accepted ${declared.functions.length} of ${catalog.names.length} functions`,
  );

  await client.send("把主题换成深色", [
    { role: "user", parts: [{ text: "早上好" }] },
    { role: "agent", parts: [{ text: "早上好，需要什么？" }] },
  ]);
  await done;
} catch (error) {
  failures.push(`threw: ${error && error.message}`);
  finished();
} finally {
  clearTimeout(timer);
  client.close();
}

check(turnStatus === "completed", `turn should complete, got ${turnStatus}`);
check(turnText.includes("theme ok"), `the answer should carry the function result, got ${JSON.stringify(turnText)}`);
check(ui.calls.includes("theme:dark"), `ui.setTheme("dark") should have run, got ${JSON.stringify(ui.calls)}`);
check(invoked.includes("ui_theme"), `the call should be reported to the UI, got ${JSON.stringify(invoked)}`);
check(diagnostics.length === 0, `no protocol diagnostics expected, got ${JSON.stringify(diagnostics)}`);

if (failures.length) {
  for (const failure of failures) console.error(`FAIL ${failure}`);
  process.exit(1);
}

console.log("ok");
process.exit(0);
