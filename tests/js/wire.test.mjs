import test from "node:test";
import assert from "node:assert/strict";

import {
  IdAllocator,
  ParseFailure,
  encodeError,
  encodeNotification,
  encodeRequest,
  encodeSuccess,
  parseFrame,
} from "../../src/oacp/web/agent/wire.js";

test("four envelopes, and a notification has no id", () => {
  assert.deepEqual(encodeRequest("aaas/sendInput", { a: 1 }, "c-1"), {
    jsonrpc: "2.0",
    id: "c-1",
    method: "aaas/sendInput",
    params: { a: 1 },
  });
  assert.deepEqual(encodeNotification("aaas/event", {}), {
    jsonrpc: "2.0",
    method: "aaas/event",
    params: {},
  });
  assert.deepEqual(encodeSuccess("s-4", { status: "ok" }), {
    jsonrpc: "2.0",
    id: "s-4",
    result: { status: "ok" },
  });
  assert.deepEqual(encodeError("c-2", -32010, "FUNCTION_UNKNOWN", { name: "x" }), {
    jsonrpc: "2.0",
    id: "c-2",
    error: { code: -32010, message: "FUNCTION_UNKNOWN", data: { name: "x" } },
  });
});

test("parse: request, notification, success, error", () => {
  const request = parseFrame('{"jsonrpc":"2.0","id":"s-1","method":"aaas/invokeFunction","params":{}}');
  assert.equal(request.kind, "request");
  assert.equal(request.id, "s-1");
  assert.equal(request.method, "aaas/invokeFunction");

  const notification = parseFrame('{"jsonrpc":"2.0","method":"aaas/event","params":{"seq":1}}');
  assert.equal(notification.kind, "notification");
  assert.equal(notification.id, null);

  const success = parseFrame('{"jsonrpc":"2.0","id":"c-3","result":{}}');
  assert.equal(success.kind, "success");

  const failure = parseFrame('{"jsonrpc":"2.0","id":"c-3","error":{"code":-32601,"message":"x"}}');
  assert.equal(failure.kind, "error");
  assert.equal(failure.error.code, -32601);
});

test("missing params is an empty object, not a rejection", () => {
  const parsed = parseFrame('{"jsonrpc":"2.0","id":"s-1","method":"aaas/invokeFunction"}');
  assert.deepEqual(parsed.params, {});
});

test("not JSON: -32700, id null, and it closes", () => {
  assert.throws(
    () => parseFrame("{oops"),
    (error) => {
      assert.ok(error instanceof ParseFailure);
      assert.equal(error.code, -32700);
      assert.equal(error.id, null);
      assert.equal(error.closeCode, 1002);
      return true;
    },
  );
});

test("legal JSON but not an envelope: -32600, and the connection stays up", () => {
  const cases = [
    "[]",                                                   // batch
    '"just a string"',
    '{"jsonrpc":"1.0","id":"c-1","result":{}}',
    '{"jsonrpc":"2.0","id":7,"result":{}}',                 // id must be a string
    '{"jsonrpc":"2.0","id":null,"method":"aaas/event"}',    // explicit null id on a request
    '{"jsonrpc":"2.0","id":"c-1","result":{},"error":{}}',  // both
    '{"jsonrpc":"2.0","id":"c-1"}',                         // neither
    '{"jsonrpc":"2.0","id":"c-1","method":"m","params":[]}',// params must be an object
    '{"jsonrpc":"2.0","id":"c-1","result":[]}',             // result must be an object
    '{"jsonrpc":"2.0","id":"c-1","error":{"message":"no code"}}',
  ];
  for (const raw of cases) {
    assert.throws(
      () => parseFrame(raw),
      (error) => {
        assert.ok(error instanceof ParseFailure, raw);
        assert.equal(error.code, -32600, raw);
        assert.equal(error.closeCode, null, `${raw} must not close the connection`);
        return true;
      },
    );
  }
});

test("ids: caller prefix, monotonic, and a reconnect continues the count", () => {
  const ids = new IdAllocator(7);
  assert.equal(ids.take(), "c-7");
  assert.equal(ids.take(), "c-8");
  const resumed = new IdAllocator(9);
  assert.equal(resumed.take(), "c-9");
  // A bad idBase must not produce "c-NaN".
  assert.equal(new IdAllocator(undefined).take(), "c-1");
  assert.ok(IdAllocator.owns("c-1"));
  assert.ok(!IdAllocator.owns("s-1"));
  assert.ok(!IdAllocator.owns(null));
});
