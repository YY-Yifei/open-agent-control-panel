# Open Agent Control Panel

**A control panel for A2A agents.** Register them through the A2A Discover Protocol,
see everyone who is online, submit tasks over A2A JSON-RPC, and follow those tasks —
from one small FastAPI service with a dependency-free web UI.

[![PyPI](https://img.shields.io/pypi/v/open-agent-control-panel.svg)](https://pypi.org/project/open-agent-control-panel/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-lightgrey)](LICENSE)
[![Protocol](https://img.shields.io/badge/protocol-ADP%20Core%201.0.0-informational)](https://github.com/YY-Yifei/a2a-discover/blob/main/spec/adp-core.md)

---

## What it does

The panel sits between *you* and a fleet of [A2A](https://a2a-protocol.org) agents. It plays
three roles at once:

| Role | Surface | What it gives you |
|---|---|---|
| **Registry** | `/.well-known/a2a-registry.json`, `/v1/agents` | Agents register themselves by AgentCard URL; the panel fetches, validates and probes the card |
| **Control plane** | `/api/panel/*` + the bundled UI | Every registered agent, its card, its skills, and the tasks you sent it |
| **A2A client** | `message/send`, `tasks/get`, `tasks/cancel` | Submit a task, watch it move, cancel it, continue the conversation |

The registry half implements the **[A2A Discover Protocol (ADP) Core v1](https://github.com/YY-Yifei/a2a-discover/blob/main/spec/adp-core.md)**
contract: five operations, four invariants, and the SSRF rules in §7. The published
[`spec/adp-core.yaml`](https://github.com/YY-Yifei/a2a-discover/blob/main/spec/adp-core.yaml) is the machine-readable source of truth; responses are
validated against its schemas (`tests/test_conformance.py`, which needs the spec checked out
next to this repo or `OACP_ADP_SPEC` pointing at it). The panel half is deliberately outside
that contract — protocol endpoints stay exactly the protocol.

## Why a registry at all

A2A defines how agents talk to each other, but not where to find them. ADP closes that gap
with one claim and nothing more:

> *I fetched this card from this URL at `lastOkAt`, and its digest is `cardHash`. I claim
> nothing else.*

No scores, no trust badges, no ranking, no proxying. Registration is not proof of ownership —
it only proves that the URL served that card at that moment. The panel honours that: it
never invents a "verified" flag, and it never changes a byte of a card.

## Quick start

```bash
pip install open-agent-control-panel
oacp serve
```

Open <http://127.0.0.1:8000>. Anyone can now register an agent:

```bash
curl -X POST http://127.0.0.1:8000/v1/agents \
     -H 'content-type: application/json' \
     -d '{"url":"https://agent.example.com/.well-known/agent-card.json"}'
```

The response is the stored record plus a one-time `manageToken`. Keep it if you want to
unregister later — it is not rotated and cannot be recovered.

Then open the agent in the UI, press **Send task**, and the panel calls the agent's A2A
endpoint with `message/send`.

## Try it locally

A real registration needs a public HTTPS URL, because the registry fetches the card itself.
For a laptop demo, the repository ships a tiny echo agent, and the panel relaxes the checks
on its own once it is bound to loopback:

```bash
pip install -e ".[dev]"

# terminal 1 — the panel (--host defaults to 127.0.0.1, so local mode is automatic)
oacp serve --port 8000

# terminal 2 — a demo agent that finds the panel by itself
python examples/echo_agent.py --port 9100
```

The agent shows up in the UI about a second after it starts — no registration call, no form.
Under the hood it is the protocol doing the work: the agent posts *where its card is*, and the
registry fetches and stores what it fetched. The agent never sends a card body, because a
registry that believed a self-reported card would have nothing to verify.

Order does not matter, and neither does a panel restart. While the panel is away the agent
retries with backoff (1s → 30s) and keeps serving A2A traffic; when a heartbeat `GET` comes
back `404` it simply registers again.

| Flag | Default | Meaning |
|---|---|---|
| `--registry` | `http://127.0.0.1:8000` | panel to register with (`OACP_REGISTRY` also works) |
| `--advertise` | derived from `--host`/`--port` | base URL the panel should use to reach this agent |
| `--no-register` | off | stay a plain agent: serve cards and A2A, never call the panel |
| `--heartbeat` | `30` | seconds between liveness checks; `0` registers once and stops |
| `--deregister-on-exit` | off | `DELETE` the entry on shutdown, with the manage token |

The same registration by hand is one request:

```bash
curl -X POST http://127.0.0.1:8000/v1/agents \
     -H 'content-type: application/json' \
     -d '{"url":"http://127.0.0.1:9100/.well-known/agent-card.json"}'
```

**Local mode** is derived from the bind address, not from a flag you have to remember. When
`--host` is a loopback address, `http://` and private/loopback **card URLs** are accepted and
the A2A client may call private endpoints. The panel says so at startup, in
`/api/panel/summary` (`localMode: true`) and in a banner in the UI — because such a
deployment does not claim ADP Core compliance, which requires `https`.

Bind anything else (`--host 0.0.0.0`, a LAN or public address) and the strict checks apply
in full. Two knobs override the derivation:

| Knob | Effect |
|---|---|
| `--allow-insecure-cards` / `--allow-private-agents` | turn one guard off explicitly, on any bind address |
| `--strict` | never relax, even on loopback |

> Behind a reverse proxy or a container port map the panel can be bound to loopback and still
> be reachable from outside, and loopback is no longer evidence of locality. In that case pass
> `--strict` and protect the panel with `--panel-token`.

## What the UI gives you

The interface is a conversation client, not a protocol console. A2A's tasks, `contextId`,
method names and push tokens stay behind `/api/panel`; the page itself only ever talks about
**agents, conversations and messages**.

* **Agents** — the list on the left, each with a live/offline dot and how many conversations
  it has. Adding one is a single field: the agent card URL (agents that register themselves
  appear without it).
* **Conversations** — pick an agent and the panel opens its most recent conversation, or an
  empty one if there is none. Each conversation keeps its own history and its own context on
  the agent, so coming back tomorrow continues the same thread.
* **Messages** — you write, the agent answers. While it works the composer waits and a *stop*
  button appears; a turn that asks for more input turns the composer back on.
* **Pictures** — the paperclip takes up to four images per message (PNG, JPEG, WebP, GIF,
  5 MB each by default), previews them above the composer and sends them as A2A file parts.
  An agent whose card lists its input modes without any `image/*` gets no paperclip at all,
  and the server answers `415 agent_no_image_input` if one is forced through anyway.
  Pictures *from* an agent are shown too, including an answer that is nothing but an image —
  inline `bytes`/`raw` directly, a `url`/`uri` through the same guard a card URL goes through
  (off by default only because a panel with no outbound access still has to show the reply).
  A file the panel will not put on screen still leaves a chip saying why: not an image, too
  large, unreadable, unfetchable, or past the per-message count. Nothing is dropped quietly.
* **Six states** — 空闲 / 正在处理 / 等待回复 / 已完成 / 出错 / 已停止. Nothing else is shown,
  and no protocol word reaches the page.
* **Removing an agent** — the dialog uses the manage token from first registration when this
  browser holds one. Otherwise the panel removes on *operator authority*: a verified panel
  token, or a loopback-only deployment with no panel token. A network-exposed panel without a
  token answers `401 manage_token_required` rather than letting anyone delete entries, and ADP
  `/v1` keeps its own rule (`X-ADP-Token`) regardless. Conversations and the task ledger stay
  readable — their rows carry the agent's name and URL.
* **Zero build step** — the front end is three static files (`index.html`, `styles.css`,
  `app.js`), served by the same process. No Node, no bundler, no CDN.
* **Chinese by default** — the interface ships in Chinese; the sidebar footer toggles to
  English and the choice is remembered per browser. Error text follows the same switch:
  `/api/panel` messages come from a bilingual catalog (`src/oacp/errors.py`, picked through
  `Accept-Language`, Chinese by default) and always carry a stable `code`; ADP `/v1` errors
  keep their English `code`/`message` contract, which the UI shows verbatim.

## Agent Native

The panel can also be *driven*: an external model calls the panel's own functions
instead of only typing into it. The caller is the **browser**. It opens a WebSocket to an
[aaas](https://pypi.org/project/aaas/) server, declares what it can do, and executes those
functions when the model asks. The panel does not embed an agent loop, and the protocol
lives in `web/agent/`, not on the server.

The page is the caller because half of the useful capabilities only exist in a page:
system notifications, the clipboard, a download, scrolling a row into view, writing a
draft into the composer so you can finish it and press send. A server-side caller can
reach the HTTP API and nothing else — and the panel *is* mostly the HTTP API, so that
would buy a shell around `fetch`.

### Connecting

```bash
oacp serve                                  # ws://localhost:8888 by default
oacp serve --aaas-url ws://127.0.0.1:9999   # somewhere else
oacp serve --no-agent                       # no entry in the UI at all
```

Nothing to install on this side. If the socket cannot be opened — no server listening, or
no `aaas` library — the entry does not appear. An absent feature is honest; a button that
cannot work is not.

`GET /api/panel/agent` answers with the one URL the page should dial. With
`--aaas-token` it points at this panel's `/api/panel/agent/ws` instead, a byte pump that
adds the `Authorization` header the browser cannot set. The token never leaves the
process, and never appears in a response body.

### What the model can call

| Tier | Count | Examples |
| --- | --- | --- |
| `ui_*` | 12 | `ui_theme`, `ui_notify`, `ui_open`, `ui_draft`, `ui_download`, `ui_modal` |
| `panel_*` read | 10 | `panel_overview`, `panel_search`, `panel_get_agent`, `panel_db_query` |
| `panel_*` act | 3 | `panel_rename_session`, `panel_refresh_agent`, `panel_cancel_session` |
| `panel_*` destroy | 2 | `panel_delete_session`, `panel_delete_agent` |

`--agent-policy` decides which tiers are *declared*: `read` (21), `act` (25, the default),
`all` (27), `delegate` (28). A capability outside the policy does not exist for the model —
it is not declared, so it cannot be called and cannot be dialectically talked around.

`panel_send_message` — speaking to another agent in your name — is only declared under
`delegate`. It is off by default because that is how two agents end up in a loop.

`panel_db_schema` / `panel_db_query` are the two exceptions to "a capability answers by id":
they answer questions whose shape is not known in advance (which agent fails most, how the
failure rate moved this week, which host registered and never ran a task). One says what is in
the database, the other runs a single read-only statement. Both are `read` because the server
opens the file with `mode=ro` + `PRAGMA query_only` — a write fails inside SQLite, not by
convention — and credential columns are masked on the way out. See
[docs/db-cookbook.md](docs/db-cookbook.md) for the queries worth stealing.

### Two things the model may not do

Deleting goes through the panel's own confirmation dialog, with a 120-second deadline. A
refusal comes back as `rejected`/`denied`, a timeout as `rejected`/`busy`, and the model
is told that **nothing was deleted**. It never gets to report a deletion that did not
happen.

Only one tab is connected at a time. Two tabs would each hold their own transcript and
their own view of the same conversation, and would disagree in front of the person using
them. A second tab keeps its button and says so in a toast.

### Search

`GET /api/panel/search?q=&scope=&limit=` spans agents, conversations and tasks. It is the
one read the page could not do itself: the page holds a single agent's conversations,
while "which conversation mentioned this" needs the ledger. It is read-only, treats `%`
and `_` as literal characters, and re-checks the parsed card for the agent scope so that
searching for `name` does not return every agent whose JSON contains that key.

## Protocol surface

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/.well-known/a2a-registry.json` | RegistryCard (name, apiBase, spec, probe interval, limits) |
| `POST` | `/v1/agents` | `{"url": …}` → fetch, validate, store; `201` + one-time `manageToken` |
| `GET` | `/v1/agents` | `?q=&skill=&tag=&status=&changedSince=&full=&limit=&cursor=` |
| `GET` | `/v1/agents/{id}` | One AgentRecord, including the card |
| `DELETE` | `/v1/agents/{id}` | Unregister; requires `X-ADP-Token` |

Records follow the contract exactly: `cardHash` is `sha256(JCS(card))`, `checkedAt` moves on
every attempt, `lastOkAt` only on success, and `changedAt`/`prevHash` are always present
(nullable) so clients need one branch, not two.

### Identity is the host, the path and the card name; the id is only a handle

ADP §4.1 leaves the id's *algorithm* to the implementation and fixes four properties instead:
URL-safe (`^[A-Za-z0-9_-]{1,64}$`), stable within an entry, at most one entry per url, and
opaque to consumers. This registry keys an entry on the agent's **host, the path it is served
at, and its card name**, deriving `a_` + the first 16 hex characters of
`sha256(host \n path \n name)`. Names are folded first — NFC, whitespace collapsed,
case-folded — so a cosmetic edit is not a different agent.

The **port is not in the key**, and that is the point. A locally-run agent restarts on another
port several times a day; keyed by URL, every restart was a *new agent*, with the conversations
pointing at the old one suddenly unreachable. Keyed by host, path and name, the agent that comes
back is the same entry, with its id, its token and its conversations.

The **path stays** because it is what separates two agents on one shared hostname. Behind a
reverse proxy a registry sees one host and many paths: the port discriminates nothing there and
the path always does. Folding paths together would let one agent's registration move another's
entry — and, with it, another's conversations — onto a different agent.

All four §4.1 properties still hold: one url normalises to exactly one `(host, path, name)`, so
`url -> id` is still a function and "at most one entry per url" is untouched. What changed is the
converse, which the contract never required: one id may now cover several urls — one agent seen
on successive ports — and the registry resolves that by treating a re-registration under a new
url as the same entry moving.

Normalisation of the URL is unchanged and still minimal — lower-case scheme and host, drop a
default `:443`, upper-case percent escapes — so `HTTPS://Geo.Example.com:443/Card.json` and
`https://geo.example.com/Card.json` are one entry, while `…/Card.json` and `…/Card.json/` are
two. Nothing else is rewritten: no trailing slash, no `..` collapsing, no query sorting.

| Situation | Result |
|---|---|
| Same URL, spelled differently | `200`, one entry |
| Same URL, same spelling | `200`, the stored record, token never rotated |
| Same card on two **hosts**, or at two **paths** | `201`, **two** entries |
| Same host, path and name, new **port** | `200`, the entry **moves**: id, `firstSeenAt` and token stay |
| Renamed card at a stable URL | `200`, `changedAt`/`prevHash` move; the id does **not** |

Three costs, stated rather than hidden:

* **A repeat registration costs a fetch.** The name lives in the card, so the entry cannot be
  located without retrieving it; before, storage alone answered. Re-registering an agent that
  is *offline* therefore fails like any other unreachable URL.
* **Within one host and path the name decides.** Two differently-named agents there are two
  entries; two identically-named ones are one, and the second address moves the entry.
* **A move requires the old address to be dead.** Registering the same name on a new port moves
  the entry only once the previous url has stopped answering — a restart leaves it dead, which is
  the case this exists for. While the old address still serves that card, the answer is
  `409 AGENT_STILL_AT_OLD_URL`: that is a second agent with the same name, not a move.

An id is a handle that stays put: it is derived once, when the entry is born, and never
recomputed. A rename updates the card and leaves the id — and the conversations — alone, which
is why a renamed agent's id still spells its first name's digest. The id says nothing about
*who* an agent is, and nothing may be read out of it — no url, no host, no timestamp, no
ordering. ADP is explicit that identity comes from the A2A card's signature, and a registry that
cannot prove ownership has no business minting it. The same url gets a different id at another
registry, so ids are not a cross-registry correlation key.

A database written by any release before 0.9 is re-keyed once, on open (schema 7 → 8): ids are
recomputed from each entry's stored host and card name, and `sessions.agent_id` /
`tasks.agent_id` are re-pointed. Rows for one agent across several ports — exactly what a file
written over several restarts holds — cannot all take the same id, and merging would throw away
a manage token, so nothing is deleted: the most recently successful row takes the derived id
and the others take `<id>_2`, `<id>_3`, … . Should two rows from *different* hosts and names
derive one id (§4.1's 64-bit caveat), the pass stops and refuses to merge: §10 says the
judgement is the row, never the id, and the alternative is losing a real entry.

Panel endpoints (not part of ADP). The conversation surface is what the UI uses; the
`tasks` rows underneath are kept for automation and debugging:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/panel/summary` | Counts, deployment facts, RegistryCard |
| `GET` | `/api/panel/agents` | Agents with local task counts |
| `GET` | `/api/panel/agents/{id}` | Record + interfaces + recent tasks |
| `POST` | `/api/panel/agents/{id}/refresh` | Probe this agent now |
| `DELETE` | `/api/panel/agents/{id}` | Unregister: `{"token": …}` (owner) or empty body (operator) |
| `GET` | `/api/panel/sessions` | Conversations, newest activity first (`agentId`, `limit`) |
| `POST` | `/api/panel/agents/{id}/sessions` | Open a conversation, optionally with `{"text": …}` |
| `GET` | `/api/panel/sessions/{id}` | One conversation with its messages |
| `POST` | `/api/panel/sessions/{id}/messages` | Send `{"text": …}`; answers `working` when the agent will report back |
| `POST` | `/api/panel/sessions/{id}/sync` | Ask the agent for the current turn (`tasks/get`) |
| `POST` | `/api/panel/sessions/{id}/cancel` | Stop the running turn (`tasks/cancel`) |
| `PATCH` | `/api/panel/sessions/{id}` | Rename: `{"title": …}` |
| `DELETE` | `/api/panel/sessions/{id}` | Forget the conversation; the task rows stay |
| `POST` | `/api/panel/agents/{id}/tasks` | One message over A2A (what the conversations use) |
| `GET` | `/api/panel/tasks` | Local task ledger (`agentId`, `state`, `q`, `limit`, `cursor`) |
| `GET` | `/api/panel/tasks/{id}` | Task + event log + raw payloads |
| `POST` | `/api/panel/tasks/{id}/refresh` | `tasks/get` against the agent |
| `POST` | `/api/panel/tasks/{id}/cancel` | `tasks/cancel` against the agent |
| `POST` | `/api/panel/tasks/{id}/messages` | Follow-up message in the same task/context |
| `DELETE` | `/api/panel/tasks/{id}` | Forget the task locally |

## Two A2A generations

Agents in the wild speak two dialects. The client reads the card, picks one, and — if the
agent answers *method not found* or *invalid params* — retries once with the other:

| Card shape | Methods | Message shape |
|---|---|---|
| `url` + `protocolVersion: 0.x` | `message/send`, `tasks/get`, `tasks/cancel` | `role: "user"`, parts tagged `kind: "text"` |
| `supportedInterfaces` / `protocolVersion: 1.x` | `SendMessage`, `GetTask`, `CancelTask` | `role: "ROLE_USER"`, parts as `{text}` |

Both responses are normalized to the same internal view, including task states such as
`TASK_STATE_INPUT_REQUIRED` → `input-required`.

## Conversations on top of A2A

A2A has no conversation object. It has a `contextId` that ties tasks together, and a
`message/send` that answers with a `Task`. The panel keeps the shape its users need in
`sessions` / `session_messages` (`src/oacp/sessions.py`) and maps it like this:

| Panel | A2A |
|---|---|
| a conversation | `sessions.context_id`, assigned by the agent on the first turn |
| a message you send | `SendMessage` — a follow-up into the open task while there is one, a fresh send carrying the same `contextId` once the turn is finished |
| a picture you attach | a file `Part` in that same message — `{"raw", "mediaType", "filename"}` in 1.x, `{"kind": "file", "file": {"bytes", "mimeType", "name"}}` in 0.3 |
| the agent's answer | the task's text, folded into one message |
| 正在处理 / 等待回复 | `working` / `input-required`, `auth-required` |
| 已停止 | `canceled` |

Everything that can change a task — the send's own result, a `GetTask` through
`POST /api/panel/sessions/{id}/sync`, and a push callback — goes through the same `_absorb`
step, so a conversation cannot show two different truths depending on which way the update
arrived. A repeated snapshot is stored once, compared per turn (two turns may legitimately
answer with the same words), and a finished turn is a one-way door: a late `working` never
overwrites it.

`GET /api/panel/tasks*` still serves the raw ledger, with event logs and payloads, for
whoever is debugging an agent. The interface does not ask anyone to read it.

### Rendered replies

Agent replies are Markdown, rendered in the browser by a small module with no dependencies
and no syntax highlighting (`src/oacp/web/md.js`). Headings, lists, fenced and inline code,
bold/italic, strikethrough, links, images, quotes, rules and tables are supported. A single
newline stays a line break, because models use them to break lines.

Messages you send stay plain text, and so do error notices: typing `# heading` should not
silently become a heading, and an error string is already a finished sentence.

The reply is escaped before any tag is added back, and link/image URLs pass a whitelist
(`http(s)`, `mailto`, same-origin paths). A `javascript:` link degrades to its label instead
of becoming a link, and a reply containing `<script>` shows it as text — a model's output is
untrusted input that ends up in `innerHTML`.

## Push notifications (the agent calls back)

A2A has exactly one way for an *agent* to speak first, and the panel uses it by default:
while a task is running it offers the agent a URL to report to, and the agent `POST`s the
whole `Task` there whenever it moves.

* **capability gate** — the card's `capabilities.pushNotifications`. Without it the panel
  offers nothing and records why (`pushState: "unsupported"` on the task).
* **registration** — the `{url, token}` config rides inline with the message
  (`configuration`) and is then confirmed with `tasks/pushNotificationConfig/set`
  (0.3) or `CreateTaskPushNotificationConfig` (1.x).
* **delivery** — the agent `POST`s the whole `Task` to `/a2a/push/<token>`; the panel stores
  it, so the task on screen is current without anyone pressing refresh.
* **the token is the credential** — 192 bits minted per task. It buys exactly one thing:
  updating that one task. An unknown or mismatched token is refused, as is a body over
  256 KB or one that is not a Task.
* **idempotent and monotonic** — a retry changes nothing, and a late `working` cannot
  overwrite a `completed` we already hold. A callback that omits artifacts does not erase
  the answer already stored.

Only running tasks are subscribed to: one that finishes inside the `SendMessage` call is
recorded as `settled` and never offered a URL. If registration is refused, the inline copy
may still work, so the panel asks once (`GetTask`) instead of leaving the task stale.

Where that URL points is derived, the same way local mode is: bind loopback (the default)
and the callback base is `http://localhost:8000/a2a/push`; `--public-url` overrides it, and a
wildcard bind falls back to `127.0.0.1`. On one machine the whole loop therefore works out of
the box, demo echo agent included.

Reachability is the one real prerequisite, and it is a network fact rather than a flag: an
agent on another host can only POST to an address it can route to. `--public-url` is what
turns a demo into a deployment; the startup banner prints the callback base and says so when
it is a loopback address on a non-loopback bind. `--no-push` (`OACP_PUSH_ENABLED=0`) turns the
flow off, and `tasks/get` remains the only way a task is updated.

## Configuration

Every setting has an environment variable (`OACP_*`) and most have a flag. Precedence is
default < environment < flag.

| Flag | Variable | Default | Meaning |
|---|---|---|---|
| `--db` | `OACP_DB_PATH` | `oacp.db` | SQLite file |
| `--public-url` | `OACP_PUBLIC_URL` | `http://127.0.0.1:8000` | External base URL, advertised in the RegistryCard |
| `--name` | `OACP_DISPLAY_NAME` | `Open Agent Control Panel` | Registry display name |
| `--contact` | `OACP_CONTACT` | — | Contact line in the RegistryCard |
| — | `OACP_SPEC_URL` | `urn:adp:core:1.0.0` | Identifier of the protocol spec |
| `--panel-token` | `OACP_PANEL_TOKEN` | — | Require this token on `/api/panel/*` |
| `--card-validation` | `OACP_CARD_VALIDATION` | `relaxed` | `strict` = ADP §6.3 exactly; `relaxed` also accepts A2A 0.3 cards |
| `--probe-interval` | `OACP_PROBE_INTERVAL` | `300` | Seconds between probes for healthy agents |
| — | `OACP_DOWN_PROBE_INTERVAL` | `60` | Seconds between probes for `down` agents |
| `--no-probe` | `OACP_PROBE_ENABLED` | `true` | Disable the background probe loop |
| — | `OACP_PROBE_CONCURRENCY` | `4` | Concurrent probes |
| — | `OACP_MAX_CARD_BYTES` | `1048576` | Card size cap |
| — | `OACP_MAX_AGENTS_PER_DOMAIN` | `100` | Per-host entry cap |
| — | `OACP_REGISTER_PER_HOUR` | `20` | Per-IP registration attempts per hour |
| `--allow-insecure-cards` | `OACP_ALLOW_INSECURE_CARDS` | `false` | **Dev only:** accept http/private card URLs |
| `--allow-private-agents` | `OACP_ALLOW_PRIVATE_AGENTS` | `false` | **Dev only:** call private A2A endpoints |
| `--a2a-timeout` | `OACP_A2A_TIMEOUT` | `60` | A2A request timeout in seconds |
| `--no-push` | `OACP_PUSH_ENABLED` | `true` | Offer agents a callback URL (`--push`, the default; this turns it off) |
| `--host` / `--port` | — | `127.0.0.1` / `8000` | Bind address |

## Security model

The panel makes requests to URLs chosen by strangers, so the outbound path is the product.

* **https only** for cards; userinfo and fragments are rejected.
* **DNS pinning** — the resolved address is validated and then pinned into the connection
  (`Host` header + TLS SNI preserved), so a name cannot resolve to one address at check time
  and another at connect time. Mixed public/private answers are refused outright.
* **Manual redirects**, at most three hops, each re-validated from scratch.
* **Streaming body cap**, connect/total timeouts, no cookies, no caller credentials,
  TLS verification that cannot be switched off, and `trust_env=False` so environment proxies
  cannot silently take over DNS and connection establishment.
* **A2A calls use the same guard**, which matters because a card can point anywhere.
* **Manage tokens are stored hashed**; the plaintext exists only in the registration response
  and in the browser that performed it.
* **Optional panel token** protects `/api/panel/*` while leaving the registry open enough for
  agents to register. That split is intentional: the protocol is public, the control plane is
  yours.

Deploy behind TLS (a reverse proxy is fine) and set `--public-url` to the external URL so the
RegistryCard points somewhere reachable.

## Development

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest -q
```

Layout:

```
src/oacp/
  adp/        spec implementation: jcs, card checks, guarded fetcher, registry, routes, probe loop
  a2a/        JSON-RPC client with the two-dialect fallback
  tasks.py    local task ledger, A2A interactions, push bookkeeping
  push.py     inbound A2A push notifications (the agent calling back)
  api.py      panel API
  app.py      application factory, static UI, error mapping
  cli.py      `oacp serve`
  web/        index.html, styles.css, app.js
examples/     a tiny echo agent for local demos
tests/        pytest, with a mock A2A transport and a fake card fetcher
```

The test suite never touches the network: registration tests inject a fake fetcher and A2A
tests inject an `httpx.MockTransport`.

## Limitations

* No streaming (`message/stream`, `tasks/resubscribe`): a long task is followed by a
  callback or by asking.
* Callbacks are on by default, and reach exactly as far as the callback base does: a loopback
  base (`http://localhost:PORT`) is invisible to an agent on another host. `--public-url` is
  the fix, and the startup banner says when it is missing.
* The callback endpoint is validated but unthrottled: there is no rate limit and no
  signature scheme, because the per-task token already scopes what a caller can do.
* Deleting a task locally does not remove the push config at the agent; later callbacks for
  it are answered `404`.
* Single process, single SQLite file: this is a control plane, not a cluster service.
* No authentication schemes beyond A2A's own endpoint requirements.

## Links

* Source: <https://github.com/YY-Yifei/open-agent-control-panel>
* Package: <https://pypi.org/project/open-agent-control-panel/>

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
