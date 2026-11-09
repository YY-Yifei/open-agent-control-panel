# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **An agent's identity is its name, not its URL.** The id is now
  `a_` + `sha256(canonical name)[:16]` (whitespace collapsed, case-folded), so an agent that goes
  offline and comes back on a different host, port or scheme keeps its id — and with it every
  conversation it holds. Re-registering an existing name at a new URL *moves* the entry:
  `url`/`card` follow, the id and the conversations stay, and no fresh `manageToken` is handed
  out. A rename at an already-registered URL answers `409 URL_TAKEN` with `existingId`/`newId`
  instead of silently overwriting the old entry, because under this rule a rename really is a
  different agent. Two trade-offs, accepted deliberately: a repeat registration must fetch the
  card again (the id is not computable before it is read), and a name is a registry-wide unique
  key. Schema 3 → 4 re-keys an existing database on open and re-points `sessions`/`tasks`.

- **Push notifications are now on by default**, and their base URL is derived from the bind
  address the way local mode is: binding loopback (the default) yields
  `http://localhost:PORT/a2a/push`, so an agent on the same machine can always call back.
  `--no-push` (`OACP_PUSH_ENABLED=0`) turns the flow off. The startup banner prints the
  callback base and flags the one case that cannot work — a loopback base on a non-loopback
  bind.

  The demo echo agent now implements the other half: it declares `pushNotifications`, accepts
  the config inline or through `tasks/pushNotificationConfig/set` /
  `CreateTaskPushNotificationConfig`, accepts a task as `working`, finishes it a moment later,
  and `POST`s the finished `Task` to the registered URL.

### Added

- **Agent replies are rendered as Markdown.** A dependency-free module
  (`src/oacp/web/md.js`, no build step, no highlighter) renders headings, lists, fenced and
  inline code, bold/italic, strikethrough, links, images, quotes, rules and tables, and keeps
  single newlines as line breaks. Your own messages and error notices stay plain text.

  The reply is escaped before any tag is added back, and link/image URLs pass a whitelist, so
  a `javascript:` link degrades to its label and a reply containing `<script>` shows up as
  text. Covered by `tests/test_markdown.py`, which drives the browser module from Node and
  asserts the tag whitelist rather than a list of forbidden substrings.

- **The panel is a conversation client now.** The page shows agents, conversations and
  messages; tasks, `contextId` and JSON-RPC method names never reach it. A conversation
  belongs to one agent, keeps its own history, and maps onto A2A as a `contextId` plus one
  task per turn (`src/oacp/sessions.py`). New `/api/panel/sessions` endpoints, and
  `GET /api/panel/agents` also reports `sessionCount` / `lastSessionAt`.

  The raw ledger is unchanged and still served (`/api/panel/tasks*`, with event logs and
  payloads); the interface simply no longer asks anyone to read it. Schema v3 adds the
  `sessions` and `session_messages` tables and `tasks.session_id`, all widened in place on
  first start, so a 0.3 database keeps every row it had.

- **Agents can now report results back, instead of being polled for them.** `--push`
  (`OACP_PUSH_ENABLED`) makes the panel offer a callback URL when a task is still running;
  the agent `POST`s the whole `Task` to `/a2a/push/<token>` and the ledger updates itself.
  This is A2A's own push-notification flow, not an invention: gated on the card's
  `capabilities.pushNotifications`, registered inline with the message and confirmed with
  `tasks/pushNotificationConfig/set` (`CreateTaskPushNotificationConfig` on 1.x cards), with
  the two-dialect fallback the rest of the client already uses. Each task gets its own
  192-bit token, which is the only credential the endpoint accepts; bodies are capped at
  256 KB and must be a Task. Deliveries are idempotent and monotonic — retries change
  nothing, a late `working` cannot overwrite a `completed`, and a callback that omits
  artifacts does not erase the answer already stored. Task records gain `pushState`,
  `pushCount` and `lastPushAt`, and `/api/panel/summary` reports `pushEnabled`. A refused
  registration falls back to the inline copy plus one `GetTask`, so a task never sits on
  `working` waiting for a callback that will not come.

- **Agents can now be removed without the manage token, by the panel's operator.**
  `DELETE /api/panel/agents/{id}` accepts an empty body: if the caller is the operator — the
  panel token was verified, or this is a loopback-only deployment with no panel token — the
  entry is removed and the removal is written to the registration log
  (`outcome = removed-by-operator`). A manage token sent in the body is still checked first,
  exactly as the contract requires, so ownership always wins. A network-exposed panel with no
  token refuses (`401`, `code: manage_token_required`) instead of letting anyone delete
  anyone's entry, and `/v1/agents/{id}` keeps demanding `X-ADP-Token` — the operator authority
  is panel-only. `GET /api/panel/summary` reports the policy as `removal: {operator, reason}`.
- **A remove button on every agent card**, not only on the detail page. The dialog has three
  states: this browser holds the manage token (prefilled), the operator may remove directly
  (with the reason shown), or neither (the token field stays open for pasting, plus an
  explanation of how to become the operator).
- Two panel error codes in the bilingual catalog: `manage_token_required` and
  `manage_token_mismatch` (a wrong token used to surface as the contract's English
  `UNAUTHORIZED`).

### Changed

- The web UI was rewritten around that model: agents on the left, the selected agent's
  conversations beneath them, one chat pane. The task list, the agent detail page and the
  raw-JSON views are gone; three static files, no build step, still.

- **The web UI defaults to Chinese.** Every piece of panel copy — navigation, stat cards,
  tables, empty states, modals, toasts and relative timestamps — now goes through a small
  dictionary in `src/oacp/web/app.js`. Chinese is the default on first visit; the sidebar
  footer switches to English and remembers the choice in `localStorage`. Machine values
  (task states, ids, URLs, raw JSON) and every API payload are untouched.
- **Error messages follow the same switch.** `/api/panel` failures are rendered from a
  bilingual catalog in `src/oacp/errors.py` (`PanelError` + `{"detail": …, "code": …}`), in
  Chinese by default and in English when the caller sends `Accept-Language: en`. The UI
  sends that header for whatever language it is showing, and translates the ADP `/v1`
  contract's stable error codes (and card-validation problems) for display — the protocol's
  own `code`/`message` pair stays English on the wire.

### Fixed

- The demo agent flushes its status lines, so `[self-registration] …` still shows up when its
  output is piped into a file instead of a terminal.
- The test that watches the demo agent re-register no longer races the log line it asserts on:
  the keeper publishes its new registration before it writes the note, so the assertion now
  waits for the note instead of reading once. This was flaky on `main` before any of the above
  (1 failure in 5 runs, measured).

## [0.2.1] — 2026-11-06

### Added

- **The demo agent registers itself.** `examples/echo_agent.py` posts its card URL to the
  panel on startup, retries with backoff while the panel is absent, and registers again when
  a heartbeat `GET /v1/agents/{id}` returns `404`. Start order and panel restarts no longer
  need a manual `curl`. New flags: `--registry`, `--advertise`, `--no-register`,
  `--heartbeat`, `--deregister-on-exit`.
- Tests for the example agent against a real registry: `201`/`200` idempotency, the manage
  token rule, `404` detection, late-panel retry, re-registration and opting out.

## [0.2.0] — 2026-11-06

### Changed

- **Local mode is derived from the bind address.** When `oacp serve` binds a loopback
  address (`127.0.0.0/8`, `::1`, `localhost`), `http` and private card URLs are accepted and
  the A2A client may target private hosts — the same local demo no longer needs
  `--allow-insecure-cards --allow-private-agents`.
- The relaxation is visible, not silent: a startup line, `localMode` in
  `/api/panel/summary` and a banner in the web UI all state that ADP Core compliance is not
  claimed. Explicit flags and `OACP_ALLOW_*` environment variables still win over the default.

### Added

- `--strict`: keep the https/private-address checks even on loopback. Required when a reverse
  proxy or port mapping makes a loopback bind externally reachable.
- `Settings.local_mode` and the `localMode` field in the panel summary.
- Tests for loopback detection, precedence (flag > environment > derived default), and for
  `http` registration being accepted in local mode while still rejected when strict.

## [0.1.0] — 2026-11-06

### Added

- ADP Core v1 registry: `/.well-known/a2a-registry.json`, `POST /v1/agents`,
  `GET /v1/agents`, `GET /v1/agents/{id}`, `DELETE /v1/agents/{id}`.
- Deterministic agent ids from normalized URLs, RFC 8785 `cardHash`, idempotent
  registration and one-time hashed manage tokens.
- SSRF-safe card fetching: https-only, IP validation with connection pinning,
  manual redirects, size and time limits, no environment proxies and mandatory
  TLS verification.
- Background probe loop: success/failure tracking, `lastOkAt` versus `checkedAt`,
  automatic following of card drift with `changedAt`/`prevHash`, and `down` after
  three consecutive failures.
- A2A JSON-RPC client with automatic dialect detection and fallback between the
  0.3 (`message/send`, `tasks/get`, `tasks/cancel`) and 1.x (`SendMessage`,
  `GetTask`, `CancelTask`) wire formats.
- Task ledger: submit, follow up, refresh, cancel, list, filter and forget.
- Panel API under `/api/panel/*` with optional token protection.
- Vanilla HTML/CSS/JS control panel: agent grid and detail views, task list and
  task detail with event log, raw payloads and follow-up messages.
- CLI: `oacp serve` with flags for database, public URL, token, card validation
  mode, probing and development-only network relaxations.
- Development mode (`--allow-insecure-cards`, `--allow-private-agents`) and a
  sample echo agent for local demos.
- Test suite covering URL normalization, card validation, registry behaviour,
  task lifecycle, both A2A dialects and panel security.

[Unreleased]: https://github.com/YY-Yifei/open-agent-control-panel/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/YY-Yifei/open-agent-control-panel/releases/tag/v0.2.1
[0.2.0]: https://github.com/YY-Yifei/open-agent-control-panel/releases/tag/v0.2.0
[0.1.0]: https://github.com/YY-Yifei/open-agent-control-panel/releases/tag/v0.1.0
