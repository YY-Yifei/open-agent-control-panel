/**
 * aaas wire — the JSON-RPC 2.0 subset this page speaks (PROTOCOL.md §5).
 *
 * Four envelopes and no fifth: request, notification, success, error. This module
 * knows no `aaas/` method — semantics live in `client.js`, which is the same split
 * the SDK keeps between `wire.py` and `caller.py`. Keeping it that way is what
 * lets the framing be tested without a socket or a model.
 *
 * Two failure grades, and the difference is load-bearing:
 *
 *   -32700  not JSON at all  → answer with `id: null`, then close with 1002.
 *   -32600  parses, but is not a legal envelope → answer, **keep the connection**.
 *
 * Closing on the second would turn "the peer has a bug" into "neither side can
 * work". Silence on either would leave a caller waiting for a reply that is never
 * coming — an error nobody can see.
 */

/** Wire subprotocol name (`aaas.SUBPROTOCOL`). */
export const SUBPROTOCOL = "aaas.v1";

/** Protocol major version, carried in `connect.params.protocolVersion`. */
export const PROTOCOL_VERSION = "1";

/** A frame that could not be accepted, with the grade that decides what happens. */
export class ParseFailure extends Error {
  constructor(code, reason, message, id = null) {
    super(`[${code} ${reason}] ${message}`);
    this.code = code;
    this.reason = reason;
    this.id = id;
  }

  /** 1002 only for -32700: a frame we cannot even read ends the conversation. */
  get closeCode() {
    return this.code === -32700 ? 1002 : null;
  }
}

/** A request frame. `id` must be a string, and it is the caller's to choose. */
export function encodeRequest(method, params, id) {
  return { jsonrpc: "2.0", id, method, params };
}

/** A notification frame — **no** `id`, which is what makes it unanswerable by design. */
export function encodeNotification(method, params) {
  return { jsonrpc: "2.0", method, params };
}

/** A success frame. Echo the peer's own id (`s-…`): it is their pending request. */
export function encodeSuccess(id, result) {
  return { jsonrpc: "2.0", id, result };
}

/** An error frame. `id === null` is legal only for -32700. */
export function encodeError(id, code, message, data) {
  const error = { code, message };
  if (data !== undefined) error.data = data;
  return { jsonrpc: "2.0", id, error };
}

/**
 * Per-connection id allocator: prefix by role, strictly increasing, never reused.
 *
 * The prefix is not style. Both sides counting from 1 would collide, and a
 * collision makes "which call does this response belong to" undecidable — the one
 * class of error that cannot be detected, only suffered. `start` comes from
 * `connect.result.idBase.caller` so a reconnect continues the count instead of
 * reusing ids whose responses may still be in flight.
 */
export class IdAllocator {
  constructor(start = 1) {
    const value = Number(start);
    this.next = Number.isFinite(value) && value >= 1 ? Math.floor(value) : 1;
  }

  take() {
    return `c-${this.next++}`;
  }

  /** True when this id was issued by a caller (us) rather than the server. */
  static owns(id) {
    return typeof id === "string" && id.startsWith("c-");
  }
}

const ERROR_CODES = new Set();

function invalid(reason, message, id = null) {
  return new ParseFailure(-32600, reason, message, id);
}

/**
 * Parse one frame into `{kind, id, method, params, result, error}`.
 *
 * Throws `ParseFailure`; never returns half an object. Batch arrays are rejected
 * outright: with one call per frame, a frame *is* a call, so "this frame failed"
 * always maps to "this call failed".
 */
export function parseFrame(raw) {
  let text = raw;
  if (typeof text !== "string") {
    // Browser WebSocket text frames arrive as string; Blob/ArrayBuffer would mean
    // binary framing, which this protocol does not define.
    throw new ParseFailure(-32700, "PARSE_ERROR", `frame is ${typeof text}, not text`);
  }

  let obj;
  try {
    obj = JSON.parse(text);
  } catch (error) {
    throw new ParseFailure(-32700, "PARSE_ERROR", `not JSON: ${error.message}`);
  }

  if (Array.isArray(obj)) throw invalid("ENVELOPE_INVALID", "batch is not accepted");
  if (obj === null || typeof obj !== "object") {
    throw invalid("ENVELOPE_INVALID", `frame must be an object, got ${typeof obj}`);
  }
  if (obj.jsonrpc !== "2.0") {
    throw invalid("ENVELOPE_INVALID", 'jsonrpc must be the string "2.0"', obj.id ?? null);
  }

  const id = obj.id ?? null;
  if (id !== null && typeof id !== "string") {
    // Numbers are legal JSON-RPC and illegal here: a number has no prefix, so it
    // cannot be partitioned between the two directions.
    throw invalid("ENVELOPE_INVALID", `id must be a string, got ${typeof id}`, null);
  }

  const hasMethod = "method" in obj;
  const hasResult = "result" in obj;
  const hasError = "error" in obj;

  if (id === null && hasMethod && "id" in obj) {
    // `"id": null` is an explicit non-string id, not "no id". Silently demoting it
    // to a notification would leave the caller waiting forever, with nobody
    // reporting anything.
    throw invalid("ENVELOPE_INVALID", "a request id must be a string, got null", null);
  }
  if (hasResult && hasError) {
    throw invalid("ENVELOPE_INVALID", "a response may not carry both result and error", id);
  }
  if (!hasMethod && !hasResult && !hasError) {
    throw invalid("ENVELOPE_INVALID", "neither request/notification nor response", id);
  }

  if (hasMethod) {
    const method = obj.method;
    if (typeof method !== "string") throw invalid("ENVELOPE_INVALID", "method must be a string", id);
    const params = obj.params === undefined ? {} : obj.params;
    if (params === null || typeof params !== "object" || Array.isArray(params)) {
      throw invalid("ENVELOPE_INVALID", "params must be an object", id);
    }
    return { kind: id === null ? "notification" : "request", id, method, params };
  }

  if (hasResult) {
    if (obj.result === null || typeof obj.result !== "object" || Array.isArray(obj.result)) {
      throw invalid("ENVELOPE_INVALID", "result must be an object", id);
    }
    return { kind: "success", id, result: obj.result };
  }

  const err = obj.error;
  if (err === null || typeof err !== "object" || Array.isArray(err)) {
    throw invalid("ENVELOPE_INVALID", "error must be an object", id);
  }
  if (!Number.isInteger(err.code)) throw invalid("ENVELOPE_INVALID", "error.code must be an integer", id);
  return { kind: "error", id, error: err };
}

/** Exposed for tests that want to assert the closed set of error codes we emit. */
export const KNOWN_ERROR_CODES = Object.freeze([
  -32700,
  -32600,
  -32601,
  -32602,
  -32010,
  ...ERROR_CODES,
]);
