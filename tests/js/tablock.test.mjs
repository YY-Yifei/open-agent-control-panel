import test from "node:test";
import assert from "node:assert/strict";

import { BEAT, OWNER_KEY, TTL, createTabLock, electLeader } from "../../src/oacp/web/agent/tablock.js";

const NOW = 1_000_000;

function fakeStorage() {
  const memory = new Map();
  const storage = {
    writes: 0,
    memory,
    getItem: (key) => (memory.has(key) ? memory.get(key) : null),
    setItem: (key, value) => {
      storage.writes += 1;
      memory.set(key, String(value));
    },
    removeItem: (key) => memory.delete(key),
  };
  return storage;
}

/** A tab lock wired to a fake store, a manual clock and no BroadcastChannel. */
function harness(t, { storage = fakeStorage(), onChange } = {}) {
  t.mock.timers.enable({ apis: ["setInterval"] });
  // A real channel would keep this test process alive after the run.
  const RealChannel = globalThis.BroadcastChannel;
  delete globalThis.BroadcastChannel;
  t.after(() => {
    if (RealChannel) globalThis.BroadcastChannel = RealChannel;
  });
  const clock = { now: NOW };
  const flips = [];
  const lock = createTabLock({
    storage,
    now: () => clock.now,
    onChange: onChange || ((leader) => flips.push(leader)),
  });
  return { lock, clock, flips, storage };
}

test("no holder: take the lock", () => {
  assert.equal(electLeader({ tabId: "a", startedAt: 1 }, null, NOW), true);
});

test("a fresh holder keeps it", () => {
  const beat = { tabId: "a", startedAt: 1, ts: NOW - 500 };
  assert.equal(electLeader({ tabId: "b", startedAt: 2 }, beat, NOW), false);
});

test("a stale holder is replaced, even by a later tab", () => {
  const beat = { tabId: "a", startedAt: 1, ts: NOW - TTL - 1 };
  assert.equal(electLeader({ tabId: "b", startedAt: 2 }, beat, NOW), true);
});

test("two tabs at the same instant agree on the same winner", () => {
  const earlier = { tabId: "a", startedAt: 5, ts: NOW };
  const later = { tabId: "b", startedAt: 6, ts: NOW };
  // The earlier tab keeps it, and the later one yields — both computed from the
  // same heartbeat, so neither can conclude "I win".
  assert.equal(electLeader(earlier, later, NOW), true);
  assert.equal(electLeader(later, earlier, NOW), false);
});

test("the same start time is broken by tabId, deterministically", () => {
  const a = { tabId: "aaa", startedAt: 5, ts: NOW };
  const b = { tabId: "bbb", startedAt: 5, ts: NOW };
  assert.equal(electLeader(a, b, NOW), true);
  assert.equal(electLeader(b, a, NOW), false);
});

test("a heartbeat with no timestamp is treated as dead", () => {
  assert.equal(electLeader({ tabId: "b", startedAt: 2 }, { tabId: "a", startedAt: 1 }, NOW), true);
});

test("a heartbeat from the future is dead too, not fresh forever", () => {
  // Clock rollback after a crashed holder, or a skewed write.
  const beat = { tabId: "a", startedAt: 1, ts: NOW + 60_000 };
  assert.equal(electLeader({ tabId: "b", startedAt: 2 }, beat, NOW), true);
});

test("a page restored from bfcache re-arms the beat with resume()", (t) => {
  const { lock, clock, flips, storage } = harness(t);

  assert.equal(lock.start(), true);
  assert.ok(storage.memory.has(OWNER_KEY), "the holder publishes a beat");

  // `pagehide` while the page is parked: the lock and the beat both go, because
  // the page may never come back.
  lock.release();
  assert.equal(lock.isLeader, false);
  assert.ok(!storage.memory.has(OWNER_KEY));

  // Restored: same page, listeners and channel intact — only the interval is gone.
  lock.resume();
  assert.equal(lock.isLeader, true, "resume() re-claims the lock");
  assert.deepEqual(flips, [true, false, true]);

  clock.now += BEAT;
  storage.writes = 0;
  t.mock.timers.tick(BEAT);
  assert.equal(storage.writes, 1, "exactly one heartbeat, not two");
  assert.equal(JSON.parse(storage.memory.get(OWNER_KEY)).ts, clock.now, "the beat is fresh again");

  // Idempotence: a second pageshow must not stack another interval.
  lock.resume();
  clock.now += BEAT;
  storage.writes = 0;
  t.mock.timers.tick(BEAT);
  assert.equal(storage.writes, 1, "resume() does not double the heartbeat");
});

test("resume() before start() does nothing", (t) => {
  const { lock, storage } = harness(t);
  lock.resume();
  t.mock.timers.tick(BEAT);
  assert.equal(storage.writes, 0, "no beat, no lock, and nothing registered to drive it");
  assert.equal(lock.isLeader, false);
});

test("start() twice does not stack a second heartbeat", (t) => {
  const { lock, clock, storage } = harness(t);
  assert.equal(lock.start(), true);
  assert.equal(lock.start(), true, "the second call reports the same answer");
  clock.now += BEAT;
  storage.writes = 0;
  t.mock.timers.tick(BEAT);
  assert.equal(storage.writes, 1, "one interval, one beat");
});

test("no storage at all: every tab proceeds alone", (t) => {
  // Nothing can coordinate (private mode, or no `localStorage` at all); the
  // fallback is documented, so pin it: leader immediately, announced once, no beat.
  const flips = [];
  const { lock } = harness(t, { storage: null, onChange: (leader) => flips.push(leader) });
  assert.equal(lock.start(), true);
  assert.equal(lock.isLeader, true);
  assert.deepEqual(flips, [true], "the socket opens without ever coordinating");
  // The beat has nowhere to go, so evaluation must stay quiet and still be right.
  t.mock.timers.tick(BEAT);
  assert.equal(lock.isLeader, true);
  assert.deepEqual(flips, [true], "no repeated announcements");
});
