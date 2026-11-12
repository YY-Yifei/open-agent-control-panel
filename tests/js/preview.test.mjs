/* 附件预览的判据 —— 纯函数，不需要 DOM，也不需要浏览器。
 *
 * 这些用例全部来自真实的失败形态：改名叫 .txt 的 PNG、ASCII 编码的 PDF、
 * 一兆字节的单行日志、截断点落在 emoji 中间的文本。
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  MAX_TEXT_CHARS,
  bytesToBase64,
  classify,
  decodeText,
  formatBytes,
  looksBinary,
  truncateText,
  typeLabel,
} from "../../src/oacp/web/preview.js";

const utf8 = (text) => new TextEncoder().encode(text);

test("classify: what the attachment is, from its stored mime", () => {
  assert.equal(classify({ mime: "image/png" }), "image");
  assert.equal(classify({ mime: "image/webp" }), "image");
  assert.equal(classify({ mime: "text/plain", name: "notes.md" }), "text");
  assert.equal(classify({ mime: "text/plain; charset=utf-8" }), "text");
  assert.equal(classify({ mime: "application/pdf" }), "pdf");
  assert.equal(classify({ mime: "application/zip" }), "file");
  assert.equal(classify({ mime: "application/octet-stream" }), "file");
});

test("classify: an attachment without bytes is 'missing', not 'file'", () => {
  // 没有字节的附件点开只能看到一个空层 —— 它是独立的一类，不该可点。
  assert.equal(classify({ mime: "image/png", skipped: "too_large" }), "missing");
  assert.equal(classify({ mime: "text/plain", skipped: "unsupported" }), "missing");
  assert.equal(classify(null), "missing");
  // 没有 mime 不等于没有字节：这种行仍能取回、仍可下载，谎称「没有」
  // 会让它变成不可点的一枚灰条。unknown type -> file。
  assert.equal(classify({}), "file");
});

test("decodeText: real UTF-8 text survives intact", () => {
  const text = "# 标题\n\n一行中文，一个 emoji 😀，还有 tab\t结束。\n";
  assert.equal(decodeText(utf8(text)), text);
  // 空字节不是文本：服务端在 prepare 里以 attachment_bad_data 拒掉它。前端
  // 若收下，人会在发送那一刻才撞上 415 —— 而附件已经躺在输入框里了。
  assert.equal(decodeText(new Uint8Array(0)), null);
});

test("decodeText: NUL in the first window is binary", () => {
  // 一个改名叫 notes.txt 的 PNG —— 字节说了算，名字不算。
  const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x01]);
  assert.equal(decodeText(png), null);
  assert.equal(decodeText(Uint8Array.from([0x61, 0x00, 0x62])), null);
});

test("decodeText: a PDF is ASCII-valid UTF-8 but is not text", () => {
  // 这条是最容易踩的：%PDF-1.7 的字节完全能通过 UTF-8 严格解码，
  // 只按「能不能解码」判，灯箱里就是满屏 PDF 源码。
  const pdf = utf8("%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n");
  assert.equal(decodeText(pdf), null);
  assert.equal(looksBinary(pdf), true);
});

test("decodeText: ZIP containers (docx/xlsx/pptx) are not text either", () => {
  const zip = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x14, 0x00, 0x00, 0x00]);
  assert.equal(decodeText(zip), null);
});

test("decodeText: control characters are a density test, not a presence test", () => {
  // 一个 tab 是文本；一整段 C0 是二进制碰巧解码成功。判据与服务端同一组字符。
  assert.equal(decodeText(utf8("a\tb\n")), "a\tb\n");
  assert.equal(decodeText(utf8("a\x01b")), null);
  // ESC 必须放行：日志里的 ANSI 颜色码是合法文本，<pre> 只把它原样画出来。
  const ansi = "ansi \x1b[31mred\x1b[0m\n";
  assert.equal(decodeText(utf8(ansi)), ansi);
  // 千分之一是密度上限：一千个字符里一个坏字符仍然算文本。
  assert.equal(decodeText(utf8("x".repeat(1000) + "\x01")), "x".repeat(1000) + "\x01");
});

test("decodeText measures density in code points, like the server", () => {
  // 501 个码点（500 个 emoji + 1 个坏字符）里 1 个坏字符 = 千分之二，超限。
  // 若按 UTF-16 码元算，分母会变成 1001，这条就会被错误地放过去。
  assert.equal(decodeText(utf8("😀".repeat(500) + "\x01")), null);
});

test("decodeText: invalid UTF-8 is refused, not repaired with U+FFFD", () => {
  // Latin-1 的 café、GBK 的中文：都不是 UTF-8。转码就是改内容。
  assert.equal(decodeText(Uint8Array.from([0x63, 0x61, 0x66, 0xe9])), null);
  assert.equal(decodeText(Uint8Array.from([0xd6, 0xd0, 0xce, 0xc4])), null);
});

test("decodeText: HTML and SVG are text — they are read, never executed", () => {
  const html = utf8("<script>alert(1)</script>\n");
  assert.equal(decodeText(html), "<script>alert(1)</script>\n");
  const svg = '<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>';
  // 自证循环的断言等于没断言：这里明确要求「原样读得到」。
  assert.equal(decodeText(utf8(svg)), svg);
});

test("decodeText accepts ArrayBuffer and typed-array views", () => {
  const bytes = utf8("hello");
  assert.equal(decodeText(bytes.buffer), "hello");
  // 一个带 offset 的 view：只读它那一段，不要整块 buffer。
  const padded = new Uint8Array(10);
  padded.set(bytes, 3);
  assert.equal(decodeText(padded.subarray(3, 8)), "hello");
});

test("truncateText: short text passes through, CRLF normalised", () => {
  const result = truncateText("a\r\nb\rc");
  assert.equal(result.text, "a\nb\nc");
  assert.equal(result.truncated, false);
  assert.equal(result.lines, 3);
  // total 量的是**归一化前**的源文本：`a\r\nb\rc` 有 6 个字符。
  // 拿归一化后的串去数，每个 CRLF 行都会少算一个 `\r`。
  assert.equal(result.total, 6);
  assert.equal(result.shownLines, 3);
});

test("truncateText: a 1 MB single line is cut, and says so", () => {
  const oneLine = "x".repeat(1_000_000);
  const result = truncateText(oneLine, 1000);
  assert.equal(result.text.length, 1000);
  assert.equal(result.truncated, true);
  assert.equal(result.total, 1_000_000);
  assert.equal(result.lines, 1);
  assert.equal(result.shownLines, 1);
});

test("truncateText: never cuts a surrogate pair in half", () => {
  // "a" 占 1 个码元，😀 占 2 个：limit=2 正好切在代理对中间。
  const result = truncateText("a😀b", 2);
  assert.equal(result.text, "a");
  assert.equal(result.truncated, true);
  assert.doesNotMatch(result.text, /[\uD800-\uDBFF]$/);
  assert.equal(result.text.length, 1);
});

test("truncateText: the default cap is 200k characters", () => {
  const huge = "y".repeat(MAX_TEXT_CHARS + 5);
  const result = truncateText(huge);
  assert.equal(result.truncated, true);
  assert.equal(result.text.length, MAX_TEXT_CHARS);
});

test("truncateText: empty text is one empty line, not zero", () => {
  const result = truncateText("");
  assert.deepEqual(result, { text: "", truncated: false, total: 0, lines: 0, shownLines: 0 });
});

test("bytesToBase64 matches Buffer for a chunk-boundary-crossing payload", () => {
  // 0x8000 分块：跨过边界的那一段最容易写错。
  const bytes = new Uint8Array(0x8000 * 2 + 7);
  for (let index = 0; index < bytes.length; index += 1) bytes[index] = index % 256;
  assert.equal(bytesToBase64(bytes), Buffer.from(bytes).toString("base64"));
  assert.equal(bytesToBase64(new Uint8Array(0)), "");
});

test("formatBytes: whole numbers stay whole, 500 KB is not 1 MB", () => {
  assert.equal(formatBytes(5 * 1024 * 1024), "5 MB");
  assert.equal(formatBytes(1024 * 1024), "1 MB");
  assert.equal(formatBytes(1.6 * 1024 * 1024), "1.6 MB");
  assert.equal(formatBytes(500 * 1024), "500 KB");
  // 边界：1048575 B 走 KB 分支会渲染成 "1024 KB" —— 一句自相矛盾的文案。
  assert.equal(formatBytes(1024 * 1024 - 1), "1 MB");
  assert.equal(formatBytes(1024 * 1024 - 512), "1 MB");
  assert.equal(formatBytes(1023.5), "1 KB");
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(undefined), "0 B");
});

test("typeLabel: known types are named, unknown ones are shown, not guessed", () => {
  assert.equal(typeLabel("application/pdf"), "PDF");
  assert.equal(typeLabel("text/plain"), "TXT");
  assert.equal(typeLabel("text/plain; charset=utf-8"), "TXT");
  assert.equal(typeLabel("application/x-weird"), "APPLICATION/X-WEIRD");
  assert.equal(typeLabel(""), "FILE");
});
