/**
 * 待定气泡：agent 正在干活时那一行字。
 *
 * A2A 的 `status.message` 是「我正在干什么」，不是回答。把它当正文追加进对话
 * 流，一个每步都报一句的 agent 就会把聊天刷成一串「正在读取 index.html」——
 * 那是进度，属于这里，不属于消息列表。
 *
 * 为什么单独一个模块：这段判据是纯函数，能在 node 里测；而 `app.js` 一 import
 * 就要摸 `document`。留在 app.js 里，「进度不能变成气泡」这条规矩就测不住，
 * 下一次重构又会悄悄把它写回消息列表。
 *
 * 气泡有两处渲染：整段列表重建时拼 HTML（`pendingNode`），轮询时只改那一行
 * （`fillPending`）。两处都从 `readProgress` 取同一对「显示什么 / 全文是什么」，
 * 于是它们不会各自长出一套判据。
 */

import { esc } from "./html.js";
// 截断的代理对保护只有一份（preview.js）：状态行没有理由再写一遍 0xd800 判据。
import { truncateText } from "./preview.js";

/** 状态行是一行字，不是正文：再长的进度也只留这么点，其余交给 title。 */
export const MAX_DETAIL = 160;

/** 压成一行：换行会把待定气泡撑成一块，而它每一拍都要换一次。 */
export function oneLine(value) {
  return String(value ?? "")
    .replace(/\s+/g, " ")
    .trim();
}

/** 一行且限长；超出的用省略号收尾。传进来之前已经 `oneLine` 过。 */
function clipLine(line) {
  if (line.length <= MAX_DETAIL) return line;
  return `${truncateText(line, MAX_DETAIL - 1).text}…`;
}

/**
 * 这个气泡要显示的两样东西，一次算完。
 *
 * `text` 是那一行（单行、限长），`full` 是截断前的全文（给 title，也用来判断
 * 「有没有进度」）。没有进度时两者都是空串。
 */
export function readProgress(progress) {
  const full = progress && typeof progress === "object" ? oneLine(progress.text) : "";
  return { text: full ? clipLine(full) : "", full };
}

/**
 * 这个会话的进度指纹：只在轮询里比「有没有变」，不参与显示。
 *
 * 两样都比：显示那行变了当然要重画；只有**全文**的尾巴变了也得重画，否则
 * title 永远停在旧句子。真正的播报噪声由 `fillPending` 挡 —— 显示文字没变时
 * 它不碰文本节点。
 */
export function progressKey(session) {
  if (!session || !session.progress) return "";
  const { text, full } = readProgress(session.progress);
  return `${text}\u0000${full}`;
}

/**
 * 一整块待定气泡的 HTML。
 *
 * 转义直接从 `html.js` 取，不由调用方注入：这段 HTML 是**这里**拼的，安全性就
 * 不能取决于每个调用方都记得传对函数。
 *
 * 不带 `role="status"`：`#chat` 本身已经是 `aria-live="polite"`，再套一层 live
 * region 会让 NVDA / JAWS 把同一句进度播报两遍（或者干脆漏一遍）。
 */
export function pendingNode(progress, fallback) {
  const { text, full } = readProgress(progress);
  // title 只要有进度就挂上：真正的省略常常来自 CSS（一行放不下时由浏览器截断），
  // 那时的全文 JS 根本没截过 —— 只按「JS 截过没有」决定有无 title，会让人看不到
  // 被浏览器吃掉的那半句。
  const title = full ? ` title="${esc(full)}"` : "";
  return `<div class="msg pending"><div class="pending"${title}>${esc(text || fallback)}</div></div>`;
}

/**
 * 把已经存在的那行字更新成当前进度。
 *
 * 不重挂节点：`#chat` 是 live region，换掉节点等于让读屏软件重新读一块新内容。
 * 显示文字没变时连 `textContent` 都不碰 —— 赋一个相同的值同样会换掉文本节点，
 * 读屏软件会再念一遍。变的只有 title 时，就只改 title。
 */
export function fillPending(node, progress, fallback) {
  const { text, full } = readProgress(progress);
  const next = text || fallback;
  if (node.textContent !== next) node.textContent = next;
  if (full) {
    if (node.getAttribute("title") !== full) node.setAttribute("title", full);
  } else if (node.hasAttribute("title")) {
    node.removeAttribute("title");
  }
}
