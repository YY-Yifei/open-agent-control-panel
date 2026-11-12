/**
 * 粘贴取图 —— 只有纯函数，不碰 DOM。
 *
 * 为什么单独一个文件：剪贴板的形状（items / files / 缺 type / 一次多张）、
 * 命名、缩放阶梯、动图判定都是易错且值得盯的地方，而 app.js 一被 import 就会 boot()，
 * 进不了 node:test。副作用（canvas 压缩）留在 app.js，这里只做判断。
 */

/** 面板认的图片类型 —— 全仓唯一一份：选文件、粘贴、`accept` 都从它派生。 */
export const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];

/** 超限截图的长边阶梯：逐档试，够小就停。1280 仍超就交回原图。 */
export const FIT_STEPS = [1920, 1600, 1440, 1280];

/**
 * 剪贴板里的文件项。
 *
 * Safari 只可靠地给 `items`，`files` 可能是空的；而 Chrome 两个都填。
 * 所以先读 items，一个都没读出来才退回 files —— 反过来会在 Safari 上
 * 静默变成「粘了没反应」。
 */
export function pasteFiles(clipboard) {
  if (!clipboard) return [];
  const out = [];
  const items = clipboard.items ? Array.from(clipboard.items) : [];
  for (const item of items) {
    if (!item || item.kind !== "file") continue;
    const file = typeof item.getAsFile === "function" ? item.getAsFile() : null;
    if (file) out.push(file);
  }
  if (out.length) return out;
  return clipboard.files ? Array.from(clipboard.files).filter(Boolean) : [];
}

/**
 * 前 12 字节的魔数。
 *
 * 三种返回，含义不同，别混：
 *   - 类型串（`"image/png"`）：认出来了；
 *   - `""`：字节读得到，但不是这张表里的格式 —— **如实判定为不支持**；
 *   - `null`：字节根本读不了（没有 `slice`、读的时候抛错）。只有这一种才该退回声明。
 *
 * 为什么要把后两者分开：四种格式的签名都是明确的，所以「读到了但认不出」
 * 只可能是一个声明与字节不符的文件（改了扩展名的 PDF、从资源管理器拖出来的
 * 文本）。放它过去，chip 会显示正常，直到服务端重新嗅一遍才 415 —— 那正是
 * 这个函数要提前挡掉的那类晚失败。
 */
export async function sniffImageType(file) {
  if (!file || typeof file.slice !== "function") return null;
  let head;
  try {
    head = new Uint8Array(await file.slice(0, 12).arrayBuffer());
  } catch {
    return null;
  }
  if (head.length < 12) return "";
  const at = (offset, bytes) => bytes.every((byte, index) => head[offset + index] === byte);
  // PNG 的签名是 8 字节；只查前 4 个会把任意 \x89PNG 前缀的数据误认成 PNG。
  if (at(0, [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) return "image/png";
  if (at(0, [0xff, 0xd8, 0xff])) return "image/jpeg";
  if (at(0, [0x47, 0x49, 0x46, 0x38])) return "image/gif";
  // WebP 是 RIFF 容器：`RIFF....WEBP`，中间四个字节是长度。
  if (at(0, [0x52, 0x49, 0x46, 0x46]) && at(8, [0x57, 0x45, 0x42, 0x50])) return "image/webp";
  return "";
}

/**
 * `screenshot-20261112-091600.png`；同一次粘贴的后续几张加 `-2`、`-3`。
 *
 * 浏览器给粘贴的图起名 `image.png`，这个名字会出现在附件条、消息气泡的
 * `alt`、服务端落库的文件名、以及模型读到的 part 名里 —— 四个地方都叫
 * `image.png`，翻旧会话时完全分不清哪张是哪张。
 */
export function pastedName(date, index, mime) {
  const pad = (value) => String(value).padStart(2, "0");
  const stamp =
    `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}` +
    `-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
  const ext = { "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif" }[mime] || "png";
  return `screenshot-${stamp}${index > 0 ? `-${index + 1}` : ""}.${ext}`;
}

/**
 * 剪贴板 → 可直接交给 `addFiles` 的 File[]：补上 type、给出可读的名字、
 * 丢掉不是图片的东西。
 *
 * 第一个参数既收 `clipboardData`，也收已经由 `pasteFiles` 抽好的数组。
 * 调用方（app.js 的 paste 监听）必须先用数组那条路：它得在**过滤之前**
 * 就知道"剪贴板里到底有没有文件"，否则一段纯文字也会被当成待处理的粘贴。
 * 让它把抽好的结果传进来，剪贴板就只被扫一次 —— `getAsFile()` 调两次，
 * 万一台浏览器两次给出不同的东西，"有没有"和"收了什么"就对不上了。
 *
 * 注意这里**不**过滤「没有 type 的项」：那正是魔数兜底要救回来的那批。
 */
export async function pastedFiles(source, now = new Date()) {
  const found = Array.isArray(source) ? source : pasteFiles(source);
  const out = [];
  for (const file of found) {
    // 字节说了算。服务端会用字节重新嗅一遍，声明与字节不一致直接回 415
    // （attachment_type_mismatch）—— 所以这里也不信声明：
    //   · `application/octet-stream` + PNG 字节  → 嗅出 PNG，救回来
    //   · JPEG 字节 + 声明 `image/png`（重命名过的文件） → 嗅出 JPEG，纠正
    // 只有「字节读不了」才退回声明；「读到了但不是这四种」一律丢掉 ——
    // 放进去只会等到服务端 415 时才发现。
    const sniffed = (await sniffImageType(file)) ?? String(file.type || "").toLowerCase();
    if (!IMAGE_TYPES.includes(sniffed)) continue;
    // 用户真给了名字（从资源管理器复制的文件）就留着；只有浏览器默认的
    // `image.png` / `blob` 才换成时间戳。
    const named = Boolean(file.name) && file.name !== "image.png" && file.name !== "blob";
    const name = named ? file.name : pastedName(now, out.length, sniffed);
    out.push(new File([file], name, { type: sniffed }));
  }
  return out;
}

/**
 * 逐档长边。返回空数组 = 尺寸本身就不超，不必缩。
 *
 * 只按长边算：竖屏截图与横屏截图走同一套阶梯，缩放比一致，不会一边一档。
 */
export function fitPlan(width, height) {
  const long = Math.max(width, height);
  const plan = [];
  for (const max of FIT_STEPS) {
    if (long <= max) break;
    const scale = max / long;
    plan.push({
      max,
      width: Math.max(1, Math.round(width * scale)),
      height: Math.max(1, Math.round(height * scale)),
    });
  }
  return plan;
}

/**
 * 是不是动图 —— GIF、APNG、动态 WebP。
 *
 * 只按 MIME 判会漏掉后两种：APNG 的名字就是 `image/png`，动态 WebP 就是
 * `image/webp`。而 canvas 的 `drawImage` + `toBlob` 只会留下第一帧 —— 于是
 * 一张超限的 APNG 会被"压缩"成一张静止图，还被告知「已压缩」。那正是
 * `fitToLimit` 上面写着"不换内容"却悄悄换了内容的情况。
 *
 * 解析不动就按静态处理 —— 宁可压一张畸形图，也不要因为解析失败把正常图
 * 全部拒收。
 */
export async function isAnimatedImage(file) {
  const type = String((file && file.type) || "").toLowerCase();
  if (type === "image/gif") return true;
  if (type !== "image/png" && type !== "image/webp") return false;
  if (typeof file.slice !== "function") return false;
  try {
    return type === "image/png" ? await _apng(file) : await _webpAnimated(file);
  } catch {
    return false;
  }
}

/**
 * 读一段字节；读不了返回 null。
 *
 * 为什么每次只读 8 个字节：块的**头**才是需要的，而块的数据可以任意大。
 * 真实截图里 `iCCP`（ICC 色彩配置）常是 100 KB 以上，libwebp 还把 `ICCP`
 * 排在 `VP8X` 与 `ANIM` 之间 —— 只读一个 64 KB 窗口，就会把 `acTL` / `ANIM`
 * 推到窗口外，然后把一张动图当静态图压掉第一帧。
 */
async function _window(file, offset, length) {
  try {
    return new Uint8Array(await file.slice(offset, offset + length).arrayBuffer());
  } catch {
    return null;
  }
}

const _MAX_CHUNKS = 4096;

/** PNG chunk 流：4 字节长度（大端）+ 4 字节类型 + 数据 + 4 字节 CRC。 */
async function _apng(file) {
  let offset = 8; // 跳过 8 字节签名
  for (let guard = 0; guard < _MAX_CHUNKS; guard += 1) {
    const head = await _window(file, offset, 8);
    if (!head || head.length < 8) return false;
    const length = new DataView(head.buffer, head.byteOffset, head.byteLength).getUint32(0);
    const type = _fourcc(head, 4);
    if (type === "acTL") return true;
    // acTL 必须在第一个 IDAT 之前 —— 见到 IDAT 就可以收工。
    if (type === "IDAT" || type === "IEND") return false;
    offset += 12 + length;
  }
  return false;
}

/** RIFF 容器：4 字节 fourcc + 4 字节小端长度 + 数据（奇数长度补齐一字节）。 */
async function _webpAnimated(file) {
  let offset = 12; // 跳过 `RIFF` + 长度 + `WEBP`
  for (let guard = 0; guard < _MAX_CHUNKS; guard += 1) {
    const head = await _window(file, offset, 8);
    if (!head || head.length < 8) return false;
    const type = _fourcc(head, 0);
    const size = new DataView(head.buffer, head.byteOffset, head.byteLength).getUint32(4, true);
    if (type === "ANIM" || type === "ANMF") return true;
    // 静态位图数据开始出现，说明后面没有动画块了。
    if (type === "VP8 " || type === "VP8L") return false;
    offset += 8 + size + (size & 1);
  }
  return false;
}

function _fourcc(bytes, offset) {
  return String.fromCharCode(bytes[offset], bytes[offset + 1], bytes[offset + 2], bytes[offset + 3]);
}
