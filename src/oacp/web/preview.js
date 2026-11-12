/**
 * 附件预览的判据：这东西是什么、能读多少、怎么读。
 *
 * 放在一个独立模块里，是因为它有两处用户：灯箱（渲染）与选文件（收发）。
 * 两边各写一份「什么算文本」，迟早会分叉出一条「选得进来、显示不出来」的缝。
 *
 * 三条纪律，全部来自这个仓库已有的教训：
 *
 * 1. **字节说了算**。扩展名与声明类型只是标签：一个改名叫 `.txt` 的 PNG 不能
 *    因为名字被当文本读，一个叫 `.png` 的日志也不能因为名字被拒绝。
 * 2. **文本只经 textContent 渲染**。本模块只负责把字节变成字符串，绝不产出
 *    HTML —— 拼接 HTML 的地方在 app.js，而那里必须用 textContent。
 * 3. **截断要说出来**。截断一个文件却假装完整，比拒绝它还糟。
 */

/** 一次预览最多读取多少个字符（约 5000 行）。 */
export const MAX_TEXT_CHARS = 200_000;

/**
 * 不属于文本的 C0 控制字符：除 tab / 换行 / 换页 / 回车 / ESC 之外的全部，
 * 加上 DEL。ESC 留着，因为日志里有 ANSI 颜色码，而浏览器在 <pre> 里只是把
 * 它原样画出来，不会解释。
 *
 * 与服务端 `media._BAD_CONTROLS` 同一组字符。客户端这道检查是**前置过滤**：
 * 目的是让「能选进来」与「能发出去」尽量重合，服务端仍是权威。
 */
const BAD_CONTROLS = /[\x00-\x08\x0b\x0e-\x1a\x1c-\x1f\x7f]/g;

/**
 * 前 64 KB 找 NUL 就判二进制 —— 这是「不是文本」最强的单一信号。
 *
 * 只查前缀而不是全量：一个 5 MB 的文件在这里做一次全扫描是白花的，
 * 而真实文本几乎不可能在 64 KB 之后才第一次出现 NUL。
 */
const NUL_WINDOW = 64 * 1024;

/**
 * 已知二进制 / 文档格式的 magic bytes。
 *
 * 为什么要显式列出来：PDF 的字节是**合法的 UTF-8 ASCII**（`%PDF-1.7` 开头），
 * 只按「能不能解码」判会把一份 PDF 当文本读进来，灯箱里就是满屏乱码源码。
 * ZIP 同理（docx / xlsx / pptx 都是 ZIP）。这里只用于**排除文本**，不用于
 * 放行任何类型 —— 放行由服务端嗅探决定。
 */
const BINARY_PREFIXES = [
  [0x25, 0x50, 0x44, 0x46, 0x2d], // %PDF-
  [0x50, 0x4b, 0x03, 0x04], // ZIP（含 docx / xlsx / pptx）
  [0x50, 0x4b, 0x05, 0x06], // 空 ZIP
  [0x1f, 0x8b], // gzip
  [0xd0, 0xcf, 0x11, 0xe0], // OLE（旧版 Office）
  [0x7f, 0x45, 0x4c, 0x46], // ELF
  [0x00, 0x61, 0x73, 0x6d], // wasm
];

/** 去掉 `; charset=…` 之类的参数、去空白、转小写 —— 全模块只此一处。 */
function normalizeMime(value) {
  return String(value || "")
    .split(";", 1)[0]
    .trim()
    .toLowerCase();
}

/**
 * 一个附件属于哪一类。只看服务端存下的 `mime` 与 `skipped`。
 *
 * `missing` 是独立的一类，不是「file 的一种」：没有字节的附件点开只能看到
 * 一个空层，所以它根本不该可点（见 app.js 的 attachmentNode）。
 *
 * @param {{mime?: string, skipped?: string|null}|null|undefined} file
 * @returns {"missing"|"image"|"text"|"pdf"|"file"}
 */
export function classify(file) {
  if (!file || file.skipped) return "missing";
  const mime = normalizeMime(file.mime);
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("text/")) return "text";
  if (mime === "application/pdf") return "pdf";
  return "file";
}

function toBytes(input) {
  if (input instanceof Uint8Array) return input;
  if (input instanceof ArrayBuffer) return new Uint8Array(input);
  if (ArrayBuffer.isView(input)) {
    return new Uint8Array(input.buffer, input.byteOffset, input.byteLength);
  }
  // 静默返回空数组会把「传错了类型」变成「这文件是空的」：错误离现场很远，
  // 而症状（一个空附件、一份被判成二进制的文本）完全指不出真正的原因。
  throw new TypeError("toBytes expects a Uint8Array, ArrayBuffer or ArrayBufferView");
}

function startsWith(bytes, prefix) {
  if (bytes.length < prefix.length) return false;
  for (let index = 0; index < prefix.length; index += 1) {
    if (bytes[index] !== prefix[index]) return false;
  }
  return true;
}

/**
 * 是不是一个已知的二进制 / 文档格式（只用来把文本排除掉）。
 *
 * @param {Uint8Array|ArrayBuffer|ArrayBufferView} input
 * @returns {boolean}
 */
export function looksBinary(input) {
  const bytes = toBytes(input);
  return BINARY_PREFIXES.some((prefix) => startsWith(bytes, prefix));
}

/**
 * 把字节按 UTF-8 解成文本；不是文本就返回 `null`。
 *
 * 五个拒绝条件，与服务端的 `media.sniff_text` 对齐。客户端是**前置过滤**，
 * 服务端才是权威 —— 这里放过去的东西，那边还会再判一次。两边判据要贴着走：
 * 客户端宽松一次，就是让人选进来、写到一半才在发送时撞 415。
 *
 *   - 空字节（服务端连空文件都不收，收下它没有任何可预览的东西）；
 *   - 前 64 KB 有 NUL；
 *   - 是已知二进制 / 文档格式（PDF、ZIP…）；
 *   - 不是合法 UTF-8（UTF-16 / GBK 一律不支持：转码就是改内容）；
 *   - 控制字符密度超过千分之一。
 *
 * 注意：合法 UTF-8 且无 NUL 的 HTML / SVG **会**被当作文本收下 —— 这是有意的。
 * 它们以 `text/plain` 存储、以 `attachment` + `nosniff` 回吐、以 textContent
 * 渲染，所以结果是「能读到源码」，不是「会被执行」。
 *
 * @param {Uint8Array|ArrayBuffer|ArrayBufferView} input
 * @returns {string|null}
 */
export function decodeText(input) {
  const bytes = toBytes(input);
  // 空文件不是文本：服务端在 `prepare` 里就以 attachment_bad_data 拒掉它，
  // 前端放过去只会在发送时才暴露。
  if (!bytes.length) return null;
  const head = bytes.subarray(0, Math.min(bytes.length, NUL_WINDOW));
  for (let index = 0; index < head.length; index += 1) {
    if (head[index] === 0) return null;
  }
  if (looksBinary(bytes)) return null;
  let text;
  try {
    // fatal: true 关掉「解码失败就塞替换符」：带 U+FFFD 的预览会把
    // 「这不是文本」说成「这文件里有怪字」。
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
  // 密度而不是「有没有」：一个 tab 是文本，一整段 C0 是二进制碰巧解码成功。
  //
  // 分母按**码点**算，不按 UTF-16 码元：服务端 `sniff_text` 用的是 `len(text)`，
  // 那是码点。星平面字符（emoji）在 JS 里占两个码元，直接用 text.length 会把
  // 门槛放宽一倍 —— 正好是「客户端放过去、发送时撞 415」的那条缝。
  // fatal 解码不会产出孤立代理，所以减去高位代理数就是码点数。
  const points = countCodePoints(text);
  const bad = (text.match(BAD_CONTROLS) || []).length;
  return bad * 1000 <= points ? text : null;
}

/** 码点数：星平面字符（emoji）在 JS 里占两个码元，数「字符」时不能算两次。 */
function countCodePoints(text) {
  let points = 0;
  for (let index = 0; index < text.length; index += 1) {
    points += 1;
    const code = text.charCodeAt(index);
    // 只有后面确实是低位代理时才一起带走。`truncateText` 是导出的、收任意
    // 字符串：孤立的高位代理不能吞掉紧接着的那个正常字符，否则「共 N 字符」
    // 会少报一个。
    const next = index + 1 < text.length ? text.charCodeAt(index + 1) : 0;
    if (code >= 0xd800 && code <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) index += 1;
  }
  return points;
}

function countLines(text) {
  if (!text) return 0;
  let count = 1;
  for (let index = 0; index < text.length; index += 1) {
    if (text.charCodeAt(index) === 10) count += 1;
  }
  return count;
}

/**
 * 允许预览多少字符，并把「截断了」这件事说清楚。
 *
 * 换行统一成 `\n` 再数：`\r\n` 在 `<pre>` 里会被渲染成两行，行数就对不上。
 * 截断点若落在一个代理对的中间，退回一个码元 —— 半个 emoji 会显示成 U+FFFD，
 * 而 U+FFFD 恰好会让人以为「文件坏了」。
 *
 * @param {string} text
 * @param {number} [max]
 * @returns {{text: string, truncated: boolean, total: number, lines: number, shownLines: number}}
 */
export function truncateText(text, max = MAX_TEXT_CHARS) {
  const source = String(text === null || text === undefined ? "" : text);
  const whole = source.replace(/\r\n?/g, "\n");
  const limit = Math.max(1, Math.floor(Number(max) || MAX_TEXT_CHARS));
  const lines = countLines(whole);
  // 预算与切点按码元算（那是内存），「共 N 字符」按码点算（否则 500 个 emoji
  // 会被读成 1000 字符）。而且它量的是**归一化前**的源文本：归一化把 `\r\n`
  // 折成一个 `\n`，若拿归一化后的串去数，每个 CRLF 行都会少算一个字符 ——
  // 一个千行的 Windows 文件能少报一千。
  const total = countCodePoints(source);
  if (whole.length <= limit) {
    return { text: whole, truncated: false, total, lines, shownLines: lines };
  }
  let cut = limit;
  const code = whole.charCodeAt(cut - 1);
  // 0xD800–0xDBFF 是代理对的前半个：切在这里会让它变成孤立代理。
  //
  // 退回一个码元是常规解；但 max 为 1 时退回会把 cut 变成 0 —— 一个空预览却
  // 标着「已截断」。只有在**退不了**的时候才保留整对（宁可超出一个码元）。
  // 不能无条件 Math.max(2, …)：`"a😀"` 截 2 个码元时，正确的切点是 1（显示 "a"），
  // 强行留住整对反而切出半个代理。
  if (code >= 0xd800 && code <= 0xdbff) cut = cut > 1 ? cut - 1 : 2;
  const slice = whole.slice(0, cut);
  return {
    text: slice,
    truncated: true,
    total,
    lines,
    shownLines: countLines(slice),
  };
}

/**
 * 字节 → 标准 base64（无 data-URL 前缀、单行）。
 *
 * 分块是必须的：`String.fromCharCode(...bytes)` 在 5 MB 上会撑爆调用栈
 * （参数个数有上限），逐字节拼接在同样体积下又慢到肉眼可见。
 *
 * @param {Uint8Array|ArrayBuffer|ArrayBufferView} input
 * @returns {string}
 */
export function bytesToBase64(input) {
  const bytes = toBytes(input);
  const step = 0x8000;
  let binary = "";
  for (let index = 0; index < bytes.length; index += step) {
    binary += String.fromCharCode.apply(null, bytes.subarray(index, index + step));
  }
  return btoa(binary);
}

/**
 * 体积文案。上限来自服务端配置，可能是 500 KB：一律说成 "1 MB" 会和它
 * 自己的报错互相矛盾。
 *
 * @param {number} bytes
 * @returns {string}
 */
export function formatBytes(bytes) {
  const value = Math.max(0, Number(bytes) || 0);
  // 门槛要落在「四舍五入之后仍然是这个单位」的地方：1048575 B 走 KB 分支会
  // 得到 `Math.round(1023.999) = 1024 KB` —— 一句自相矛盾的文案，而这句话会
  // 出现在附件条、灯箱和服务端的上限报错里。
  if (value >= 1024 * 1024 - 512) return `${_magnitude(value / (1024 * 1024))} MB`;
  const kb = Math.round(value / 1024);
  if (kb >= 1) return `${kb} KB`;
  return `${Math.round(value)} B`;
}

function _magnitude(size) {
  // 整数就写整数，其余保留一位：四舍五入会把 1.6 MB 的上限说成 "2 MB"，
  // 于是拒收 1.8 MB 的报错自己打自己。
  const rounded = Math.round(size * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

/**
 * MIME → 一句人话，给文件卡用。不认识的原样回，别假装知道。
 *
 * @param {string} mime
 * @returns {string}
 */
export function typeLabel(mime) {
  const value = normalizeMime(mime);
  const known = {
    "application/pdf": "PDF",
    "text/plain": "TXT",
    "text/markdown": "Markdown",
    "text/csv": "CSV",
    "application/json": "JSON",
    "application/zip": "ZIP",
    "application/octet-stream": "BIN",
  };
  // `known` 是对象字面量，`Object.prototype` 上的键也"存在"：`typeLabel("constructor")`
  // 会返回一个原生函数，标签就渲染成 `function Object() { [native code] }`。
  // 仓库里其余几处都用 hasOwnProperty.call（catalog.js ×3、app.js ×1）：这个包
  // 没有转译步骤，而 `Object.hasOwn` 是 ES2022 —— 旧 Safari 会在渲染附件标签
  // 时直接 TypeError，把整个附件条带下去。
  const label = Object.prototype.hasOwnProperty.call(known, value) ? known[value] : "";
  return label || value.toUpperCase() || "FILE";
}

/**
 * 「类型 · 体积」一行，附件卡与灯箱头共用。
 *
 * 三处各写一遍的时候，改一次格式要改三次，漏一处就是两个地方对同一份文件
 * 说两个数。
 *
 * @param {{mime?: string, size?: number}} file
 * @returns {string}
 */
export function fileMeta(file) {
  return `${typeLabel(file && file.mime)} · ${formatBytes(file && file.size)}`;
}
