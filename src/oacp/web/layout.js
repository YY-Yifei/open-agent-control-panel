/* layout.js —— 柔性布局：唯一的写入口。
 *
 * 三件东西各有一个旋钮，因为它们的**理由**不同：
 *
 *   --side-w      左栏宽度    ← 人自己拖（200~480）
 *   --reading-w   对话栏宽度  ← 三档：读字 / 看表 / 看 diff
 *   --drawer-w    助手栏宽度  ← 人自己拖（320~640），默认停靠
 *
 * 为什么不合成一个「整体缩放」：人要的是「给内容更多地方」，不是「把字变小」。
 * 缩字会把 12px 的等宽代码缩到看不清，而列表并没变短。
 *
 * 为什么正文要两档宽度（--reading-w 与 --reading-prose）：只把列拉宽会把一行
 * 变成 150 个字符，读起来更累。所以「宽」只给宽内容（表格 / 代码 / 图），
 * 段落仍然停在 760px 的阅读线上。宽度给容器，长度留给句子。
 *
 * 两层状态，务必分清（这是本文件最容易写错的地方）：
 *
 *   wanted   人（或模型）要的。**内存里保留原始请求值**（所以 `apply({side:900})`
 *            之后 `wanted.side` 就是 900）；绝对边界的夹在写盘那一刻做
 *            （`serializeLayout`），视口那一份夹在 `resolveLayout`。
 *   applied  当前视口下真正做到的。按**视口**再夹一次，因为窗口小的时候
 *            800px 的助手栏就没法停靠 —— 但那是显示问题，不该回头改写
 *            wanted，否则在小窗口里打开一次页面，人的 480 就永久变成 320。
 *
 * 于是「窗口缩小时要重新 clamp」与「不要把 clamp 的结果存起来」同时成立。
 */

export const LAYOUT_KEY = "oacp.layout.v1";
export const LAYOUT_VERSION = 1;

export const SIDEBAR = Object.freeze({ min: 200, max: 480, default: 268 });
export const DRAWER = Object.freeze({ min: 320, max: 640, default: 400 });

/** 主区的地板。低于它就不该再往两侧让了 —— 否则对话栏会窄到没有意义。 */
export const MAIN_MIN = 560;

/** 三档对话栏宽度。`full` = 不加盖（聊天区自己的 padding 就是留白）。 */
export const READING_PRESETS = Object.freeze({
  comfortable: 760,
  wide: 1100,
  full: null,
});
export const READING_NAMES = Object.freeze(Object.keys(READING_PRESETS));
export const READING_DEFAULT = "comfortable";

/** 段落、列表、标题的阅读线宽。与「舒适」档同数：宽档变宽的只有表格与代码。 */
export const READING_PROSE = READING_PRESETS.comfortable;

/** 窄到只能上下堆叠（与 styles.css 的 `@media (max-width: 760px)` 同一个数）。 */
export const NARROW_MAX = 760;

function toNumber(value, fallback) {
  const number = typeof value === "string" ? Number(value) : value;
  return typeof number === "number" && Number.isFinite(number) ? number : fallback;
}

function has(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

export function isReadingPreset(name) {
  // 必须走 hasOwnProperty：`READING_PRESETS["toString"]` 是继承来的函数，
  // 一个 `!== undefined` 就足以让 `"toString"` 成为一个合法的档位名。
  return typeof name === "string" && has(READING_PRESETS, name);
}

/** 档位 → CSS 值。`null` 是「不加盖」，在 CSS 里写作 `none`。 */
export function readingCss(name) {
  const width = isReadingPreset(name) ? READING_PRESETS[name] : READING_PRESETS[READING_DEFAULT];
  return width === null ? "none" : `${width}px`;
}

export function readingWidth(name) {
  return isReadingPreset(name) ? READING_PRESETS[name] : READING_PRESETS[READING_DEFAULT];
}

/** 一个全新的默认布局。停靠是默认：那样「窄」与「被盖住」两件事一起消失。 */
export function defaults() {
  return { side: SIDEBAR.default, drawer: DRAWER.default, reading: READING_DEFAULT, docked: true };
}

/**
 * 夹进边界。
 *
 * 第三个别忘了：**坏值的兜底是默认值，不是最小值**。`toNumber(value, min)`
 * 看起来一样，后果却不同 —— 一份读坏的存储、一次 `NaN` 的拖动计算，会把人的
 * 侧栏悄悄压到 200（最小值）而不是回到 268（默认）。夹的是「越界」，
 * 不认识的值该回到中间。
 */
function clampTo(value, bounds, fallback = bounds.default === undefined ? bounds.min : bounds.default) {
  const number = toNumber(value, fallback);
  return Math.round(Math.min(Math.max(number, bounds.min), bounds.max));
}

/**
 * 左栏宽度：先按绝对边界夹，再按视口让路。
 * 视口小到夹不下去时**宁可守住左栏的下限** —— 左栏是导航，窄得看不见比
 * 主区少几像素更糟。
 */
export function clampSide(px, { viewport = 0, drawer = 0, docked = false } = {}) {
  let max = SIDEBAR.max;
  if (viewport > 0) {
    const reserved = docked ? Math.max(0, toNumber(drawer, 0)) : 0;
    max = Math.min(max, viewport - reserved - MAIN_MIN);
  }
  return clampTo(px, { min: SIDEBAR.min, max: Math.max(SIDEBAR.min, max) }, SIDEBAR.default);
}

/** 助手栏宽度：停靠时它挤的是主区，所以要把主区的地板先扣出来。 */
export function clampDrawer(px, { viewport = 0, side = SIDEBAR.min } = {}) {
  let max = DRAWER.max;
  if (viewport > 0) {
    max = Math.min(max, viewport - toNumber(side, SIDEBAR.min) - MAIN_MIN);
  }
  return clampTo(px, { min: DRAWER.min, max: Math.max(DRAWER.min, max) }, DRAWER.default);
}

/**
 * 能不能停靠 —— 由「现有宽度装不装得下」算，不用媒体查询。
 * 两处各自写一个断点，迟早会不一致（一个是 CSS、一个是 JS，改一处忘一处），
 * 所以只有一个真源，就是这个函数。
 */
export function canDock(viewport, side = SIDEBAR.default) {
  if (!(viewport > 0)) return true; // 不知道视口时按「能」处理：默认值是停靠
  return viewport - toNumber(side, SIDEBAR.default) - MAIN_MIN >= DRAWER.min;
}

/**
 * 想要的状态 → 这个视口下真正能做到的状态。
 *
 * 纯函数：不碰 DOM、不读 localStorage。`tests/js/layout.test.mjs` 直接钉它。
 *
 * @param {object} wanted 人（或模型）要的
 * @param {{viewport?: number}} [options]
 * @returns {{side:number, drawer:number, reading:string, docked:boolean, wanted:object,
 *            adjusted:string[]}}
 */
export function resolveLayout(wanted, { viewport = 0 } = {}) {
  const base = defaults();
  const source = wanted && typeof wanted === "object" ? wanted : {};
  const reading = isReadingPreset(source.reading) ? source.reading : base.reading;
  const asksDock = source.docked !== false;

  // 左栏在**任何**模式下都要给主区留出地板：浮层只决定助手栏占不占位，不改变
  // 「主区不能被挤没」这条。以前这一夹只在 docked 分支里做，于是 800px 的窗口 +
  // 480px 的左栏会把主区压到 320px —— 正是这个模块自称要守住的那条线。
  let side = clampSide(source.side === undefined ? base.side : source.side, {
    viewport,
    drawer: 0,
    docked: false,
  });
  let drawer = clampDrawer(source.drawer === undefined ? base.drawer : source.drawer);
  const docked = asksDock && canDock(viewport, side);

  if (docked) {
    // 顺序是「助手栏先让路」，不是反过来。
    //
    // 这个模块一开始就声明左栏（导航）优先，可「先夹 side 再夹 drawer」会把
    // 优先级反过来：`resolveLayout({drawer: 640}, {viewport: 1200})` 会返回
    // side=200（最小值）—— 一个根本没被提到的字段被改了；更糟的是
    // `defaults()` 在 1148~1228 这一段自己就装不下（canDock 只保证助手栏的
    // **下限**放得下），默认布局会静默缩到 ~240px。模型照着 adjusted 说话，
    // 于是会向用户报告一个它从未要求过的左栏变化。
    drawer = clampDrawer(drawer, { viewport, side });
    side = clampSide(side, { viewport, drawer, docked: true });
  }

  const adjusted = [];
  const wantedSide = toNumber(source.side === undefined ? base.side : source.side, base.side);
  const wantedDrawer = toNumber(source.drawer === undefined ? base.drawer : source.drawer, base.drawer);
  if (Math.round(wantedSide) !== side) adjusted.push(`sidebar ${Math.round(wantedSide)}px → ${side}px`);
  if (Math.round(wantedDrawer) !== drawer) adjusted.push(`drawer ${Math.round(wantedDrawer)}px → ${drawer}px`);
  if (asksDock && !docked) adjusted.push("docked → overlay (the window is too narrow to dock)");

  return { side, drawer, reading, docked, wanted: { side: wantedSide, drawer: wantedDrawer, reading, docked: asksDock }, adjusted };
}

/** 宽松读回：任何坏值都退回默认，而不是让下次打开看到一个错位的页面。 */
export function parseLayout(raw) {
  const base = defaults();
  if (raw === null || raw === undefined || raw === "") return base;
  let parsed = raw;
  if (typeof raw === "string") {
    try {
      parsed = JSON.parse(raw);
    } catch {
      return base;
    }
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return base;
  // 版本不对就当没存过。将来的 v2 完全可能把 `reading` 换成像素数、把 `side`
  // 换成比例 —— 静默按 v1 解释会得到一个错位的页面，而「不让人打开就看到
  // 一个错位页面」正是这个函数的全部理由。
  if (parsed.v !== undefined && parsed.v !== LAYOUT_VERSION) return base;
  return {
    side: clampTo(parsed.side, SIDEBAR),
    drawer: clampTo(parsed.drawer, DRAWER),
    reading: isReadingPreset(parsed.reading) ? parsed.reading : base.reading,
    // 只有明确的 `false` 才关掉停靠：一个坏值不该把人从默认状态里踢出去。
    docked: parsed.docked === false ? false : true,
  };
}

/** 写盘用：只存 wanted（绝对夹过的），视口夹过的结果不落盘。 */
export function serializeLayout(wanted) {
  const source = wanted && typeof wanted === "object" ? wanted : {};
  return JSON.stringify({
    v: LAYOUT_VERSION,
    side: clampTo(source.side, SIDEBAR),
    drawer: clampTo(source.drawer, DRAWER),
    reading: isReadingPreset(source.reading) ? source.reading : READING_DEFAULT,
    docked: source.docked !== false,
  });
}

/** `apply()` 认识的字段。多一个都不行 —— 见 apply 里的理由。 */
const FIELDS = Object.freeze(["reading", "side", "drawer", "docked"]);

/** 键盘步进：分隔条上按方向键走多远。`Shift` 走一大步。 */
export const KEY_STEP = Object.freeze({ small: 16, large: 64 });

/**
 * 一条分隔条的键盘语义。
 *
 * @param {string} kind "side" | "drawer"
 * @param {string} key  KeyboardEvent.key
 * @param {number} current 现在的宽度（wanted，不是视口夹过的）
 * @param {boolean} [shift]
 * @returns {number|null} 新的宽度；不归这条分隔条管就返回 null
 */
export function stepped(kind, key, current, shift = false) {
  const bounds = kind === "drawer" ? DRAWER : SIDEBAR;
  const step = shift ? KEY_STEP.large : KEY_STEP.small;
  // 助手栏在右边：分隔条往左拖 = 助手栏变宽。方向反了的话键盘和鼠标会打架。
  const grow = kind === "drawer" ? -1 : 1;
  switch (key) {
    case "ArrowLeft":
      return clampTo(current - step * grow, bounds);
    case "ArrowRight":
      return clampTo(current + step * grow, bounds);
    case "Home":
      return bounds.min;
    case "End":
      return bounds.max;
    default:
      return null;
  }
}

/**
 * 建一个页面上的布局控制器。
 *
 * 没有 DOM 也能建（`doc` 为 null）—— 那是给测试与「在非浏览器环境里读一下
 * 默认值」用的，所有 DOM 操作都被挡在 `if (!root)` 后面。
 *
 * @param {{doc?: object, win?: object}} [options]
 */
export function createLayout({ doc = null, win = null } = {}) {
  const root = doc ? doc.documentElement : null;
  const shell = () => (doc ? doc.querySelector(".app") : null);
  const drawerNode = () => (doc ? doc.querySelector("#agent-drawer") : null);
  const viewport = () => (win && toNumber(win.innerWidth, 0)) || 0;

  const storage = () => {
    try {
      return (win && win.localStorage) || null;
    } catch {
      // 隐私模式下访问 localStorage 本身就会抛，不是「为空」。
      return null;
    }
  };

  const load = () => {
    try {
      const box = storage();
      return parseLayout(box ? box.getItem(LAYOUT_KEY) : null);
    } catch {
      return defaults();
    }
  };

  let wanted = load();
  let applied = resolveLayout(wanted, { viewport: viewport() });
  const listeners = new Set();
  let frame = 0;
  let bound = false;

  function snapshot() {
    return {
      side: applied.side,
      drawer: applied.drawer,
      reading: applied.reading,
      readingWidth: readingWidth(applied.reading),
      docked: applied.docked,
      // 这个视口装不装得下停靠 —— 停靠按钮靠它决定「能不能点」。
      dockable: canDock(viewport(), applied.side),
      viewport: viewport(),
      adjusted: applied.adjusted.slice(),
      wanted: { ...applied.wanted },
    };
  }

  function paint() {
    if (!root) return;
    root.style.setProperty("--side-w", `${applied.side}px`);
    root.style.setProperty("--reading-w", readingCss(applied.reading));
    root.style.setProperty("--drawer-w", `${applied.drawer}px`);
    // 阅读线宽由这里写，而不是在 CSS 里再写一个 760px：它是
    // READING_PRESETS.comfortable 的另一半，两处各写一次迟早会分叉，
    // 表现是「把舒适档改小了，段落却还是老宽度」。
    root.style.setProperty("--reading-prose", `${READING_PROSE}px`);
    root.dataset.reading = applied.reading;
    const box = shell();
    if (box) box.classList.toggle("drawer-docked", applied.docked);
    paintSplitters();
  }

  function paintSplitters() {
    if (!doc) return;
    const side = doc.querySelector("#split-side");
    if (side) {
      side.setAttribute("aria-valuemin", String(SIDEBAR.min));
      // aria 报的必须是**这个视口里真能拖到**的上限：停靠时它是
      // viewport - drawer - MAIN_MIN。报 480 而 End 键只能到 340，等于告诉
      // 屏幕阅读器用户一个走不到的区间。
      side.setAttribute(
        "aria-valuemax",
        String(clampSide(Number.MAX_SAFE_INTEGER, { viewport: viewport(), drawer: applied.drawer, docked: applied.docked })),
      );
      side.setAttribute("aria-valuenow", String(applied.side));
      // 窄屏是上下堆叠，横向的拖拽条没有意义（CSS 也会把它藏掉，
      // 这里同步 aria/orientation，屏幕阅读器才不会报一个不存在的分隔条）。
      const narrow = viewport() > 0 && viewport() <= NARROW_MAX;
      side.setAttribute("aria-orientation", narrow ? "horizontal" : "vertical");
      side.setAttribute("aria-disabled", narrow ? "true" : "false");
    }
    const edge = doc.querySelector("#split-drawer");
    if (edge) {
      edge.setAttribute("aria-valuemin", String(DRAWER.min));
      edge.setAttribute(
        "aria-valuemax",
        String(clampDrawer(Number.MAX_SAFE_INTEGER, { viewport: viewport(), side: applied.side })),
      );
      edge.setAttribute("aria-valuenow", String(applied.drawer));
      // 只有「停靠 + 抽屉打开」才有一条缝可拖；浮层状态下它不存在。
      const drawer = drawerNode();
      const open = Boolean(drawer) && !drawer.hidden;
      edge.hidden = !applied.docked || !open;
    }
  }

  function persist() {
    try {
      const box = storage();
      if (box) box.setItem(LAYOUT_KEY, serializeLayout(wanted));
    } catch {
      /* 配额 / 隐私模式：这次的选择在本页面内仍然有效 */
    }
  }

  function notify() {
    const state = snapshot();
    for (const listener of listeners) {
      try {
        listener(state);
      } catch {
        /* 一个订阅者出错不该让其他订阅者收不到 */
      }
    }
  }

  /**
   * 唯一的写入口。人拖、人按键、模型调用 ui_layout，全部走这里 ——
   * 差别只在 `persist` 与 `wanted` 的合并方式。
   */
  function apply(patch = {}, { persist: save = true } = {}) {
    // 名字写错必须当场报错。这个 bug 真发生过：`ui_layout` 声明的字段叫
    // `sidebar`，控制器内部叫 `side`，中间没有翻译 —— 于是模型每一次「把左栏
    // 调宽」都被静默丢掉，还返回成功。静默丢参数的写入比拒绝更坏：模型会
    // 以为自己改成了，然后照着那个数字往下说。
    for (const key of Object.keys(patch)) {
      if (!FIELDS.includes(key)) {
        throw new Error(`unknown layout field: ${key} (expected ${FIELDS.join(" / ")})`);
      }
    }
    const next = { ...wanted };
    if (has(patch, "reading") && patch.reading !== undefined) {
      if (!isReadingPreset(patch.reading)) {
        throw new Error(`unknown reading preset: ${patch.reading} (expected ${READING_NAMES.join(" / ")})`);
      }
      next.reading = patch.reading;
    }
    // 存的是**请求值**，不夹。夹只有一处 —— resolveLayout —— 于是「你要 900、
    // 实际 480」这件事才能被如实报出来（adjusted）。夹两次的话，第二次看到
    // 的已经是 480，报告就变成「480 → 480」，等于什么都没说。
    for (const key of ["side", "drawer"]) {
      if (!has(patch, key) || patch[key] === undefined) continue;
      // `"480px"` / `null` 以前会悄悄退回**当前值**：写入没发生，却拿到一个
      // 看起来成功的快照。这和「字段名写错」是同一种病 —— 名字写对了、值写坏了
      // 却报成功，模型会照着那个数字往下说。
      // 直接看类型，不走 toNumber：那个函数是**读存储**时故意宽松的
      // （"480" 也认）。写路径上 `"480"` 与 `"480px"` 一个成功一个报错，
      // 是最难解释的不一致。
      const value = patch[key];
      if (typeof value !== "number" || !Number.isFinite(value)) {
        throw new Error(`layout ${key} must be a finite number, got ${String(patch[key])}`);
      }
      next[key] = value;
    }
    if (has(patch, "docked") && patch.docked !== undefined) {
      // 字符串 "false" 是真值 —— `Boolean("false")` 会**开启**停靠。`parseLayout`
      // 早就把字符串当成坏值而不是选择，写路径必须一样严格，否则同一份 payload
      // 从存储来和从 ui_layout 来会有两种结果。
      if (typeof patch.docked !== "boolean") {
        throw new Error(`layout docked must be a boolean, got ${String(patch.docked)}`);
      }
      next.docked = patch.docked;
    }

    wanted = next;
    applied = resolveLayout(wanted, { viewport: viewport() });
    paint();
    if (save) persist();
    notify();
    return snapshot();
  }

  /**
   * 只改**显示**：不动 wanted、不落盘、不通知订阅者。
   *
   * 拖拽用它。理由：拖动中那个宽度只是「试探」，松手之前它还不是人的选择。
   * 以前拖拽走 apply()，而 apply() 会写 wanted —— 于是「一次没动的点击把
   * wanted 里更宽的请求毁掉」这条根本没被修好，moved 守卫在 pointerup 时才
   * 检查，可 wanted 早就在 flush() 里被覆盖了。
   */
  function preview(patch) {
    applied = resolveLayout({ ...wanted, ...patch }, { viewport: viewport() });
    paint();
  }

  /** 复位：整页，或者一条分隔条（双击 / 键盘 Home 之外的那条退路）。 */
  function reset(what = "all") {
    if (what === "side") return apply({ side: SIDEBAR.default });
    if (what === "drawer") return apply({ drawer: DRAWER.default });
    wanted = defaults();
    applied = resolveLayout(wanted, { viewport: viewport() });
    paint();
    persist();
    notify();
    return snapshot();
  }

  /**
   * 视口变了：重新算 applied（不动 wanted、不写盘）。
   *
   * 必须 notify：窗口一缩，`docked` 可能从 true 翻成 false，订阅者（停靠按钮）
   * 不跟着变的话，按钮亮着「停靠中」而实际已经是浮层。
   */
  function refresh() {
    applied = resolveLayout(wanted, { viewport: viewport() });
    paint();
    notify();
    return snapshot();
  }

  /**
   * 一条分隔条。指针事件统一鼠标 / 触屏 / 笔：一套代码，三种设备，
   * 而且 `setPointerCapture` 保证拖到窗口外面也不丢事件。
   */
  function bindSplitter(element, kind) {
    if (!element || !doc) return;
    const key = kind === "side" ? "side" : "drawer";
    let dragging = false;
    let moved = false;
    let startX = 0;
    let startValue = 0;
    let pending = null;
    let dragFrame = 0;

    const flush = () => {
      if (dragFrame && win && win.cancelAnimationFrame) {
        win.cancelAnimationFrame(dragFrame);
        dragFrame = 0;
      }
      if (pending === null) return;
      const value = pending;
      pending = null;
      preview({ [key]: value });
    };

    element.addEventListener("pointerdown", (event) => {
      if (event.button !== undefined && event.button > 0) return;
      dragging = true;
      moved = false;
      startX = event.clientX;
      // 从**看得见的**宽度起拖：wanted 里可能是 9999 这种被夹之前的请求值，
      // 拿它当起点会让鼠标一动就跳。
      startValue = kind === "side" ? applied.side : applied.drawer;
      pending = null;
      // 捕获之后 pointermove 会一直送到这个元素上，哪怕指针已经移出窗口。
      try {
        element.setPointerCapture(event.pointerId);
      } catch {
        /* 老浏览器：退化成普通事件流，拖到窗口外会停，但不会坏 */
      }
      element.classList.add("dragging");
      if (doc.body) doc.body.classList.add("resizing");
      event.preventDefault();
    });

    element.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      const delta = event.clientX - startX;
      // 助手栏在右边：鼠标往左 = 助手栏变宽。
      const next = kind === "side" ? startValue + delta : startValue - delta;
      // 判据是「**屏幕上真会变成多宽**」，用和 resolveLayout 同一套夹法算，
      // 而不是拿绝对边界去比。纯竖直的触摸拖动（`touch-action: none` 只是不许
      // 页面滚，照样送 pointermove）、已经顶在视口边缘还继续推 —— 这两种屏幕上
      // 一个像素都不会变。若把它们当成「动过」，松手时就会把 wanted 里那个被
      // 夹住的更大请求（比如模型设的 drawer=640）永久收敛成当前显示的 372。
      const current = kind === "side" ? applied.side : applied.drawer;
      if (resolveLayout({ ...wanted, [key]: next }, { viewport: viewport() })[key] === current) {
        // 不写 pending：flush() 与 stop() 都只认 pending，于是 wanted 不会被碰。
        pending = null;
        return;
      }
      moved = true;
      pending = next;
      if (dragFrame) return;
      const run = () => {
        dragFrame = 0;
        flush();
      };
      dragFrame = win && win.requestAnimationFrame ? win.requestAnimationFrame(run) : 0;
      if (!dragFrame) flush(); // 没有 rAF（测试环境）就直接算
    });

    const stop = (event) => {
      if (!dragging) return;
      dragging = false;
      // 最后一帧可能被 rAF 取消掉了：先把待显示的值画出来（preview 不动 wanted）。
      if (pending !== null) {
        preview({ [key]: pending });
        pending = null;
      }
      if (dragFrame && win && win.cancelAnimationFrame) win.cancelAnimationFrame(dragFrame);
      // 无条件清零：万一宿主有 requestAnimationFrame 却没有 cancel 的，
      // 留着非零句柄会让 pointermove 永远 early-return（下一次就拖不动了）。
      dragFrame = 0;
      element.classList.remove("dragging");
      if (doc.body) doc.body.classList.remove("resizing");
      try {
        if (event && event.pointerId !== undefined) element.releasePointerCapture(event.pointerId);
      } catch {
        /* 已经释放过了 */
      }
      // 只有**真的拖过**才收敛：一次没有 pointermove 的点击（或 pointercancel）
      // 不该把 wanted 里那个越界的原始请求改写成当前视口夹过的值。否则模型设了
      // drawer=640、窄窗口里显示 440，随手点一下分隔条就把 640 永久弄丢，
      // 窗口变宽也回不来了。
      if (moved) {
        // 真的拖过：这个宽度就是人现在要的，收敛进 wanted 再落盘。
        wanted = { ...wanted, [key]: applied[key] };
      }
      // 无论哪种情况都按 wanted 重算一次：没拖过时把 preview 期间动过的显示
      // 恢复原状，拖过时让 applied 与收敛后的 wanted 对齐。
      applied = resolveLayout(wanted, { viewport: viewport() });
      paint();
      // 每帧都写 localStorage 会卡；一次拖拽只写一次，就在这一刻。
      if (moved) persist();
      notify();
    };

    element.addEventListener("pointerup", stop);
    element.addEventListener("pointercancel", stop);

    // 双击复位：人把栏拖歪了，要有一条不用找菜单的退路。
    element.addEventListener("dblclick", (event) => {
      event.preventDefault();
      reset(key);
    });

    element.addEventListener("keydown", (event) => {
      // 键盘从 **wanted** 起算，不是 applied。用 applied 会有个很难发现的方向
      // 反转：wanted.side=420 在 1300px 窗口里显示 340，按一次「变宽」的 →
      // 会把 356 存下来 —— 一个意思是「加宽」的键把偏好改小了，窗口一放宽
      // 反而显示 356 而不是 420。
      const current = kind === "side" ? wanted.side : wanted.drawer;
      const next = stepped(kind, event.key, current, event.shiftKey);
      if (next === null) return;
      event.preventDefault();
      apply({ [key]: next });
    });
  }

  /** 接上页面：拖拽条、窗口变化、助手抽屉的开合。 */
  function bind() {
    // 幂等：再调一次会挂上第二套 pointer/pointermove 监听，于是一次拖动
    // 走两遍 apply —— 那种 bug 只在拖拽速度上表现出来，很难归因。
    if (!doc || bound) return;
    bound = true;
    bindSplitter(doc.querySelector("#split-side"), "side");
    bindSplitter(doc.querySelector("#split-drawer"), "drawer");

    if (win && win.addEventListener) {
      const onResize = () => {
        // 拖动窗口时每一像素都重算一次是浪费；一帧一次足够。
        if (frame) return;
        const run = () => {
          frame = 0;
          refresh();
        };
        frame = win.requestAnimationFrame ? win.requestAnimationFrame(run) : 0;
        if (!frame) refresh();
      };
      win.addEventListener("resize", onResize);
    }

    // 抽屉开合由 panel.js 直接改 `hidden`，这里不去抢它的开关 —— 只监听。
    // 停靠的那条缝只在抽屉打开时才有意义。
    const drawer = drawerNode();
    if (drawer && win && win.MutationObserver) {
      const observer = new win.MutationObserver(() => paintSplitters());
      observer.observe(drawer, { attributes: true, attributeFilter: ["hidden"] });
    }
  }

  function subscribe(fn) {
    if (typeof fn !== "function") return () => {};
    listeners.add(fn);
    return () => listeners.delete(fn);
  }

  return {
    bind,
    apply,
    reset,
    refresh,
    subscribe,
    get state() {
      return snapshot();
    },
    get wanted() {
      return { ...wanted };
    },
  };
}
