/* 聊天气泡用的极轻量 Markdown —— 够用为止，不做完整合规。
 *
 * 三条硬约束，改之前先读一遍：
 *   1. **原文只用来判断结构，转义只发生在拼 HTML 的那一刻。** 块级扫描跑在
 *      原始文本上（否则 `>` 早已是 `&gt;`，引用整段识别不出来）；每个片段进
 *      inline() 时先整体转义，再匹配行内语法。
 *   2. 链接与图片地址走 safeUrl() **白名单**；拦不住的写法里 `javascript&#58;`
 *      这种靠「只解一层实体」才安全的边角，白名单一次解决。
 *   3. 只支持聊天里真用得上的：标题、列表、代码块、行内代码、粗体、斜体、
 *      删除线、链接、图片、引用、分隔线、表格。嵌套列表、脚注、内联 HTML
 *      不做 —— 要么用不上，要么等于自己开一个 XSS 口子。
 *
 * 输出是字符串，由调用方写进 innerHTML；本项目只有本文件拼 HTML。
 */

const ESCAPES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

/** 把任意文本变成可以安全落进 HTML 的文本。 */
export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
}

/**
 * 地址白名单：只放行 http(s)、mailto 与站内相对地址。
 *
 * Args:
 *   raw: 原始地址（尚未转义）。
 *
 * Returns:
 *   可以放进 href/src 的字符串；不合法返回 null。
 */
function safeUrl(raw) {
  const value = String(raw ?? "").trim();
  if (!value) return null;
  // 浏览器解析 URL 前会吃掉 tab / 换行，`java<TAB>script:` 仍是 javascript:，
  // 所以判断必须建立在剥掉控制字符后的形态上。
  const probe = value.replace(/[\u0000-\u0020]+/g, "");
  if (/^(https?|mailto):/i.test(probe)) return value;
  // 协议相对地址（//evil.example/x）会继承页面的 scheme，等于放行外链。
  if (/^\/\//.test(probe)) return null;
  if (/^[/#?]/.test(probe)) return value;
  if (/^\.{1,2}\//.test(probe)) return value;
  return null;
}

function image(alt, src) {
  const url = safeUrl(src);
  if (!url) return escapeHtml(alt || src);
  // 外链图片不当第一等公民：不懒加载会一次拉一堆，不带 referrer 政策会把
  // 面板地址告诉对方服务器。
  return `<img src="${escapeHtml(url)}" alt="${escapeHtml(alt)}" loading="lazy" referrerpolicy="no-referrer">`;
}

function anchor(label, href) {
  const url = safeUrl(href);
  if (!url || !label.trim()) {
    // 危险协议 → 退化成普通文字：链接不成立，内容也不丢。
    return label.trim() ? label : escapeHtml(href);
  }
  const external = /^https?:\/\//i.test(url);
  const attrs = external ? ' target="_blank" rel="noopener noreferrer"' : "";
  return `<a href="${escapeHtml(url)}"${attrs}>${label}</a>`;
}

const isDivider = (line) => /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line);
const isHeading = (line) => /^ {0,3}#{1,6}\s+/.test(line);
const isFenceSlot = (line) => /^\u0000\d+\u0000$/.test(line.trim());
const isBullet = (line) => /^\s*[-*+]\s+/.test(line);
const isOrdered = (line) => /^\s*\d{1,9}[.)]\s+/.test(line);
const isQuote = (line) => /^ {0,3}>/.test(line);
const isTableDivider = (line) =>
  /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line) && line.includes("-");

/** 这一行会不会打断当前段落（是某个块级语法的开头）。 */
function startsBlock(line, next) {
  return (
    !line.trim() ||
    isFenceSlot(line) ||
    isHeading(line) ||
    isDivider(line) ||
    isBullet(line) ||
    isOrdered(line) ||
    isQuote(line) ||
    (line.includes("|") && next !== undefined && isTableDivider(next))
  );
}

function splitRow(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function tableAlign(cell) {
  const left = cell.startsWith(":");
  const right = cell.endsWith(":");
  if (left && right) return "center";
  if (right) return "right";
  if (left) return "left";
  return "";
}

/**
 * 把一段 Markdown 渲染成 HTML。
 *
 * Args:
 *   source: 原始文本；任何内容都按不可信输入处理。
 *
 * Returns:
 *   可直接写入 innerHTML 的 HTML 字符串；空输入返回空串。
 */
export function renderMarkdown(source) {
  const raw = String(source ?? "").replace(/\u0000/g, ""); // \u0000 是占位符专用字符
  if (!raw.trim()) return "";

  const stash = [];
  const keep = (html) => `\u0000${stash.push(html) - 1}\u0000`;
  const restore = (html) => html.replace(/\u0000(\d+)\u0000/g, (_all, index) => stash[Number(index)] ?? "");

  // 行内：入参是**原文**片段，第一步就整体转义，之后只匹配语法。
  // URL 允许一层平衡括号，`javascript:alert(1)` 这类才不会被截成半截。
  function inline(text) {
    let out = escapeHtml(text);

    // 行内代码先摘走：里面的 * 和 _ 是字面量，不该被后两条规则当成强调。
    out = out.replace(/`([^`\n]+)`/g, (_all, code) => keep(`<code>${code}</code>`));

    out = out.replace(
      /!\[([^\]]*)\]\(\s*((?:[^()\s]|\([^()\s]*\))+)\s*\)/g,
      (_all, alt, src) => image(alt, src),
    );
    out = out.replace(
      /\[([^\]]*)\]\(\s*((?:[^()\s]|\([^()\s]*\))+)\s*\)/g,
      (_all, label, href) => anchor(label, href),
    );

    out = out.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?!\w)/g, "$1<strong>$2</strong>");
    out = out.replace(/\*(?=\S)([\s\S]*?\S)\*/g, "<em>$1</em>");
    out = out.replace(/(^|[^\w])_(?=\S)([\s\S]*?\S)_(?!\w)/g, "$1<em>$2</em>");
    out = out.replace(/~~(?=\S)([\s\S]*?\S)~~/g, "<del>$1</del>");

    return out;
  }

  // 块级：跑在原文上（围栏代码块已经被换成占位符）。
  function blocks(text) {
    const lines = text.split("\n");
    const out = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      if (!line.trim()) {
        i += 1;
        continue;
      }

      // 围栏代码块：早已渲染好放在 stash 里，这里只把占位符摆回去。
      if (isFenceSlot(line)) {
        out.push(line.trim());
        i += 1;
        continue;
      }

      const heading = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
      if (heading) {
        const level = heading[1].length;
        out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
        i += 1;
        continue;
      }

      if (isDivider(line)) {
        out.push("<hr>");
        i += 1;
        continue;
      }

      // 表格：本行是表头，下一行是对齐说明。
      if (line.includes("|") && i + 1 < lines.length && isTableDivider(lines[i + 1])) {
        const head = splitRow(line);
        const align = splitRow(lines[i + 1]).map(tableAlign);
        i += 2;
        const body = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
          body.push(splitRow(lines[i]));
          i += 1;
        }
        const cell = (tag, value, index) => {
          const mode = align[index];
          const cls = mode ? ` class="ta-${mode}"` : "";
          return `<${tag}${cls}>${inline(value)}</${tag}>`;
        };
        const headHtml = head.map((value, index) => cell("th", value, index)).join("");
        const bodyHtml = body
          .map((row) => `<tr>${row.map((value, index) => cell("td", value, index)).join("")}</tr>`)
          .join("");
        out.push(
          `<table><thead><tr>${headHtml}</tr></thead>${bodyHtml ? `<tbody>${bodyHtml}</tbody>` : ""}</table>`,
        );
        continue;
      }

      if (isQuote(line)) {
        const inner = [];
        while (i < lines.length && isQuote(lines[i])) {
          inner.push(lines[i].replace(/^ {0,3}>\s?/, ""));
          i += 1;
        }
        out.push(`<blockquote>${blocks(inner.join("\n"))}</blockquote>`);
        continue;
      }

      if (isBullet(line) || isOrdered(line)) {
        const ordered = isOrdered(line);
        const pattern = ordered ? /^\s*\d{1,9}[.)]\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
        const first = ordered ? Number.parseInt(lines[i].trim(), 10) : 1;
        const items = [];
        while (i < lines.length && (ordered ? isOrdered(lines[i]) : isBullet(lines[i]))) {
          const match = pattern.exec(lines[i]);
          if (!match) break;
          items.push(`<li>${inline(match[1])}</li>`);
          i += 1;
        }
        const tag = ordered ? "ol" : "ul";
        const attr = ordered && Number.isFinite(first) && first !== 1 ? ` start="${first}"` : "";
        out.push(`<${tag}${attr}>${items.join("")}</${tag}>`);
        continue;
      }

      // 段落：吃到空行或下一个块开头为止；单个换行保留成 <br>（聊天语义，
      // 也是 GFM 的做法 —— 模型很爱用单换行分行）。
      const paragraph = [];
      while (i < lines.length && !startsBlock(lines[i], lines[i + 1])) {
        paragraph.push(inline(lines[i]));
        i += 1;
      }
      if (!paragraph.length) {
        // 理论到不了这里（startsBlock 已拦住其余情形），守一手防死循环。
        paragraph.push(inline(lines[i]));
        i += 1;
      }
      out.push(`<p>${paragraph.join("<br>")}</p>`);
    }

    return out.join("");
  }

  // 围栏代码块：先整块摘出来。未闭合的围栏也照样吃掉 —— agent 输出被截断时，
  // 把剩下的 ``` 原样吐在气泡里比少一个闭合更难看。
  const fenced = raw.replace(
    /^ {0,3}```([^\n`]*)\n?([\s\S]*?)(?:^ {0,3}```[ \t]*$|$)/gm,
    (_all, info, body) => {
      const language = String(info).trim().split(/\s+/)[0];
      const cls = language ? ` class="lang-${escapeHtml(language)}"` : "";
      const text = body.replace(/\n$/, "");
      if (!text && !language) return "";
      return `\n${keep(`<pre><code${cls}>${escapeHtml(text)}</code></pre>`)}\n`;
    },
  );

  return restore(blocks(fenced)).trim();
}
