/**
 * HTML 转义：页面只有这一份。
 *
 * 单独一个模块，是因为调用方不止一处 —— `app.js` 的模板字符串、`md.js` 的
 * Markdown 渲染、`progress.js` 的待定气泡，以及 `progress.test.mjs` 里
 * 「进度里的标记不能变成 HTML」那条断言。任何一处再抄一份都等于没测：抄本永远
 * 不会跟着原件一起坏掉。它不碰 DOM，所以能在 node 里直接跑。
 */

// 表放模块级：每次转义都重建一个对象，等于给每一段正文加一次垃圾。
const ESCAPES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
}
