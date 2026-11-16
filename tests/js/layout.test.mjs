/* layout.js 的判据，全部在无 DOM 的情况下钉住。
 *
 * 三件事值得单独测，因为它们都是「看起来对、其实错」的地方：
 *
 *  1. **wanted 与 applied 是两层**。在小窗口里打开一次页面，不该把人选的
 *     480px 永久压成 320px —— 夹是显示的事，落盘的是人的选择。
 *  2. **停靠由算得出，不由媒体查询猜**。两处断点（CSS 一个、JS 一个）迟早
 *     会不一致，所以只有 canDock() 一个真源。
 *  3. **档位名必须走 hasOwnProperty**。`READING_PRESETS["toString"]` 是继承
 *     来的函数，一个 `!== undefined` 就足以让 "toString" 成为合法档位，
 *     然后把 `max-width: toString` 写进样式表。
 */

import test from "node:test";
import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const WEB = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "src", "oacp", "web");
const {
  DRAWER,
  LAYOUT_KEY,
  MAIN_MIN,
  READING_PRESETS,
  SIDEBAR,
  canDock,
  clampDrawer,
  clampSide,
  createLayout,
  defaults,
  isReadingPreset,
  parseLayout,
  readingCss,
  resolveLayout,
  serializeLayout,
  stepped,
} = await import(pathToFileURL(join(WEB, "layout.js")).href);

function memoryWin(width = 0, seed = null) {
  const box = new Map();
  if (seed) box.set(LAYOUT_KEY, seed);
  return {
    innerWidth: width,
    localStorage: {
      getItem: (key) => (box.has(key) ? box.get(key) : null),
      setItem: (key, value) => box.set(key, String(value)),
      removeItem: (key) => box.delete(key),
    },
  };
}

/* ------------------------------------------------------------------ 夹 */

test("a sidebar width is kept inside its absolute bounds", () => {
  assert.equal(clampSide(268), 268);
  assert.equal(clampSide(0), SIDEBAR.min);
  assert.equal(clampSide(9999), SIDEBAR.max);
  assert.equal(clampSide("infinite"), SIDEBAR.default, "a bad value falls back, it does not become NaN");
  assert.equal(clampSide(null), SIDEBAR.default);
});

test("a narrow window never squeezes the sidebar below a usable floor", () => {
  // 视口小到「装不下主区地板 + 左栏下限」时，宁可让主区少几像素 ——
  // 左栏是导航，窄得看不见比主区窄更糟。
  assert.equal(clampSide(300, { viewport: 400 }), SIDEBAR.min);
});

test("when docked, the conversation's floor is subtracted before the drawer may grow", () => {
  // 1200 - 268 - 560 = 372：这是停靠状态下助手栏能拿到的最宽值。
  assert.equal(clampDrawer(400, { viewport: 1200, side: 268 }), 372);
  assert.equal(clampDrawer(400, { viewport: 0, side: 268 }), DRAWER.default, "no viewport, no squeeze");
  // 下限守住：再小的窗口也不给一个抓不住的窄条。
  assert.equal(clampDrawer(400, { viewport: 700, side: 268 }), DRAWER.min);
});

/* ---------------------------------------------------------------- 停靠 */

test("the drawer yields first: a wide drawer request never shrinks the sidebar", () => {
  // 只改助手栏。旧的夹法（先夹 side 再夹 drawer）会把这条请求变成 side=200 ——
  // 一个根本没被提到的字段被改了，而模型被要求照着 adjusted 说话，于是它会
  // 向用户报告一个自己从未要求过的左栏变化。
  const state = resolveLayout({ drawer: DRAWER.max }, { viewport: 1200 });
  assert.equal(state.side, SIDEBAR.default, "左栏不动");
  assert.ok(state.drawer < DRAWER.max, "让路的是助手栏");
  assert.deepEqual(
    state.adjusted.filter((line) => line.includes("sidebar")),
    [],
    "不许报告左栏变了",
  );
});

test("the default layout fits in the 1148-1228 band without shrinking itself", () => {
  // canDock() 只保证助手栏的_下限_放得下，所以这一段里「默认 268 + 400」
  // 并不真的装得下。让助手栏让路，默认布局才不会被自己挤小。
  for (const viewport of [1148, 1180, 1228]) {
    const state = resolveLayout(defaults(), { viewport });
    assert.equal(state.side, SIDEBAR.default, `viewport ${viewport} 不该压缩左栏`);
    assert.equal(state.docked, true, `viewport ${viewport} 仍然停靠`);
    // 助手栏被夹到 320 是**对的**（让路的是它）。要守的是「左栏一个字都不许动」。
    assert.deepEqual(
      state.adjusted.filter((line) => line.includes("sidebar")),
      [],
      `viewport ${viewport}: 默认布局不该压缩左栏（助手栏让路是允许的）`,
    );
  }
});

test("docking is decided by arithmetic, and the breakpoint is where it should be", () => {
  const side = SIDEBAR.default;
  const edge = side + MAIN_MIN + DRAWER.min;
  assert.equal(edge, 1148);
  assert.equal(canDock(edge - 1, side), false);
  assert.equal(canDock(edge, side), true);
  assert.equal(canDock(0, side), true, "unknown viewport keeps the default (docked)");
});

test("a wide sidebar can itself make docking impossible", () => {
  // 左栏拖到 480 之后，1200 的窗口已经给不出 320 的助手栏与 560 的主区。
  assert.equal(canDock(1200, 480), false);
  assert.equal(resolveLayout({ side: 480 }, { viewport: 1200 }).docked, false);
});

/* -------------------------------------------------------------- 分辨率 */

test("the three presets map to CSS, and 'full' is 'none'", () => {
  assert.equal(readingCss("comfortable"), "760px");
  assert.equal(readingCss("wide"), "1100px");
  assert.equal(readingCss("full"), "none");
  assert.equal(readingCss("nonsense"), "760px");
  assert.equal(READING_PRESETS.full, null);
});

test("an inherited key is not a preset name", () => {
  for (const name of ["toString", "constructor", "hasOwnProperty", "__proto__"]) {
    assert.equal(isReadingPreset(name), false, `${name} must not be a preset`);
    assert.equal(parseLayout({ reading: name }).reading, "comfortable");
  }
  assert.equal(isReadingPreset("wide"), true);
});

test("resolveLayout reports what it had to change, instead of pretending", () => {
  const state = resolveLayout({ side: 900, drawer: 400, reading: "wide" }, { viewport: 0 });
  assert.equal(state.side, 480);
  assert.equal(state.reading, "wide");
  assert.ok(state.adjusted.some((line) => line.includes("sidebar")), state.adjusted.join(" | "));

  const narrow = resolveLayout({ docked: true }, { viewport: 900 });
  assert.equal(narrow.docked, false);
  assert.ok(narrow.adjusted.some((line) => line.includes("overlay")), narrow.adjusted.join(" | "));
});

test("a default layout changes nothing and says nothing", () => {
  const state = resolveLayout(defaults(), { viewport: 1600 });
  assert.deepEqual(state.adjusted, []);
  assert.equal(state.side, SIDEBAR.default);
  assert.equal(state.drawer, DRAWER.default);
  assert.equal(state.docked, true);
});

/* ------------------------------------------------------------ 持久化 */

test("a broken payload falls back to the defaults, it does not throw", () => {
  for (const raw of ["", null, undefined, "{", "[]", "42", '"wide"', "null"]) {
    assert.deepEqual(parseLayout(raw), defaults(), `raw=${String(raw)}`);
  }
});

test("only an explicit false turns docking off", () => {
  assert.equal(parseLayout({ docked: false }).docked, false);
  assert.equal(parseLayout({ docked: "false" }).docked, true, "a string is a bad value, not a choice");
  assert.equal(parseLayout({ docked: null }).docked, true);
});

test("the persisted payload is the wanted width, already clamped to absolute bounds", () => {
  const saved = JSON.parse(serializeLayout({ side: 5000, drawer: 10, reading: "full", docked: false }));
  assert.deepEqual(saved, { v: 1, side: SIDEBAR.max, drawer: DRAWER.min, reading: "full", docked: false });
});

/* ------------------------------------------------------------ 键盘步进 */

test("an arrow key that means wider never lowers a clamped preference", () => {
  // 键盘从 wanted 起算（不是 applied）。wanted=420 在 1300px 窗口里显示 340，
  // 若从 340 起算，一次「变宽」的 → 会存下 356 —— 一个意思是加宽的键
  // 把偏好改小了，窗口一放宽反而比原来窄。
  assert.ok(stepped("side", "ArrowRight", 420) > 420, "从请求值往上走");
  assert.equal(stepped("side", "ArrowRight", 480), 480, "到顶就停住，不回弹");
});

test("arrow keys grow each side in the direction the pointer would", () => {
  // 左栏在左边：→ 变宽。
  assert.equal(stepped("side", "ArrowRight", 268), 268 + 16);
  assert.equal(stepped("side", "ArrowLeft", 268), 268 - 16);
  // 助手栏在右边：← 才变宽。方向反了，键盘和鼠标会朝相反的方向走。
  assert.equal(stepped("drawer", "ArrowLeft", 400), 400 + 16);
  assert.equal(stepped("drawer", "ArrowRight", 400), 400 - 16);
  assert.equal(stepped("drawer", "ArrowLeft", 400, true), 400 + 64);
  assert.equal(stepped("side", "Home", 300), SIDEBAR.min);
  assert.equal(stepped("side", "End", 300), SIDEBAR.max);
  assert.equal(stepped("side", "Enter", 300), null, "other keys are not ours");
  // 拖过界的一点点不该把值推到边界之外，否则再按一次反方向要先「抵消溢出」。
  assert.equal(stepped("side", "ArrowRight", SIDEBAR.max), SIDEBAR.max);
});

/* -------------------------------------------------- 控制器（无 DOM） */

test("the controller reads a fresh default layout when storage is empty", () => {
  const layout = createLayout({ win: memoryWin(1600) });
  const state = layout.state;
  assert.equal(state.side, 268);
  assert.equal(state.drawer, 400);
  assert.equal(state.reading, "comfortable");
  assert.equal(state.docked, true);
  assert.deepEqual(state.adjusted, []);
});

test("an unknown preset is refused, not silently swapped for a default", () => {
  const layout = createLayout({ win: memoryWin(1600) });
  assert.throws(() => layout.apply({ reading: "gigantic" }), /unknown reading preset/);
  assert.equal(layout.state.reading, "comfortable", "a refused write leaves the layout alone");
});

test("a wrong-typed value is refused, not quietly turned into a no-op", () => {
  const layout = createLayout({ win: memoryWin(1600) });
  // 名字对了、值坏了 —— 以前会退回当前值并返回成功快照。
  assert.throws(() => layout.apply({ side: "480px" }), /must be a finite number/);
  assert.throws(() => layout.apply({ drawer: null }), /must be a finite number/);
  // `Boolean("false")` 是 true：一个字符串会**开启**停靠，而 parseLayout
  // 早就把字符串当坏值。写路径必须一样严格。
  assert.throws(() => layout.apply({ docked: "false" }), /must be a boolean/);
  assert.equal(layout.wanted.docked, true, "被拒绝的写入没有改动任何东西");
});

test("a persisted payload from another version is ignored, not reinterpreted", () => {
  // v2 完全可能把 reading 换成像素数、把 side 换成比例。静默按 v1 解释
  // 就是「打开就看到一个错位页面」，正是 parseLayout 要防的那件事。
  assert.deepEqual(parseLayout({ v: 2, side: 400, reading: "wide" }), defaults());
  assert.equal(parseLayout({ v: 1, side: 400, reading: "wide" }).side, 400);
  // 没有 v 的（更早的、手写的）仍然按 v1 读，不让一次升级清掉人的偏好。
  assert.equal(parseLayout({ side: 300 }).side, 300);
});

test("an unknown field is refused instead of silently dropped", () => {
  // 这个 bug 真发生过：`ui_layout` 声明的名字是 `sidebar`，控制器内部叫 `side`，
  // 中间没有翻译 —— 于是模型每一次「把左栏调宽」都被丢掉，还拿到成功。
  // 静默丢参数的写入比拒绝更坏：模型会以为自己改成了。
  const layout = createLayout({ win: memoryWin(1600) });
  assert.throws(() => layout.apply({ sidebar: 320 }), /unknown layout field: sidebar/);
  assert.equal(layout.wanted.side, SIDEBAR.default, "a refused write changes nothing");
});

test("a clamp at the absolute bound is reported, not hidden", () => {
  const layout = createLayout({ win: memoryWin(1600) });
  const state = layout.apply({ side: 900 });
  assert.equal(state.side, SIDEBAR.max, "shown as 480");
  assert.equal(state.wanted.side, 900, "but the request is remembered as asked");
  assert.ok(
    state.adjusted.some((line) => line.includes("900") && line.includes("480")),
    `adjusted must say 900 -> 480, got ${JSON.stringify(state.adjusted)}`,
  );
});

test("a narrow window clamps the view but does not overwrite the person's choice", () => {
  // 900px 的窗口放不下 480px 的左栏：主区的地板是 560，所以左栏只能到 340。
  // 这条必须用「真的会被夹」的宽度 —— 用停靠不下来（走浮层分支）的宽度，
  // side 会原样返回，把视口夹整段删掉都测不出来。
  const win = memoryWin(900);
  const layout = createLayout({ win });
  const state = layout.apply({ side: 480 });
  assert.equal(state.wanted.side, 480, "请求被记住了");
  assert.equal(layout.wanted.side, 480);
  assert.equal(state.side, 340, "显示上被视口夹到 340");

  // 窗口变宽之后，那个选择自己回来了 —— 没有「被静默改小」这回事。
  win.innerWidth = 1600;
  assert.equal(layout.refresh().side, 480);
});

test("the layout round-trips through localStorage", () => {
  const win = memoryWin(1600);
  const first = createLayout({ win });
  first.apply({ reading: "wide", side: 320, drawer: 520, docked: false });
  assert.equal(win.localStorage.getItem(LAYOUT_KEY) !== null, true);

  const second = createLayout({ win });
  assert.deepEqual(second.wanted, { side: 320, drawer: 520, reading: "wide", docked: false });
});

test("reset puts everything back, and says so", () => {
  const layout = createLayout({ win: memoryWin(1600) });
  layout.apply({ reading: "full", side: 400 });
  assert.equal(layout.state.reading, "full");
  const state = layout.reset();
  assert.deepEqual(
    { reading: state.reading, side: state.side, drawer: state.drawer, docked: state.docked },
    { reading: "comfortable", side: 268, drawer: 400, docked: true },
  );
  assert.deepEqual(layout.wanted, defaults());
});

test("reset can be narrowed to one splitter", () => {
  const layout = createLayout({ win: memoryWin(1600) });
  layout.apply({ side: 400, drawer: 500 });
  layout.reset("side");
  assert.equal(layout.wanted.side, SIDEBAR.default);
  assert.equal(layout.wanted.drawer, 500, "the other splitter is untouched");
});

test("a subscriber hears every change, and a broken one does not break the rest", () => {
  const layout = createLayout({ win: memoryWin(1600) });
  const seen = [];
  layout.subscribe(() => {
    throw new Error("boom");
  });
  layout.subscribe((state) => seen.push(state.reading));
  layout.apply({ reading: "wide" });
  layout.apply({ reading: "full" });
  assert.deepEqual(seen, ["wide", "full"]);
});

test("a page with no localStorage still works for this view", () => {
  const win = { innerWidth: 1600 };
  const layout = createLayout({ win });
  assert.doesNotThrow(() => layout.apply({ reading: "wide" }));
  assert.equal(layout.state.reading, "wide");
});

test("a hostile localStorage (privacy mode) neither throws nor loses the choice", () => {
  // 上面那条只覆盖了「没有 localStorage」。隐私模式下**访问**它会抛
  // （getter 抛），配额满了 setItem 会抛 —— 两条路径各有自己的 try。
  const hostile = (mode) => ({
    innerWidth: 1600,
    get localStorage() {
      if (mode === "getter") throw new Error("access denied");
      return {
        getItem: () => null,
        setItem: () => {
          if (mode === "set") throw new Error("quota exceeded");
        },
      };
    },
  });
  for (const mode of ["getter", "set"]) {
    const layout = createLayout({ win: hostile(mode) });
    assert.doesNotThrow(() => layout.apply({ reading: "wide" }), mode);
    assert.equal(layout.state.reading, "wide", `${mode}: the choice still holds for this view`);
  }
});
