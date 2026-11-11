/**
 * The panel's aaas caller — one connection, four outbound methods, one inbound
 * request type (PROTOCOL.md §3 / §5.2).
 *
 * This is deliberately *only* the protocol: no DOM, no fetch, no storage. The
 * capability bodies are injected as `handler`, the UI is reached through
 * callbacks, and `WebSocketImpl` is a parameter. That is what makes the whole
 * thing runnable in Node against a real server (tests/js/live_client.mjs, driven
 * by tests/test_agent_live.py) — the
 * mitigation for the fact that this page is a second implementation of a
 * protocol whose first implementation is Python.
 *
 * Direction table, and it is strict:
 *
 *   we send      aaas/connect · aaas/declareAgent · aaas/sendInput · aaas/cancelTurn
 *   we receive   aaas/invokeFunction (request, MUST answer)
 *                aaas/event (notification) · aaas/cancelCall (notification)
 *
 * A server that sends us a *declaration* method gets -32601, not an `aaas/`
 * code: a specific code would mean "this direction exists, your arguments were
 * wrong", which is exactly the symmetry the protocol removes.
 */

import {
  IdAllocator,
  PROTOCOL_VERSION,
  ParseFailure,
  SUBPROTOCOL,
  encodeError,
  encodeRequest,
  encodeSuccess,
  parseFrame,
} from "./wire.js";

/** Features we ask for. Both are required by the protocol. */
export const REQUIRED_FEATURES = ["functions", "turn.cancel"];

/** Optional: only used when the server says it understands per-turn instructions. */
export const OPTIONAL_FEATURES = ["instructions.turn"];

/** How long a request may hang before we call it dead. `connect` is faster. */
export const REQUEST_TIMEOUT = 30_000;
export const CONNECT_TIMEOUT = 10_000;
export const SEND_TIMEOUT = 20_000;
export const DECLARE_TIMEOUT = 15_000;

/** Backoff for a dropped socket: 1,2,4,8,15,30s, then 30s forever. */
const BACKOFF = [1_000, 2_000, 4_000, 8_000, 15_000, 30_000];

/**
 * Delay before reconnect attempt `attempt` (0-based), with ±20% jitter.
 *
 * Jitter matters when the panel is open in several places or restarted with the
 * server: identical timers would have them all hit the server on the same tick.
 */
export function reconnectDelay(attempt, random = Math.random) {
  const base = BACKOFF[Math.min(Math.max(attempt, 0), BACKOFF.length - 1)];
  const spread = base * 0.2;
  return Math.round(base - spread + random() * spread * 2);
}

/**
 * Turn the server's same-origin path into an absolute `ws(s)://` URL.
 *
 * `new WebSocket()` throws on a relative path, and only the page knows its own
 * origin. Resolving here — rather than on the server from the `Host` header, which
 * a client can rewrite — is what keeps the panel token addressed to this origin,
 * and what makes the scheme follow the page's own behind a TLS-terminating proxy.
 */
export function absoluteWsUrl(url, base = globalThis.location && globalThis.location.href) {
  const raw = String(url || "");
  if (/^wss?:\/\//i.test(raw)) return raw;
  if (!base) return raw;
  try {
    const resolved = new URL(raw, base);
    resolved.protocol = resolved.protocol === "https:" ? "wss:" : "ws:";
    return resolved.toString();
  } catch {
    return raw;
  }
}

const CONNECT_FIELDS = ["sessionId", "idBase", "features", "historyMode", "limits"];

export class AgentClient {
  /**
   * @param {object} options
   * @param {string} options.url            ws(s):// URL, already resolved by the server
   * @param {string} [options.instructions] turn instructions, when supported
   * @param {object} [options.handler]      `{ has(name), execute(call) }`
   * @param {object} [options.callbacks]    status / event / turn / call / diagnostic
   * @param {Function} [options.WebSocketImpl]
   */
  constructor({ url, instructions = "", handler = null, callbacks = {}, WebSocketImpl } = {}) {
    this.url = absoluteWsUrl(url);
    this.instructions = instructions;
    this.handler = handler;
    this.callbacks = callbacks;
    this.WebSocketImpl = WebSocketImpl || globalThis.WebSocket;
    this.ws = null;
    this.ids = new IdAllocator(1);
    this.pending = new Map();
    // callId -> AbortController, so `aaas/cancelCall` reaches the capability body.
    this.calls = new Map();
    this.session = null;
    this.declared = null;
    this.connected = false;
    this._open = false;
    this._text = "";
    this._seq = 0;
    this._turnId = null;
  }

  // ---------------------------------------------------------------- lifecycle

  /** Open the socket, then `connect`. Resolves with the five-field result. */
  async connect() {
    if (typeof this.WebSocketImpl !== "function") {
      throw new Error("websocket_unavailable");
    }
    const ws = new this.WebSocketImpl(this.url, SUBPROTOCOL);
    this.ws = ws;

    await new Promise((resolve, reject) => {
      let settled = false;
      // A proxy that accepts the TCP connection but never completes the upgrade
      // would leave this pending forever — and with it the caller's `connecting`
      // flag, so no retry is ever scheduled and the assistant stays dead.
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        try {
          ws.close(1000, "handshake timeout");
        } catch {
          /* already gone */
        }
        reject(new Error("socket_timeout"));
      }, CONNECT_TIMEOUT);
      const settle = (fn, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        fn(value);
      };
      ws.onopen = () => settle(resolve);
      ws.onerror = () => settle(reject, new Error("socket_error"));
      ws.onclose = (event) => settle(reject, new Error(`socket_closed:${event && event.code}`));
    });

    this._open = true;
    this._attachHandlers(ws);

    let result;
    try {
      result = await this._request(
        "aaas/connect",
        { protocolVersion: PROTOCOL_VERSION, features: [...REQUIRED_FEATURES, ...OPTIONAL_FEATURES] },
        CONNECT_TIMEOUT,
      );
      this._adopt(result);
    } catch (error) {
      // Leave nothing half-open: a rejected `connect` must not leave `_open` true
      // with handlers attached, or a retry on this instance opens a second socket
      // and the new `IdAllocator` starts issuing ids that are still in `pending`.
      this._open = false;
      this.connected = false;
      this._failAll(error instanceof Error ? error : new Error(String(error)));
      try {
        ws.close(1000, "connect failed");
      } catch {
        /* already gone */
      }
      this.ws = null;
      throw error;
    }
    this.connected = true;
    this.callbacks.onStatus?.({ state: "online", session: this.session });
    return this.session;
  }

  _attachHandlers(ws) {
    ws.onmessage = (event) => this._onFrame(event.data);
    ws.onerror = () => {
      /* `onclose` follows; reporting twice would double-count one outage. */
    };
    ws.onclose = (event) => {
      const wasOpen = this._open;
      this._open = false;
      this.connected = false;
      this._failAll(new Error("connection_closed"));
      if (wasOpen) {
        this.callbacks.onStatus?.({
          state: "offline",
          code: event && event.code,
          reason: (event && event.reason) || "",
        });
      }
    };
  }

  /** Validate and adopt `connect.result` — exactly five fields, no more (§3.1). */
  _adopt(raw) {
    const extra = Object.keys(raw).filter((key) => !CONNECT_FIELDS.includes(key));
    if (extra.length) throw new Error(`connect.result has unknown fields: ${extra.join(",")}`);
    const missing = CONNECT_FIELDS.filter((key) => !(key in raw));
    if (missing.length) throw new Error(`connect.result is missing: ${missing.join(",")}`);
    if (!["session", "stateless"].includes(raw.historyMode)) {
      throw new Error(`historyMode must be session|stateless, got ${raw.historyMode}`);
    }
    const offered = Array.isArray(raw.features) ? raw.features : [];
    const missingFeatures = REQUIRED_FEATURES.filter((feature) => !offered.includes(feature));
    if (missingFeatures.length) {
      // Both are protocol requirements; continuing would fail later as a timeout
      // that looks like a network problem instead of a negotiation failure.
      throw new Error(`connect.result is missing required features: ${missingFeatures.join(",")}`);
    }
    this.session = {
      sessionId: raw.sessionId,
      idBase: raw.idBase || {},
      features: Array.isArray(raw.features) ? raw.features : [],
      historyMode: raw.historyMode,
      limits: raw.limits || {},
    };
    // Continue the count rather than restarting it: an id reused across a
    // reconnect can be paired with a response still in flight, and that failure
    // is undetectable — the wrong result is accepted as the right one.
    const base = Number(this.session.idBase.caller);
    if (!Number.isInteger(base) || base < 1) {
      throw new Error(`connect.result idBase.caller must be a positive integer, got ${this.session.idBase.caller}`);
    }
    this.ids = new IdAllocator(base);
  }

  /** Full declaration (§3.2). One call, atomic: it either all lands or none does. */
  async declare(functions, { revision = "" } = {}) {
    const definition = {
      revision: revision || `panel-${functions.length}`,
      instructions: this.instructions,
      functions,
    };
    const result = await this._request("aaas/declareAgent", { definition }, DECLARE_TIMEOUT);
    this.declared = {
      revision: result.revision,
      functions: Array.isArray(result.functions) ? result.functions : [],
    };
    return this.declared;
  }

  /**
   * Start a turn. Returns as soon as the server accepts — the answer arrives as
   * events, not as this response (§3.3).
   *
   * `history` is only sent when the server said `stateless`, and is omitted when
   * it said `session`; sending both would be two truths about one conversation.
   */
  async send(text, history = []) {
    const params = { message: { role: "user", parts: [{ text: String(text) }] } };
    if (this.session && this.session.historyMode === "stateless") {
      params.history = history.map((item) => ({
        role: item.role,
        parts: item.parts.map((part) => ({ text: String(part.text) })),
      }));
    }
    if (this.instructions && this.supports("instructions.turn")) {
      params.instructions = this.instructions;
    }
    const result = await this._request("aaas/sendInput", params, SEND_TIMEOUT);
    this._turnId = result.turnId;
    this._text = "";
    this._seq = 0;
    return result.turnId;
  }

  /** Ask the server to stop a turn. The result only means "accepted" (§3.4). */
  async cancel(turnId, reason = "user") {
    const params = { turnId };
    if (reason) params.reason = reason;
    return this._request("aaas/cancelTurn", params, REQUEST_TIMEOUT);
  }

  /** True when the server listed this feature in `connect.result.features`. */
  supports(feature) {
    return Boolean(this.session && this.session.features.includes(feature));
  }

  close(code = 1000, reason = "") {
    this._open = false;
    this.connected = false;
    this._failAll(new Error("connection_closed"));
    try {
      if (this.ws) this.ws.close(code, reason);
    } catch {
      /* already gone */
    }
    this.ws = null;
  }

  /** Turn id of the turn in flight, if any. */
  get turnId() {
    return this._turnId;
  }

  // ------------------------------------------------------------------ outbound

  _send(frame) {
    if (!this.ws || !this._open) throw new Error("socket_not_open");
    this.ws.send(JSON.stringify(frame));
  }

  _replyError(fid, code, message, data) {
    try {
      this._send(encodeError(fid, code, message, data));
    } catch {
      /* the peer will see its own disconnect */
    }
  }

  _request(method, params, timeoutMs = REQUEST_TIMEOUT) {
    if (!this.ws || !this._open) return Promise.reject(new Error("socket_not_open"));
    const id = this.ids.take();
    return new Promise((resolve, reject) => {
      const timer =
        timeoutMs > 0
          ? setTimeout(() => {
              this.pending.delete(id);
              reject(new Error(`${method}:timeout`));
            }, timeoutMs)
          : null;
      this.pending.set(id, { resolve, reject, timer });
      try {
        this._send(encodeRequest(method, params, id));
      } catch (error) {
        this.pending.delete(id);
        if (timer) clearTimeout(timer);
        reject(error);
      }
    });
  }

  _failAll(error) {
    for (const [id, entry] of this.pending) {
      if (entry.timer) clearTimeout(entry.timer);
      entry.reject(error);
      this.pending.delete(id);
    }
    // Capability bodies too: with no socket left to report to, a destructive body
    // that keeps running is an effect nobody can see or undo.
    for (const [callId, controller] of this.calls) {
      controller.abort();
      this.calls.delete(callId);
    }
  }

  // ------------------------------------------------------------------- inbound

  _onFrame(raw) {
    let parsed;
    try {
      parsed = parseFrame(raw);
    } catch (failure) {
      if (!(failure instanceof ParseFailure)) throw failure;
      this._replyError(failure.id ?? null, failure.code, failure.message, {
        reason: failure.reason,
      });
      this.callbacks.onDiagnostic?.({ kind: "frame.invalid", reason: failure.reason });
      if (failure.closeCode) {
        // A frame we cannot read means we should stop talking, not keep guessing.
        try {
          this.ws.close(failure.closeCode, "parse error");
        } catch {
          /* already gone */
        }
      }
      return;
    }

    if (parsed.kind === "success") {
      const entry = this.pending.get(parsed.id);
      if (!entry) {
        this.callbacks.onDiagnostic?.({ kind: "response.unmatched", id: parsed.id });
        return;
      }
      this.pending.delete(parsed.id);
      if (entry.timer) clearTimeout(entry.timer);
      entry.resolve(parsed.result);
      return;
    }

    if (parsed.kind === "error") {
      const entry = this.pending.get(parsed.id);
      const error = new Error(parsed.error.message || `error ${parsed.error.code}`);
      error.code = parsed.error.code;
      error.data = parsed.error.data;
      if (!entry) {
        this.callbacks.onDiagnostic?.({ kind: "error.unmatched", id: parsed.id });
        return;
      }
      this.pending.delete(parsed.id);
      if (entry.timer) clearTimeout(entry.timer);
      entry.reject(error);
      return;
    }

    if (parsed.kind === "request") {
      if (parsed.method === "aaas/invokeFunction") {
        // Not awaited: a slow capability must not stop the next frame from being
        // read (a cancelCall for *this* call arrives on the same socket).
        this._handleInvoke(parsed.id, parsed.params).catch((error) => {
          this.callbacks.onDiagnostic?.({ kind: "invoke.crashed", message: String(error) });
        });
        return;
      }
      this._replyError(parsed.id, -32601, "METHOD_NOT_FOUND", { method: parsed.method });
      this.callbacks.onDiagnostic?.({ kind: "direction.violation", method: parsed.method });
      return;
    }

    if (parsed.method === "aaas/event") {
      this._handleEvent(parsed.params);
      return;
    }
    if (parsed.method === "aaas/cancelCall") {
      // The server has stopped waiting for this call. Abort the body instead of
      // letting it finish: for a destructive capability, finishing after a cancel
      // means the effect lands anyway.
      const params = parsed.params || {};
      const controller = this.calls.get(params.callId);
      if (controller) controller.abort();
      this.callbacks.onCancelCall?.(params);
      return;
    }
    this.callbacks.onDiagnostic?.({ kind: "method.unknown", method: parsed.method });
  }

  async _handleInvoke(fid, params) {
    const call = params || {};
    const required = ["callId", "turnId", "name", "argumentsRaw", "deadlineMs"];
    const missing = required.filter((key) => !(key in call));
    if (missing.length) {
      this._replyError(fid, -32602, "INVALID_CALL", { missing });
      return;
    }
    if (!this.handler || !this.handler.has(call.name)) {
      // The one legal use of -32010: the model called a name this turn never saw.
      this._replyError(fid, -32010, "FUNCTION_UNKNOWN", { name: call.name });
      return;
    }

    const started = Date.now();
    const controller = new AbortController();
    this.calls.set(call.callId, controller);
    let result;
    try {
      result = await this.handler.execute({ ...call, signal: controller.signal });
    } catch (error) {
      result = {
        status: "error",
        reason: "EXECUTION_FAILED",
        parts: [{ text: String((error && error.message) || error) }],
      };
    }
    this.calls.delete(call.callId);
    const elapsedMs = Date.now() - started;
    this.callbacks.onCall?.({ name: call.name, arguments: call.arguments, result, elapsedMs });
    try {
      this._send(encodeSuccess(fid, { ...result, elapsedMs }));
    } catch {
      /* socket gone — the peer sees its own disconnect */
    }
  }

  _handleEvent(event) {
    if (!event || typeof event !== "object") return;
    if (event.kind === "turn.started") {
      this._text = "";
      this._seq = 0;
    }
    const expected = this._seq + 1;
    if (event.seq !== expected) {
      // seq is continuous from 1 with no gaps. A gap is not something to swallow:
      // the caller would assemble a message that can never be completed.
      this.callbacks.onDiagnostic?.({ kind: "seq.gap", expected, got: event.seq });
    }
    if (Number.isInteger(event.seq)) this._seq = event.seq;

    if (event.kind === "message.delta" && typeof event.text === "string") {
      this._text += event.text;
    } else if (event.kind === "message.completed" && Array.isArray(event.parts)) {
      // Authoritative: on disagreement with the deltas, this one wins (§4.4).
      const text = event.parts.map((part) => (part && typeof part.text === "string" ? part.text : "")).join("");
      // Unconditional: an empty `parts` is the authority saying "nothing", and
      // keeping a possibly-gapped delta buffer instead would invent an answer.
      this._text = text;
    }

    if (event.kind === "turn.completed") {
      if (this._turnId && event.turnId !== this._turnId) {
        // A late completion from an abandoned turn would otherwise be appended to
        // the transcript as the answer to the current question.
        this.callbacks.onDiagnostic?.({ kind: "turn.mismatch", expected: this._turnId, got: event.turnId });
        return;
      }
      const final = {
        turnId: event.turnId,
        status: event.status,
        text: this._text,
        error: event.error || null,
      };
      this._turnId = null;
      this._text = "";
      this._seq = 0;
      this.callbacks.onTurn?.(final);
      return;
    }

    this.callbacks.onEvent?.(event, this._text);
  }
}
