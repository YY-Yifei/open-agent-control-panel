/**
 * The pending bubble's rules.
 *
 * One specific thing is under test: an agent's progress (A2A `status.message`)
 * only replaces the "working…" line — it is never appended as a message. It
 * used to be folded into the answer, so every step added a bubble that was not
 * an answer. Hence these assertions about replacement, truncation and escaping,
 * not about layout.
 *
 * Run with `node --test "tests/js/*.test.mjs"` (`tests/test_js.py` does exactly
 * that, so `pytest` — and CI — execute this file too).
 */

import test from "node:test";
import assert from "node:assert/strict";

// 转义从页面用的那一份 import，不在这里抄一遍：抄本永远不会跟着原件一起坏。
import { esc } from "../../src/oacp/web/html.js";
import {
  MAX_DETAIL,
  fillPending,
  oneLine,
  pendingNode,
  progressKey,
  readProgress,
} from "../../src/oacp/web/progress.js";

/** 一粒假节点：记下每次 textContent 赋值，够钉住「没变就别碰」。 */
function stubNode() {
  const attrs = {};
  let text = "";
  let writes = 0;
  return {
    get textContent() {
      return text;
    },
    set textContent(next) {
      writes += 1;
      text = next;
    },
    get writes() {
      return writes;
    },
    getAttribute(name) {
      return name in attrs ? attrs[name] : null;
    },
    setAttribute(name, value) {
      attrs[name] = value;
    },
    hasAttribute(name) {
      return name in attrs;
    },
    removeAttribute(name) {
      delete attrs[name];
    },
    attrs,
  };
}

/** 有没有落单的代理对半个 —— 浏览器会把它画成 �。 */
function hasLoneSurrogate(text) {
  for (let i = 0; i < text.length; i += 1) {
    const code = text.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = text.charCodeAt(i + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return true;
      i += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      return true;
    }
  }
  return false;
}

test("no progress means no text, and the working line stays", () => {
  assert.deepEqual(readProgress(null), { text: "", full: "" });
  assert.deepEqual(readProgress({}), { text: "", full: "" });
  assert.deepEqual(readProgress({ text: "   " }), { text: "", full: "" });

  const html = pendingNode(null, "正在处理…");
  assert.ok(html.includes("正在处理…"), html);
  assert.ok(!html.includes("title="), html);
});

test("progress replaces the line instead of being appended to it", () => {
  const progress = readProgress({ text: "读取 index.html" });
  assert.equal(progress.text, "读取 index.html");

  const html = pendingNode({ text: "读取 index.html" }, "正在处理…");
  assert.ok(html.includes("读取 index.html"), html);
  assert.ok(!html.includes("正在处理"), html);
});

test("it is one line, and only the boundary is truncated", () => {
  assert.equal(oneLine("  正在\n读取\tindex.html "), "正在 读取 index.html");

  const exact = "读".repeat(MAX_DETAIL);
  assert.deepEqual(readProgress({ text: exact }), { text: exact, full: exact });

  const shown = readProgress({ text: "读".repeat(MAX_DETAIL + 10) }).text;
  assert.equal(shown.length, MAX_DETAIL);
  assert.equal(shown.slice(0, -1), "读".repeat(MAX_DETAIL - 1));
  assert.ok(shown.endsWith("…"));
});

test("truncation never splits a surrogate pair", () => {
  for (let lead = 0; lead < 4; lead += 1) {
    const { text } = readProgress({ text: `${"读".repeat(lead)}${"🔍".repeat(200)}` });
    assert.ok(!hasLoneSurrogate(text), `lone surrogate in ${JSON.stringify(text.slice(-4))}`);
  }
});

test("markup in progress is escaped, never parsed", () => {
  const html = pendingNode({ text: "<img src=x onerror=alert(1)>" }, "正在处理…");
  assert.ok(!html.includes("<img"), html);
  assert.ok(html.includes("&lt;img"), html);
  // #chat 本身已是 aria-live="polite"：再套一层 live region 会让同一句播报两遍。
  assert.ok(!html.includes("aria-live"), html);
  assert.ok(!html.includes('role="status"'), html);
});

test("the shared escaper pins all five mappings", () => {
  assert.equal(esc(`&<>"'`), "&amp;&lt;&gt;&quot;&#39;");
  // 已经是实体的文本不能被「还原」：& 必须先被转义，否则 `&lt;` 会变成真标签。
  assert.equal(esc("&lt;"), "&amp;lt;");
});

test("a title is attached whenever there is progress, so CSS ellipsis has the full text", () => {
  const short = pendingNode({ text: "读取 index.html" }, "正在处理…");
  assert.ok(short.includes('title="读取 index.html"'), short);

  const tail = "x".repeat(200);
  const clipped = pendingNode({ text: `读 "${tail}" onmouseover=alert(1)` }, "正在处理…");
  assert.ok(clipped.includes("&quot;"), clipped);
  assert.ok(!clipped.includes('" onmouseover'), "a quote must not break out of the title");
  assert.ok(clipped.includes(tail), "the title must carry the text past the clip");
});

test("fillPending replaces the line instead of appending to it", () => {
  const node = stubNode();
  node.textContent = "正在处理…";

  fillPending(node, { text: "读取 index.html" }, "正在处理…");
  fillPending(node, { text: "写入 styles.css" }, "正在处理…");

  assert.equal(node.textContent, "写入 styles.css", "两次进度只能留下一句");
  assert.equal(node.getAttribute("title"), "写入 styles.css");
});

test("fillPending drops the title when the progress goes away", () => {
  const node = stubNode();
  fillPending(node, { text: "读取 index.html" }, "正在处理…");
  fillPending(node, null, "正在处理…");

  assert.equal(node.textContent, "正在处理…");
  assert.equal(node.getAttribute("title"), null, "没有进度就没有 title");
});

test("fillPending refreshes a title-only change without touching the text", () => {
  const node = stubNode();
  const head = "读".repeat(MAX_DETAIL);

  fillPending(node, { text: `${head}A` }, "正在处理…");
  const before = node.writes;
  fillPending(node, { text: `${head}B` }, "正在处理…");

  // 显示那行一个字都没变：不碰 textContent，live region 就不会再念一遍。
  assert.equal(node.writes, before, "an unchanged line must not be re-assigned");
  assert.ok(node.getAttribute("title").endsWith("B"), "但 title 要跟着全文更新");
});

test("progressKey covers what is on screen, and what the tooltip says", () => {
  assert.equal(progressKey(null), "");
  assert.equal(progressKey({}), "");
  assert.notEqual(
    progressKey({ progress: { text: "a" } }),
    progressKey({ progress: { text: "b" } }),
  );
  // 只差空白的一拍画出来一模一样，不该触发重画。
  assert.equal(
    progressKey({ progress: { text: "reading  index.html\n" } }),
    progressKey({ progress: { text: "reading index.html" } }),
  );
  // 前 160 个字符一样、只有尾巴不同：显示那行不变，但 title 变了，得重画。
  assert.notEqual(
    progressKey({ progress: { text: `${"读".repeat(MAX_DETAIL)}A` } }),
    progressKey({ progress: { text: `${"读".repeat(MAX_DETAIL)}B` } }),
  );
});
