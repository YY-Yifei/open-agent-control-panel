# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/YY-Yifei/open-agent-control-panel/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/YY-Yifei/open-agent-control-panel/releases/tag/v0.1.0
