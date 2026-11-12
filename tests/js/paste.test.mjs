import test from "node:test";
import assert from "node:assert/strict";

import {
  FIT_STEPS,
  IMAGE_TYPES,
  fitPlan,
  isAnimatedImage,
  pasteFiles,
  pastedFiles,
  pastedName,
  sniffImageType,
} from "../../src/oacp/web/paste.js";

/** A File whose first bytes decide what the sniffer sees. */
function fakeFile(bytes, name = "image.png", type = "") {
  const head = new Uint8Array(12);
  head.set(bytes.slice(0, 12));
  return new File([head], name, { type });
}

/** A clipboard shape: `items` with getAsFile, and/or a plain `files` list. */
function clipboard({ items = [], files = [] }) {
  return {
    items: items.map((file) => ({ kind: "file", getAsFile: () => file })),
    files,
  };
}

const PNG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const JPEG = [0xff, 0xd8, 0xff, 0xe0];
const GIF = [0x47, 0x49, 0x46, 0x38, 0x39, 0x61];
const WEBP = [0x52, 0x49, 0x46, 0x46, 0x10, 0x00, 0x00, 0x00, 0x57, 0x45, 0x42, 0x50];

// --------------------------------------------------------------- pasteFiles

test("items win, because Safari may leave files empty", () => {
  const a = fakeFile(PNG, "a.png", "image/png");
  const b = fakeFile(JPEG, "b.jpg", "image/jpeg");
  const found = pasteFiles(clipboard({ items: [a, b] }));
  assert.deepEqual(
    found.map((file) => file.name),
    ["a.png", "b.jpg"],
  );
});

test("files are the fallback when no item carries one", () => {
  const a = fakeFile(PNG, "a.png", "image/png");
  assert.equal(pasteFiles(clipboard({ files: [a] })).length, 1);
});

test("a text-only clipboard yields nothing — the browser keeps it", () => {
  assert.deepEqual(pasteFiles(clipboard({ items: [] })), []);
  assert.deepEqual(pasteFiles({ items: [{ kind: "string", getAsFile: () => null }] }), []);
  assert.deepEqual(pasteFiles(null), []);
  assert.deepEqual(pasteFiles(undefined), []);
});

test("a picture plus text still yields the picture", () => {
  const a = fakeFile(PNG, "a.png", "image/png");
  const data = {
    items: [
      { kind: "string", getAsFile: () => null },
      { kind: "file", getAsFile: () => a },
    ],
    files: [a],
  };
  assert.deepEqual(
    pasteFiles(data).map((file) => file.name),
    ["a.png"],
  );
});

// ----------------------------------------------------------- sniffImageType

test("the magic number rescues a file whose type is empty", async () => {
  assert.equal(await sniffImageType(fakeFile(PNG)), "image/png");
  assert.equal(await sniffImageType(fakeFile(JPEG)), "image/jpeg");
  assert.equal(await sniffImageType(fakeFile(GIF)), "image/gif");
  assert.equal(await sniffImageType(fakeFile(WEBP)), "image/webp");
});

test("nonsense is reported as nothing, not guessed", async () => {
  assert.equal(await sniffImageType(fakeFile([1, 2, 3, 4])), "");
  // Too short to decide: a 4-byte PNG is not a PNG anyone can display.
  assert.equal(await sniffImageType(new File([new Uint8Array(4)], "tiny.png")), "");
  // Unreadable is a *third* answer, and the only one allowed to fall back to the
  // declared type: nothing was read, so the declaration is all there is.
  assert.equal(await sniffImageType({}), null);
  assert.equal(await sniffImageType(null), null);
});

test("bytes that are not one of ours are refused even when the declaration says image", async () => {
  // 改了扩展名的 PDF / 资源管理器里拖出来的文本：声明是 image/png，字节不是。
  // 放它过去，chip 会显示正常，直到服务端重新嗅一遍才 415 —— 那正是这一层要挡的。
  const liar = fakeFile([1, 2, 3, 4], "fake.png", "image/png");
  assert.deepEqual(await pastedFiles([liar]), []);
});

test("the PNG signature is all eight bytes, not the first four", async () => {
  // \x89PNG followed by something else is not a PNG. Matching four bytes would
  // have accepted it and handed the server bytes it then rejects as mismatched.
  assert.equal(await sniffImageType(fakeFile([0x89, 0x50, 0x4e, 0x47, 0, 0, 0, 0])), "");
  assert.equal(await sniffImageType(fakeFile(PNG)), "image/png");
});

// --------------------------------------------------------------- pastedName

test("a pasted name is sortable, ASCII, and dated", () => {
  const when = new Date(2026, 10, 12, 9, 16, 5);
  assert.equal(pastedName(when, 0, "image/png"), "screenshot-20261112-091605.png");
  assert.equal(pastedName(when, 0, "image/jpeg"), "screenshot-20261112-091605.jpg");
  assert.equal(pastedName(when, 0, "image/webp"), "screenshot-20261112-091605.webp");
  assert.equal(pastedName(when, 0, "image/gif"), "screenshot-20261112-091605.gif");
});

test("the second and third picture in one paste do not collide", () => {
  const when = new Date(2026, 0, 2, 3, 4, 5);
  assert.equal(pastedName(when, 0, "image/png"), "screenshot-20260102-030405.png");
  assert.equal(pastedName(when, 1, "image/png"), "screenshot-20260102-030405-2.png");
  assert.equal(pastedName(when, 4, "image/png"), "screenshot-20260102-030405-5.png");
});

// ---------------------------------------------------------------- pastedFiles

test("an empty type is filled in, and a default name is replaced", async () => {
  const when = new Date(2026, 10, 12, 9, 16, 5);
  const files = await pastedFiles(clipboard({ items: [fakeFile(PNG)] }), when);
  assert.equal(files.length, 1);
  assert.equal(files[0].type, "image/png");
  assert.equal(files[0].name, "screenshot-20261112-091605.png");
});

test("a real file name survives — only the browser default is rewritten", async () => {
  const when = new Date(2026, 10, 12, 9, 16, 5);
  const files = await pastedFiles(
    clipboard({ items: [fakeFile(PNG, "excel-chart.png", "image/png")] }),
    when,
  );
  assert.equal(files[0].name, "excel-chart.png");
});

test("several pictures are numbered in order", async () => {
  const when = new Date(2026, 10, 12, 9, 16, 5);
  const files = await pastedFiles(
    clipboard({ items: [fakeFile(PNG), fakeFile(JPEG), fakeFile(GIF)] }),
    when,
  );
  assert.deepEqual(
    files.map((file) => file.name),
    [
      "screenshot-20261112-091605.png",
      "screenshot-20261112-091605-2.jpg",
      "screenshot-20261112-091605-3.gif",
    ],
  );
});

test("something that is not an image is dropped, not renamed and retried", async () => {
  const files = await pastedFiles(clipboard({ items: [fakeFile([1, 2, 3, 4], "notes.txt")] }));
  assert.deepEqual(files, []);
});

test("the bytes decide, so an octet-stream PNG is rescued", async () => {
  // Some clipboard sources hand over `application/octet-stream`; the server
  // re-sniffs the bytes anyway, so trusting the declaration here would drop a
  // perfectly good picture.
  const files = await pastedFiles(clipboard({ items: [fakeFile(PNG, "image.png", "application/octet-stream")] }));
  assert.equal(files.length, 1);
  assert.equal(files[0].type, "image/png");
});

test("a mislabelled file is corrected instead of failing at upload", async () => {
  // A JPEG renamed to .png: declaring it as image/png would make the server
  // answer 415 (attachment_type_mismatch) after the picture already looked fine.
  const files = await pastedFiles(clipboard({ items: [fakeFile(JPEG, "renamed.png", "image/png")] }));
  assert.equal(files.length, 1);
  assert.equal(files[0].type, "image/jpeg");
  assert.equal(files[0].name, "renamed.png");
});

test("every type the panel accepts can be pasted", () => {
  assert.deepEqual(IMAGE_TYPES, ["image/png", "image/jpeg", "image/webp", "image/gif"]);
});

// ------------------------------------------------------------------- fitPlan

test("an image already inside the largest step is not touched", () => {
  assert.deepEqual(fitPlan(1920, 1080), []);
  assert.deepEqual(fitPlan(800, 600), []);
  // One pixel over the largest step: every step still applies, and the first one
  // lands on 1920. Stopping at the first small-enough step is fitToLimit's job —
  // it can only decide that after encoding and measuring the bytes.
  const over = fitPlan(1921, 1080);
  assert.equal(over.length, 4);
  assert.ok(over[0].width <= FIT_STEPS[0]);
  // Height follows the ratio to the nearest pixel, so it lands on 1079 — the
  // assertion is about the step, not about a rounding detail.
  assert.ok(Math.abs(over[0].height - 1080) <= 1);
});

test("the long edge steps down and the aspect ratio holds", () => {
  const plan = fitPlan(3840, 2160);
  assert.equal(plan.length, FIT_STEPS.length);
  assert.equal(plan[0].width, 1920);
  assert.equal(plan[0].height, 1080);
  assert.equal(plan.at(-1).width, 1280);
  assert.equal(plan.at(-1).height, 720);
  for (const step of plan) assert.ok(Math.abs(step.width / step.height - 16 / 9) < 0.01);
});

test("a portrait screenshot steps by its own long edge", () => {
  const plan = fitPlan(1080, 3840);
  assert.equal(plan[0].height, 1920);
  assert.equal(plan[0].width, 540);
  assert.equal(plan.at(-1).height, 1280);
  assert.equal(plan.at(-1).width, 360);
});

test("a plan never shrinks something to zero", () => {
  for (const step of fitPlan(9000, 1)) {
    assert.ok(step.width >= 1);
    assert.ok(step.height >= 1);
  }
});


// -------------------------------------------------------------- pastedFiles(list)

test("a list that is already collected is converted without re-reading a clipboard", async () => {
  // app.js 先扫一遍剪贴板（判断"到底有没有文件"），再把结果传进来。
  // 这里断言第二条路真的吃得下数组 —— 否则它会在内部再扫一次剪贴板。
  const file = fakeFile(PNG, "shot.png", "image/png");
  const out = await pastedFiles([file]);
  assert.equal(out.length, 1);
  assert.equal(out[0].name, "shot.png");
  assert.equal(out[0].type, "image/png");
});

// ------------------------------------------------------------- isAnimatedImage

/** PNG chunk：4 字节长度（大端）+ 4 字节类型 + 数据 + 4 字节 CRC（这里填 0）。 */
function pngChunk(type, data = []) {
  const out = new Uint8Array(12 + data.length);
  const view = new DataView(out.buffer);
  view.setUint32(0, data.length);
  for (let index = 0; index < 4; index += 1) out[4 + index] = type.charCodeAt(index);
  out.set(data, 8);
  return out;
}

/** RIFF chunk：4 字节 fourcc + 4 字节小端长度 + 数据（奇数长度补一个字节）。 */
function riffChunk(fourcc, data = []) {
  const out = new Uint8Array(8 + data.length + (data.length & 1));
  const view = new DataView(out.buffer);
  for (let index = 0; index < 4; index += 1) out[index] = fourcc.charCodeAt(index);
  view.setUint32(4, data.length, true);
  out.set(data, 8);
  return out;
}

/** 保留全部字节的 File —— fakeFile 只留前 12 字节，读不了 chunk 流。 */
function bytesFile(chunks, type, name) {
  const flat = new Uint8Array(chunks.reduce((sum, chunk) => sum + chunk.length, 0));
  let offset = 0;
  for (const chunk of chunks) {
    flat.set(chunk, offset);
    offset += chunk.length;
  }
  return new File([flat], name, { type });
}

const PNG_SIG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

function pngFile(chunks) {
  return bytesFile([new Uint8Array(PNG_SIG), ...chunks], "image/png", "x.png");
}

function webpFile(chunks) {
  const body = chunks.reduce((sum, chunk) => sum + chunk.length, 0) + 4;
  const head = new Uint8Array(12);
  const view = new DataView(head.buffer);
  head.set([0x52, 0x49, 0x46, 0x46], 0); // RIFF
  view.setUint32(4, body, true);
  head.set([0x57, 0x45, 0x42, 0x50], 8); // WEBP
  return bytesFile([head, ...chunks], "image/webp", "x.webp");
}

test("a still PNG is not mistaken for an APNG", async () => {
  const file = pngFile([
    pngChunk("IHDR", new Array(13).fill(0)),
    pngChunk("IDAT", [1, 2, 3]),
    pngChunk("IEND"),
  ]);
  assert.equal(await isAnimatedImage(file), false);
});

test("an APNG is caught by its acTL chunk, not by its MIME", async () => {
  // APNG 的名字就是 image/png。只按 MIME 判，它会被压成一张只剩第一帧的静止图。
  const file = pngFile([
    pngChunk("IHDR", new Array(13).fill(0)),
    pngChunk("acTL", [0, 0, 0, 2, 0, 0, 0, 0]),
    pngChunk("IDAT", [1]),
  ]);
  assert.equal(await isAnimatedImage(file), true);
});

test("a still WebP is not mistaken for an animation", async () => {
  const file = webpFile([riffChunk("VP8L", [0x2f])]);
  assert.equal(await isAnimatedImage(file), false);
});

test("an animated WebP is caught by its ANIM chunk", async () => {
  const file = webpFile([
    riffChunk("VP8X", new Array(10).fill(0)),
    riffChunk("ANIM", new Array(6).fill(0)),
    riffChunk("ANMF", [1]),
  ]);
  assert.equal(await isAnimatedImage(file), true);
});

test("a GIF is an animation whatever its bytes say", async () => {
  const file = new File([new Uint8Array(GIF)], "x.gif", { type: "image/gif" });
  assert.equal(await isAnimatedImage(file), true);
});

test("a JPEG has no animation to lose", async () => {
  const file = new File([new Uint8Array([...JPEG, 0, 1, 2, 3])], "x.jpg", { type: "image/jpeg" });
  assert.equal(await isAnimatedImage(file), false);
});

test("truncated or unreadable bytes are treated as still, never as a reason to reject", async () => {
  // 解析不动就按静态处理：宁可压一张畸形图，也不要因为读不出 chunk
  // 把一堆正常截图全拒收。
  const tiny = new File([new Uint8Array(PNG_SIG)], "x.png", { type: "image/png" });
  assert.equal(await isAnimatedImage(tiny), false);
  const broken = new File([new Uint8Array([1, 2, 3])], "x.png", { type: "image/png" });
  assert.equal(await isAnimatedImage(broken), false);
  assert.equal(await isAnimatedImage(null), false);
});
