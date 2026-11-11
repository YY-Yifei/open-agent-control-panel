import test from "node:test";
import assert from "node:assert/strict";

import { AgentClient, reconnectDelay } from "../../src/oacp/web/agent/client.js";

const CONNECT_RESULT = {
  sessionId: "sess-1",
  idBase: { caller: 10, server: 1 },
  features: ["functions", "turn.cancel", "instructions.turn"],
  historyMode: "stateless",
  limits: { maxFrameBytes: 1048576 },
};

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

/** A socket that records what it was told and lets the test speak for the server. */
class FakeSocket {
  constructor(url, protocol) {
    this.url = url;
    this.protocol = protocol;
    this.readyState = 0;
    this.sent = [];
    this.closed = null;
    FakeSocket.last = this;
    queueMicrotask(() => {
      this.readyState = 1;
      this.onopen?.();
    });
  }

  send(data) {
    this.sent.push(JSON.parse(data));
  }

  close(code = 1000, reason = "") {
    const wasOpen = this.readyState === 1;
    this.readyState = 3;
    this.closed = { code, reason };
    if (wasOpen) this.onclose?.({ code, reason });
  }

  /** Deliver a frame from the server. */
  receive(frame) {
    this.onmessage?.({ data: typeof frame === "string" ? frame : JSON.stringify(frame) });
  }

  findBy(method) {
    return this.sent.find((frame) => frame.method === method);
  }

  reply(id, result) {
    this.receive({ jsonrpc: "2.0", id, result });
  }
}

async function connected(options = {}) {
  const client = new AgentClient({ url: "ws://test", WebSocketImpl: FakeSocket, ...options });
  const pending = client.connect();
  await tick();
  const socket = FakeSocket.last;
  const frame = socket.findBy("aaas/connect");
  assert.ok(frame, "connect() must send aaas/connect");
  socket.reply(frame.id, options.connectResult || CONNECT_RESULT);
  await pending;
  return { client, socket };
}

test("the handshake asks for aaas.v1 and adopts exactly five fields", async () => {
  const { client, socket } = await connected();
  assert.equal(socket.protocol, "aaas.v1");
  assert.deepEqual(socket.findBy("aaas/connect").params.features.sort(), [
    "functions",
    "instructions.turn",
    "turn.cancel",
  ]);
  assert.equal(client.session.sessionId, "sess-1");
  assert.equal(client.session.historyMode, "stateless");
  assert.equal(client.supports("turn.cancel"), true);
  assert.equal(client.supports("reasoning"), false);
});

test("ids continue from idBase.caller instead of restarting at 1", async () => {
  const { client, socket } = await connected();
  const declared = client.declare([{ name: "ui_theme", description: "d", parameters: { type: "object", properties: {} }, timeoutMs: 5000 }]);
  await tick();
  const frame = socket.findBy("aaas/declareAgent");
  assert.equal(frame.id, "c-10");
  socket.reply(frame.id, { revision: "r1", functions: ["ui_theme"] });
  const result = await declared;
  assert.equal(result.revision, "r1");
  assert.deepEqual(result.functions, ["ui_theme"]);
});

test("a stateless server gets history; a session server must not", async () => {
  {
    const { client, socket } = await connected();
    const pending = client.send("hi", [{ role: "user", parts: [{ text: "earlier" }] }]);
    await tick();
    const frame = socket.findBy("aaas/sendInput");
    assert.deepEqual(frame.params.history, [{ role: "user", parts: [{ text: "earlier" }] }]);
    assert.deepEqual(frame.params.message, { role: "user", parts: [{ text: "hi" }] });
    socket.reply(frame.id, { turnId: "t-1" });
    assert.equal(await pending, "t-1");
  }
  {
    const { client, socket } = await connected({
      connectResult: { ...CONNECT_RESULT, historyMode: "session" },
    });
    const pending = client.send("hi", [{ role: "user", parts: [{ text: "earlier" }] }]);
    await tick();
    const frame = socket.findBy("aaas/sendInput");
    assert.ok(!("history" in frame.params), "session mode must not carry history");
    socket.reply(frame.id, { turnId: "t-2" });
    await pending;
  }
});

test("instructions ride along only when the server negotiated them", async () => {
  {
    const { client, socket } = await connected({ instructions: "be brief" });
    const pending = client.send("hi");
    await tick();
    assert.equal(socket.findBy("aaas/sendInput").params.instructions, "be brief");
    socket.reply(socket.findBy("aaas/sendInput").id, { turnId: "t" });
    await pending;
  }
  {
    const { client, socket } = await connected({
      instructions: "be brief",
      connectResult: { ...CONNECT_RESULT, features: ["functions", "turn.cancel"] },
    });
    const pending = client.send("hi");
    await tick();
    assert.ok(!("instructions" in socket.findBy("aaas/sendInput").params));
    socket.reply(socket.findBy("aaas/sendInput").id, { turnId: "t" });
    await pending;
  }
});

test("an invoke is executed and answered with the server's own id", async () => {
  const seen = [];
  const { client, socket } = await connected({
    handler: {
      has: (name) => name === "ui_theme",
      execute: async (call) => {
        seen.push(call);
        return { status: "ok", parts: [{ text: "theme=dark" }] };
      },
    },
    callbacks: { onCall: (info) => seen.push(info) },
  });
  const _ = client;
  socket.receive({
    jsonrpc: "2.0",
    id: "s-7",
    method: "aaas/invokeFunction",
    params: {
      callId: "call-1",
      turnId: "t-1",
      name: "ui_theme",
      arguments: { theme: "dark" },
      argumentsRaw: '{"theme":"dark"}',
      deadlineMs: 5000,
    },
  });
  await tick();
  await tick();
  const reply = socket.sent.find((frame) => frame.id === "s-7" && frame.result);
  assert.ok(reply, "the invoke must be answered");
  assert.equal(reply.result.status, "ok");
  assert.deepEqual(reply.result.parts, [{ text: "theme=dark" }]);
  assert.equal(seen[0].name, "ui_theme");
});

test("an unknown function is -32010 and is never executed", async () => {
  const { socket } = await connected({
    handler: { has: () => false, execute: async () => assert.fail("must not run") },
  });
  socket.receive({
    jsonrpc: "2.0",
    id: "s-9",
    method: "aaas/invokeFunction",
    params: {
      callId: "c",
      turnId: "t",
      name: "panel_drop_database",
      argumentsRaw: "{}",
      deadlineMs: 5000,
    },
  });
  await tick();
  const reply = socket.sent.find((frame) => frame.id === "s-9");
  assert.equal(reply.error.code, -32010);
});

test("an invoke missing required fields is -32602, not a guess", async () => {
  const { socket } = await connected({
    handler: { has: () => true, execute: async () => assert.fail("must not run") },
  });
  socket.receive({ jsonrpc: "2.0", id: "s-3", method: "aaas/invokeFunction", params: { name: "x" } });
  await tick();
  assert.equal(socket.sent.find((frame) => frame.id === "s-3").error.code, -32602);
});

test("a server that sends a declaration method gets -32601", async () => {
  const { socket } = await connected();
  socket.receive({ jsonrpc: "2.0", id: "s-1", method: "aaas/declareAgent", params: {} });
  await tick();
  assert.equal(socket.sent.find((frame) => frame.id === "s-1").error.code, -32601);
});

test("events assemble a turn, and the final text comes from message.completed", async () => {
  const turns = [];
  const { client, socket } = await connected({ callbacks: { onTurn: (turn) => turns.push(turn) } });
  const pending = client.send("hi");
  await tick();
  socket.reply(socket.findBy("aaas/sendInput").id, { turnId: "t-9" });
  await pending;

  socket.receive({ jsonrpc: "2.0", method: "aaas/event", params: { turnId: "t-9", seq: 1, kind: "turn.started" } });
  socket.receive({ jsonrpc: "2.0", method: "aaas/event", params: { turnId: "t-9", seq: 2, kind: "message.delta", text: "你" } });
  socket.receive({ jsonrpc: "2.0", method: "aaas/event", params: { turnId: "t-9", seq: 3, kind: "message.delta", text: "好" } });
  socket.receive({
    jsonrpc: "2.0",
    method: "aaas/event",
    params: { turnId: "t-9", seq: 4, kind: "message.completed", parts: [{ text: "你好呀" }] },
  });
  socket.receive({
    jsonrpc: "2.0",
    method: "aaas/event",
    params: { turnId: "t-9", seq: 5, kind: "turn.completed", status: "completed" },
  });

  assert.equal(turns.length, 1);
  assert.equal(turns[0].status, "completed");
  assert.equal(turns[0].text, "你好呀", "message.completed is authoritative over the deltas");
});

test("a broken frame is reported and does not close the connection (-32600)", async () => {
  const diagnostics = [];
  const { client, socket } = await connected({ callbacks: { onDiagnostic: (info) => diagnostics.push(info) } });
  socket.receive("[1,2,3]");
  await tick();
  const reply = socket.sent.find((frame) => frame.error && frame.error.code === -32600);
  assert.ok(reply, "the peer must be told its frame was invalid");
  assert.equal(socket.closed, null);
  assert.equal(client.connected, true);
  assert.ok(diagnostics.some((info) => info.reason === "ENVELOPE_INVALID"));
});

test("unparseable JSON closes with 1002 after answering id:null", async () => {
  const { socket } = await connected();
  socket.receive("not json at all");
  await tick();
  const reply = socket.sent.find((frame) => frame.error && frame.error.code === -32700);
  assert.equal(reply.id, null);
  assert.equal(socket.closed.code, 1002);
});

test("a drop rejects everything in flight instead of hanging", async () => {
  const { client, socket } = await connected();
  const pending = client.send("hi");
  await tick();
  socket.close(1006, "gone");
  await assert.rejects(pending, /connection_closed/);
  assert.equal(client.connected, false);
});

test("backoff grows, is capped, and is jittered", () => {
  assert.equal(reconnectDelay(0, () => 0.5), 1000);
  assert.equal(reconnectDelay(1, () => 0.5), 2000);
  assert.equal(reconnectDelay(5, () => 0.5), 30000);
  assert.equal(reconnectDelay(50, () => 0.5), 30000, "capped, not unbounded");
  assert.ok(reconnectDelay(0, () => 0) < reconnectDelay(0, () => 1), "jitter spreads the retries");
});
