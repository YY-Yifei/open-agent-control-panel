/* The drawer's two pure rules, tested without a DOM.
 *
 * `transcriptBody()` — a model turn is Markdown, everything else is literal text,
 * the same decision the chat bubbles make. A drawer that renders `**bold**` as
 * stars, or that turns a person's `# heading` into an <h1>, is the bug this guards.
 * `historyForModel()` — what a stateless server is sent as context: recent and
 * bounded, with the two edges (one oversized message, a spent budget) pinned down.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const WEB = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "src", "oacp", "web");
const { transcriptBody, historyForModel, revisionOf } = await import(
  pathToFileURL(join(WEB, "agent", "panel.js")).href,
);

test("a model turn is Markdown, through the page's one renderer", () => {
  const body = transcriptBody("agent", "**3 个**在线\n\n| a | b |\n| --- | --- |\n| 1 | 2 |");
  assert.equal(typeof body.html, "string");
  assert.ok(body.html.includes("<strong>3 个</strong>"), body.html);
  assert.ok(body.html.includes("<table>"), body.html);
  assert.equal(body.text, undefined, "a rendered turn must not also be injected as text");
});

test("a model turn cannot smuggle HTML through the drawer", () => {
  const body = transcriptBody("agent", '<img src=x onerror="alert(1)"> [x](javascript:alert(1))');
  assert.ok(!body.html.includes("<img"), body.html);
  assert.ok(!body.html.includes("javascript:"), body.html);
  assert.ok(body.html.includes("&lt;img"), body.html);
});

test("what a person types stays exactly what they typed", () => {
  const typed = "# 标题\n**不是粗体** <b>标签</b>";
  const body = transcriptBody("user", typed);
  assert.equal(body.text, typed);
  assert.equal(body.html, undefined);
});

test("notes and errors are text too: their wording is not Markdown", () => {
  for (const role of ["note", "error"]) {
    const body = transcriptBody(role, "⚠ seq.gap");
    assert.equal(body.text, "⚠ seq.gap");
    assert.equal(body.html, undefined);
  }
});

test("empty and nullish text never becomes a rendered fragment", () => {
  assert.deepEqual(transcriptBody("agent", ""), { html: "" });
  assert.deepEqual(transcriptBody("agent", null), { html: "" });
  assert.deepEqual(transcriptBody("user", undefined), { text: "" });
});

test("history sends the newest turns and drops the oldest once the budget is spent", () => {
  const log = [
    { role: "user", text: "a".repeat(6) },
    { role: "agent", text: "b".repeat(6) },
    { role: "user", text: "c".repeat(6) },
  ];
  assert.deepEqual(historyForModel(log, { messages: 12, chars: 15 }), [
    { role: "agent", parts: [{ text: "b".repeat(6) }] },
    { role: "user", parts: [{ text: "c".repeat(6) }] },
  ]);
});

test("one message larger than the whole budget is cut to its tail, not dropped", () => {
  const log = [{ role: "user", text: `${"x".repeat(40)}TAIL` }];
  const sent = historyForModel(log, { messages: 12, chars: 10 });
  assert.deepEqual(sent, [{ role: "user", parts: [{ text: "xxxxxxTAIL" }] }]);
});

test("history honours the message cap even when everything would fit by size", () => {
  const log = Array.from({ length: 5 }, (_all, index) => ({ role: "user", text: `m${index}` }));
  const sent = historyForModel(log, { messages: 2, chars: 10_000 });
  assert.deepEqual(sent.map((item) => item.parts[0].text), ["m3", "m4"]);
});

test("history maps every non-user line to agent, so a note never becomes a user turn", () => {
  const sent = historyForModel(
    [
      { role: "note", text: "seq.gap" },
      { role: "user", text: "hi" },
    ],
    { messages: 12, chars: 100 },
  );
  assert.deepEqual(sent.map((item) => item.role), ["agent", "user"]);
});

test("a zero budget sends no history at all", () => {
  assert.deepEqual(historyForModel([{ role: "user", text: "x" }], { chars: 0 }), []);
});

test("a declaration revision is stable, and changes with the set or its order", () => {
  assert.equal(revisionOf(["ui_theme", "panel_overview"]), revisionOf(["ui_theme", "panel_overview"]));
  assert.notEqual(revisionOf(["ui_theme"]), revisionOf(["ui_theme", "panel_overview"]));
  assert.notEqual(revisionOf(["a", "b"]), revisionOf(["b", "a"]));
});

test("the tail-truncated branch normalizes the role too", () => {
  const sent = historyForModel([{ role: "note", text: "y".repeat(30) }], { chars: 5 });
  assert.deepEqual(sent, [{ role: "agent", parts: [{ text: "yyyyy" }] }]);
});
