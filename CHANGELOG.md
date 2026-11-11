# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/).

## [0.6.0] — 2026-11-11

### Added

- **Agent Native: the panel is a caller.** It can hand its own capabilities to a model
  instead of only being typed at. The browser opens a WebSocket to an
  [aaas](https://pypi.org/project/aaas/) server (`ws://localhost:8888` by default,
  `--aaas-url` to point elsewhere), declares what it can do, and executes those
  functions when the model calls them. The page speaks `aaas.v1` directly — there is no
  second protocol implementation on the server, and no server-side agent loop.
- The caller is the **page**, not the server, on purpose: half of the capabilities worth
  having exist only in a browser. System notifications, the clipboard, a file download,
  scrolling a row into view, writing a draft into the composer so a person can finish it
  and press send. A server-side caller can reach the HTTP API and nothing else.
- **22 declared capabilities and one hidden**, in four tiers: 9 `ui_*` (theme, language,
  notify, toast, open, draft, download, modal, client info), 8 `panel_*` reads (overview,
  search, agents, sessions, tasks), 3 `panel_*` acts (rename, refresh, cancel), 2
  `panel_*` destroys (delete conversation, delete agent). `panel_send_message` is
  declared only under `--agent-policy delegate`: letting a model speak to another agent
  in the user's name is how loops start.
- `GET /api/panel/agent` resolves *one* URL — `wsUrl` — so the page never has to reason
  about transport. With `--aaas-token`, `wsUrl` points at this panel's own
  `/api/panel/agent/ws` proxy, because `new WebSocket()` cannot set an `Authorization`
  header. The token is added upstream and **never** appears in a response body: that
  endpoint is unauthenticated by design, and it answers `enabled: false` when the feature
  is switched off, so an unconfigured panel shows no entry rather than a dead button.
- `GET /api/panel/search` finds agents, conversations and tasks by any word they contain.
  This is the one read the page could not do itself, since it holds a single agent's
  conversations at a time while "which conversation mentioned this" needs the ledger.
  Read-only; `%` and `_` are matched **literally**, and the agent scope re-checks the
  parsed card rather than the raw JSON — matching the raw row would also hit every key
  name, so searching for `name` would return the entire registry.
- **A confirmation gate on the two destructive capabilities.** `panel_delete_session` and
  `panel_delete_agent` open this panel's own `confirmDialog` and wait up to 120 seconds.
  A refusal comes back as `status: "rejected", reason: "denied"`, a timeout as
  `reason: "busy"` — the model is told the truth (nothing happened), never "already
  deleted". The gate is why model output is never parsed as a command: an instruction to
  change the theme is a real function with a JSON Schema, decided by the executor.
- **A dark theme.** The stylesheet had 27 hard-coded colours and two `:root` blocks; they
  are now variables, with `[data-theme="dark"]` alongside a `prefers-color-scheme`
  fallback for readers who have not chosen yet. Theme, like everything else above, is
  reachable as `ui_theme`.
- **One active tab.** Two tabs would each hold their own `sessionId`, transcript and
  declaration, and would tell the person different things about the same conversation
  ("this one says it was deleted, that one says it wasn't"). A `localStorage` lock with a
  3-second heartbeat and a 10-second TTL elects one tab, ordered by `(startedAt, tabId)`
  so simultaneous starts resolve the same way in both. The other tab keeps its button and
  explains itself in a toast instead of looking broken.

### Changed

- `--agent-policy read|act|all|delegate` decides which tiers are declared at all — a
  narrower policy means the functions do not exist for the model, not that they fail when
  called. This is a smaller surface to reason about, and it shows up in the declaration.
- The banner warns when `--aaas-url` points at a plaintext `ws://` on a non-loopback
  host: that socket can drive every capability above.
- **The assistant drawer is a chat, not a log.** Model turns render as Markdown through
  the same renderer the conversation bubbles use — tables, code, links and quotes — instead
  of showing the source. Its input is the conversation's composer (same border, padding,
  autosize ceiling and send button) rather than a bare browser textarea, a turn in flight
  shows one muted 「正在回答…」 line instead of a page that looks stuck, and a reader who
  scrolled up is no longer dragged to the bottom by the next message. The drawer is 400px
  wide, because a table in 360px is a column of single characters. A turn in flight
  can be stopped from the header, and Escape closes the drawer and returns focus to
  the button that opened it.

### Notes

- The feature is **absent when it cannot work**, not degraded: no aaas library, no server
  listening, or `--no-agent` all resolve to `enabled: false` and no UI entry at all.
- `panel_token` travels in the WebSocket query when the proxy is used, because
  `new WebSocket()` cannot send a header. It therefore shows up in a reverse proxy's
  access log — the built-in server does not log requests, but a proxy in front of it
  usually logs the URI. That token also carries operator authority, so it must not be
  reused as any other credential. A one-shot ticket is the fix if this ever matters;
  the shape is already there (`GET /api/panel/agent` would return `?ticket=` instead).
- Conversation transcripts for the drawer live in `localStorage` (the last 12 turns,
  6000 characters), because `historyMode: "stateless"` requires the caller to resend
  context each turn. No table was added to the ledger for this.

## [0.5.0] — 2026-11-11

### Added

- **Sending pictures.** The composer takes up to four images per message (PNG, JPEG,
  WebP, GIF; 5 MB each by default) and forwards them as A2A `Part`s. The bytes travel
  *inline* as base64 rather than behind a hosted URL, because the panel's own address is
  a loopback one by default and an agent that is not on this machine cannot fetch what we
  publish. Both dialects are spelled out — `{"raw", "mediaType", "filename"}` in 1.x,
  `{"kind": "file", "file": {"bytes", "mimeType", "name"}}` in 0.3 — and a text part is
  omitted rather than sent blank when the message is a picture and nothing else.
- The type is decided by the magic bytes, never by the declared `mime`: a PDF labelled
  `image/png` is refused (`415 attachment_type_mismatch`), an SVG is refused outright
  (`415 attachment_unsupported`), and 5 MB is checked on the encoded length before
  anything is decoded.
- Pictures are kept in `session_attachments` (schema 7) and served back through
  `GET /api/panel/attachments/{id}`, so an image you sent is still in the conversation
  after a reload. Bytes stay out of the conversation JSON — the UI asks for each picture
  separately — and a deleted conversation takes its pictures with it.
- An agent whose card declares its input modes and lists no `image/*` is refused with
  `415 agent_no_image_input` before anything is sent. A card that declares nothing is
  still tried: under-declaration is common, and "unknown" is not "no".
- **Receiving pictures.** A file part in an agent's answer — `{"kind": "file", "file":
  {"bytes" | "uri", …}}` in 0.3, `{"raw" | "url", …}` in 1.x — now lands in the
  conversation as a picture, in an `Artifact` or in the status message. Read from both,
  never from `history`: that repeats every earlier message and would re-deliver every
  file on every poll.
- An answer that is **only** a picture is no longer lost. The conversation used to key
  purely on text, so a text-free reply (which is what an image-only answer is) wrote no
  row at all — the bytes sat in `tasks.response_json` where nobody could see them.
- Files are deduped **within a turn** by content hash: A2A `artifacts` accumulate, so the
  second snapshot of a task still carries the first picture. Across turns they are not
  deduped — an agent that sends the same picture again in a new turn is doing something
  a person can see, and swallowing it would be the panel editing the answer.
- A file the panel will not show still leaves a row, so a reply never loses information
  silently: not an image (`unsupported`), over the limit (`too_large`), unreadable base64
  (`bad_data`), unfetchable (`unreachable`), or past the per-message count (`too_many`).
  The row stores the *reason code*; the UI picks the words, so a stored conversation reads
  correctly in either language. `GET /api/panel/attachments/{id}` answers 404 for those
  rows rather than serving an empty body as an image.
- A `url`-shaped file part is fetched through the same guard a card URL goes through
  (public addresses only, DNS pinned after validation, redirects re-checked hop by hop,
  body capped while streaming) and only when the part declares a type or a filename — a
  bare `{"url": …}` is read as a link, exactly as before. When the part names no file but
  the URL path does, the path's last segment becomes the name.
- Receiving has its own limits, because it is a different trust decision from what a person
  types: `max_inbound_image_bytes` and `max_inbound_images` (5 MB / 4 by default). The push
  callback cap became `max_push_bytes` (8 MB by default): a callback carrying one inline
  image is a normal A2A answer, and refusing it meant losing whole turns from agents that
  reply with a picture.

### Changed

- `POST /api/panel/sessions/{id}/messages` (and the message that opens a conversation)
  accepts `attachments: [{mime, name, data}]` beside `text`, and `text` may be empty when
  a picture is attached. It is one request: no upload endpoint, nothing staged, so there
  is no orphaned upload to reap when a send is abandoned.
- `session_attachments` gained `skipped` and `source` (schema 7): `skipped` is NULL while
  the bytes are shown and a reason code when they are not; `source` is the URL a part was
  fetched from, so a `url` part is not downloaded again on every poll. A database written
  by schema 6 widens in place.
- An oversized body is refused in the contract of the surface it arrived on: `/api/panel`
  keeps the panel's `{detail, code}` in the caller's language, and `/v1` answers the ADP
  shape in English (`{code: PAYLOAD_TOO_LARGE, message}`), with a cap of its own (1 MB)
  rather than the panel's image-derived one — an unauthenticated route whose body is a
  URL has no business making the registry parse tens of megabytes.
- A push callback is judged against (text, files) rather than text alone. An agent that
  adds a picture to a task whose state has not moved is saying something new, not retrying
  the previous delivery.

### Fixed

- **A `url`-shaped file part is downloaded once, not once per poll.** Dedupe keyed on the
  bytes, and a `url` part has no bytes until it has been fetched; nothing remembered the
  address, so every snapshot fetched it again (measured: four GETs for one send and three
  syncs — a 5 MB picture, up to 10 s of a thread pool worker, per poll).
  `session_attachments.source` keeps the address and the part is skipped before the fetch.
  Only a row that *has* bytes counts as seen, so an `unreachable` part is tried again on the
  next snapshot instead of disappearing because a CDN blinked.
- **A reply that is only a picture is appended once, not once per poll.** The
  already-there check compared identities that the same turn's intake had just filtered
  out, so it never matched and each snapshot added another copy of the answer.
- **The per-message picture quota does not restart on every snapshot.** It was counted from
  zero each time, so the next poll accepted what the previous one had refused — over the
  limit, and with an extra copy of the reply to go with it. It is now seeded with what the
  turn already displays, and a download attempt counts against it even when it fails, so a
  batch of broken URLs cannot make the panel fetch forever.
- **A body over `max_push_bytes` is answered 413.** FastAPI turns an exception raised while
  parsing a body into a 400, so the panel's own 413 was never reached; the guard now escapes
  that wrapper, and a test drives the real route instead of the helper behind it.
- **A push callback no longer blocks the event loop.** The handler is `async` and called the
  download directly — up to `total_timeout` of a stalled fetch with the whole panel waiting.
  Intake runs in the thread pool now, as the poll path already did.
- **Base64 with legal line breaks is accepted**, `image/jpg` is read as the JPEG alias it
  is, and a `data:` URL may carry parameters — three ways a correct part was answered 400
  or 415.
- **A `too_many` row keeps the file's name and its reason**, and the picture cache no longer
  evicts a picture the page is still showing.
- **`__version__` matches the version the package is published as.** It was left at 0.4.0
  while `pyproject.toml` went to 0.4.1 and 0.4.2, so `--version`, `/health` and the
  `User-Agent` all under-reported. They move together now, and a test fails if they drift
  apart again.
- **`Attachment` is declared before the models that use it.** It sat after `NewSession`,
  which only worked because pydantic rebuilds lazily; `model_json_schema()` on the import
  path would have raised.

## [0.4.2] — 2026-11-10

### Fixed

- **A colliding id never answers with another url's record.** §4.1 makes the id a
  handle for one entry, and the RECOMMENDED derivation only has 64 bits, so two urls
  can in principle mint one id. Registration used to trust the lookup: the row it
  found was returned as "already registered" even when its `url` was a different
  address, and the concurrent-insert path did the same. It now asserts that the row
  is this url and answers `500 ID_COLLISION` otherwise — §10's rule (stop, never
  merge) applies at runtime as well as in the migration.

## [0.4.1] — 2026-11-10

### Changed

- **The spec relaxed `id`; our words follow.** ADP §4.1 (a2a-discover `c0726b6`) no longer
  prescribes how an id is generated. It fixes four properties — URL-safe
  (`^[A-Za-z0-9_-]{1,64}$`), stable within an entry, at most one entry per url, opaque to
  consumers — and demotes the digest derivation to RECOMMENDED. This registry keeps the
  RECOMMENDED derivation and now says so, instead of claiming §4.1 *requires* it. The same
  section states the consumer side: nothing may be parsed out of an id, and two registries
  may give one url different ids, so ids are not a cross-registry correlation key.

### Fixed

- **The schema 4 → 5 migration no longer merges on an id collision** (§10). It treated
  "another row already holds this derived id" as a duplicate and deleted one of the two; §10
  says the merge judgement MUST be the url itself and the pass MUST stop on a 64-bit
  collision. It now raises `MigrationError` and leaves `user_version` at 4, so no entry can be
  lost.

### Added

- A migration collision aborts without dropping either row, and a legally-shaped non-derived
  id (ULID / UUIDv7 / `1`) is answered `404 NOT_FOUND` rather than rejected — ids are handles,
  not a format this registry may dictate.
- A registration whose derived id is already held by another url is refused instead of being
  answered with the other agent's record.

## [0.4.0] — 2026-11-10

### Fixed

- **The registry id is a hash of the URL again** (ADP Core §4.1). 0.3.0 keyed entries on the
  card's name, which the published contract does not allow: an id MUST be
  `a_` + `sha256(normalised url)[:16]`, two spellings of one address MUST converge on one
  entry, and two addresses serving one card MUST stay two entries. `409 URL_TAKEN` and the
  "move" semantics came and went with the name rule; a repeat registration is answered from
  storage again, without a fetch. Schema 4 → 5 re-keys an existing database on open,
  re-points `sessions`/`tasks`, and keeps same-named rows apart.
- **`AgentRecord` and `AgentSummary` match the published schemas exactly.** Both are closed
  objects, and they are disjoint: a full record carries `card` and the probe history but no
  facets; a summary carries `name`/`description`/`skills`/`tags` and no card. The panel's own
  endpoints add the facets the UI displays instead of widening the contract shape.
- **`RegistryCard.spec` points at the published specification** instead of an opaque `urn:`.

### Added

- `tests/test_conformance.py` — validates this registry's responses against the published
  `spec/adp-core.yaml`, so contract drift fails in CI rather than at a consumer.

### Known deployment deviations (opt-in, stated in the banner)

- `--allow-insecure-cards` accepts `http://` and private addresses for loopback development;
  §7 forbids that in production.
- `card_validation = "relaxed"` (the default) also accepts A2A 0.3 cards that advertise a plain
  `url` instead of `supportedInterfaces`. §6.3 requires the newer shape; strict mode is one flag
  away, and the relaxed default is what lets the panel talk to the A2A 0.3 agents that exist.

## [0.3.0] — 2026-11-09

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

[Unreleased]: https://github.com/YY-Yifei/open-agent-control-panel/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/YY-Yifei/open-agent-control-panel/releases/tag/v0.3.0
[0.2.1]: https://github.com/YY-Yifei/open-agent-control-panel/releases/tag/v0.2.1
[0.2.0]: https://github.com/YY-Yifei/open-agent-control-panel/releases/tag/v0.2.0
[0.1.0]: https://github.com/YY-Yifei/open-agent-control-panel/releases/tag/v0.1.0
