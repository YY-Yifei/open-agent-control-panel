/**
 * One tab talks to the assistant; the others wait their turn.
 *
 * Why not "let every tab connect": each connection is a separate `sessionId`
 * with its own declaration and its own transcript. The model would see two
 * copies of the same 22 capabilities, and the user would see one page report
 * "deleted" while another still lists the conversation. Two truths about one
 * ledger is the failure mode worth 80 lines to avoid.
 *
 * The election is a pure function so it can be tested without a browser:
 * whoever holds the lock keeps it while its heartbeat is fresh; a holder that
 * stopped beating (closed tab, crashed renderer) is replaced; and when two tabs
 * start at the same instant, `(startedAt, tabId)` decides the same way in both,
 * so they cannot both conclude they won.
 */

/** A heartbeat older than this means the holder is gone. */
export const TTL = 10_000;

/** How often the holder refreshes `ts`. */
export const BEAT = 3_000;

export const OWNER_KEY = "oacp.agent.owner";
export const CHANNEL_NAME = "oacp.agent";

/**
 * Should `local` take (or keep) the lock, given the heartbeat currently stored?
 *
 * @param {{tabId: string, startedAt: number}} local
 * @param {{tabId: string, startedAt: number, ts: number}|null} beat
 * @param {number} now
 * @param {number} [ttl]
 */
export function electLeader(local, beat, now, ttl = TTL) {
  if (!beat || typeof beat !== "object") return true;
  const stamp = Number(beat.ts) || 0;
  // A stamp in the future (system clock rollback after the holder died, a skewed
  // write) must not read as "fresh forever": only a real, non-negative age is
  // evidence that someone is still beating.
  const age = now - stamp;
  if (!(age >= 0 && age < ttl)) return true;
  const theirs = Number(beat.startedAt) || 0;
  const mine = Number(local.startedAt) || 0;
  if (mine !== theirs) return mine < theirs;
  return String(local.tabId) < String(beat.tabId);
}

function makeId() {
  const bytes = new Uint8Array(8);
  const cryptoApi = globalThis.crypto;
  if (cryptoApi && typeof cryptoApi.getRandomValues === "function") {
    cryptoApi.getRandomValues(bytes);
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  }
  return Math.random().toString(16).slice(2, 18);
}

function readOwner(storage) {
  try {
    const raw = storage.getItem(OWNER_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function writeOwner(storage, value) {
  try {
    if (value === null) storage.removeItem(OWNER_KEY);
    else storage.setItem(OWNER_KEY, JSON.stringify(value));
  } catch {
    /* private mode / quota: the in-memory answer still stands for this tab */
  }
}

/**
 * The side-effecting layer: heartbeat, release on unload, one channel message.
 *
 * @param {object} [options]
 * @param {(leader: boolean) => void} [options.onChange] fires when leadership flips
 * @param {Storage} [options.storage]
 * @param {number} [options.now] injectable clock, for tests
 */
export function createTabLock({ onChange, storage, now = () => Date.now() } = {}) {
  const store = storage || (typeof localStorage !== "undefined" ? localStorage : null);
  const local = { tabId: makeId(), startedAt: now() };
  let leader = false;
  let started = false;
  let timer = null;
  let channel = null;

  function announce(type) {
    try {
      channel?.postMessage({ type, tabId: local.tabId, startedAt: local.startedAt });
    } catch {
      /* the channel is an accelerator, never the source of truth */
    }
  }

  function claim() {
    writeOwner(store, { tabId: local.tabId, startedAt: local.startedAt, ts: now() });
  }

  function evaluate() {
    if (!store) {
      // No storage at all: nothing can coordinate, so this tab proceeds alone.
      if (!leader) {
        leader = true;
        onChange?.(true);
      }
      return;
    }
    const beat = readOwner(store);
    const mine = beat && beat.tabId === local.tabId;
    const win = mine || electLeader(local, beat, now());
    if (win) {
      claim();
      if (!leader) {
        leader = true;
        onChange?.(true);
      }
      return;
    }
    if (leader) {
      leader = false;
      onChange?.(false);
    }
  }

  function release() {
    if (timer) clearInterval(timer);
    timer = null;
    if (store) {
      const beat = readOwner(store);
      if (beat && beat.tabId === local.tabId) writeOwner(store, null);
    }
    announce("release");
    if (leader) {
      leader = false;
      onChange?.(false);
    }
  }

  /**
   * Re-arm the heartbeat only.
   *
   * `pagehide` clears the interval because the page is going away — but a page
   * restored from the back/forward cache is the same page, with its listeners and
   * channel still registered, so `start()` must not run again. Only the beat is
   * missing; without it the lock goes stale one TTL later and another tab keeps
   * stealing it, reconnecting in a loop.
   */
  function resume() {
    if (!started || timer) return;
    evaluate();
    timer = setInterval(evaluate, BEAT);
  }

  function start() {
    if (started) return leader;
    started = true;
    evaluate();
    timer = setInterval(evaluate, BEAT);
    if (typeof BroadcastChannel === "function") {
      try {
        channel = new BroadcastChannel(CHANNEL_NAME);
        // A holder that just gave up should be replaced now, not in up to 3s.
        channel.onmessage = (event) => {
          if (event?.data?.type === "release") evaluate();
        };
      } catch {
        channel = null;
      }
    }
    if (typeof window !== "undefined" && window.addEventListener) {
      window.addEventListener("pagehide", release);
      window.addEventListener("beforeunload", release);
      // A tab that was in the background may have missed its beats; the storage
      // event is delivered when it wakes up, so this is the cheap truthful check.
      window.addEventListener("storage", (event) => {
        if (event.key === OWNER_KEY) evaluate();
      });
    }
    return leader;
  }

  return {
    tabId: local.tabId,
    start,
    resume,
    release,
    evaluate,
    get isLeader() {
      return leader;
    },
  };
}
