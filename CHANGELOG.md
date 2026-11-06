# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- The demo agent flushes its status lines, so `[self-registration] …` still shows up when its
  output is piped into a file instead of a terminal.

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
