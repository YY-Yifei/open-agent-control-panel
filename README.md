# Open Agent Control Panel

**A control panel for A2A agents.** Register them through the A2A Discover Protocol,
see everyone who is online, submit tasks over A2A JSON-RPC, and follow those tasks —
from one small FastAPI service with a dependency-free web UI.

[![PyPI](https://img.shields.io/pypi/v/open-agent-control-panel.svg)](https://pypi.org/project/open-agent-control-panel/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-lightgrey)](LICENSE)
[![Protocol](https://img.shields.io/badge/protocol-ADP%20Core%201.0.0-informational)](https://github.com/YY-Yifei/open-agent-control-panel)

---

## What it does

The panel sits between *you* and a fleet of [A2A](https://a2a-protocol.org) agents. It plays
three roles at once:

| Role | Surface | What it gives you |
|---|---|---|
| **Registry** | `/.well-known/a2a-registry.json`, `/v1/agents` | Agents register themselves by AgentCard URL; the panel fetches, validates and probes the card |
| **Control plane** | `/api/panel/*` + the bundled UI | Every registered agent, its card, its skills, and the tasks you sent it |
| **A2A client** | `message/send`, `tasks/get`, `tasks/cancel` | Submit a task, watch it move, cancel it, continue the conversation |

The registry half implements the **A2A Discover Protocol (ADP) Core v1** contract: five operations, four invariants, and the SSRF rules in §7. The panel half is
deliberately outside that contract — protocol endpoints stay exactly the protocol.

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

* **Agents** — status (`ok` / `down`), failures, last successful probe, skills, tags, task counts;
  a detail page with endpoints, the full card and a raw-JSON view.
* **Tasks** — every task the panel submitted, filterable by state, with a live view of the
  agent's latest output, the event log, and the raw request/response payloads.
* **Actions** — register, re-fetch a card, send a task, follow up with another message,
  refresh from the agent, cancel, unregister, forget.
* **Zero build step** — the front end is three static files (`index.html`, `styles.css`,
  `app.js`), served by the same process. No Node, no bundler, no CDN.

## Protocol surface

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/.well-known/a2a-registry.json` | RegistryCard (name, apiBase, spec, probe interval, limits) |
| `POST` | `/v1/agents` | `{"url": …}` → fetch, validate, store; `201` + one-time `manageToken` |
| `GET` | `/v1/agents` | `?q=&skill=&tag=&status=&changedSince=&full=&limit=&cursor=` |
| `GET` | `/v1/agents/{id}` | One AgentRecord, including the card |
| `DELETE` | `/v1/agents/{id}` | Unregister; requires `X-ADP-Token` |

Records follow the contract exactly: `id` is derived from the normalized URL, `cardHash` is
`sha256(JCS(card))`, `checkedAt` moves on every attempt, `lastOkAt` only on success, and
`changedAt`/`prevHash` are always present (nullable) so clients need one branch, not two.

Panel endpoints (not part of ADP):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/panel/summary` | Counts, deployment facts, RegistryCard |
| `GET` | `/api/panel/agents` | Agents with local task counts |
| `GET` | `/api/panel/agents/{id}` | Record + interfaces + recent tasks |
| `POST` | `/api/panel/agents/{id}/refresh` | Probe this agent now |
| `DELETE` | `/api/panel/agents/{id}` | Unregister with `{"token": …}` |
| `POST` | `/api/panel/agents/{id}/tasks` | Submit `{"text": …}` over A2A |
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
  tasks.py    local task ledger and A2A interactions
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

* No streaming (`message/stream`, `tasks/resubscribe`); tasks are followed by polling.
* Tasks are refreshed when you ask, not continuously.
* Single process, single SQLite file: this is a control plane, not a cluster service.
* No push notifications, no authentication schemes beyond A2A's own endpoint requirements.

## Links

* Source: <https://github.com/YY-Yifei/open-agent-control-panel>
* Package: <https://pypi.org/project/open-agent-control-panel/>

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
