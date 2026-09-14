# Changelog

All notable changes are recorded here in concise, customer-facing language. This is
**internal pilot software** — no public or customer release has occurred yet (see
`docs/chrome-web-store-readiness.md` and `docs/part6c-b-proof.md`).

## Unreleased — Part 6C-B: Pilot Packaging & Distribution

- macOS pilot installer (`install-macos.sh`) and uninstaller (`uninstall-macos.sh`) —
  user-local install via `uv`, no admin password required.
- Automatic background service startup via a per-user macOS LaunchAgent
  (`ai.stockresearch.local`) — no manual Terminal command needed for day-to-day use.
- Deterministic release bundle builder (backend wheel/sdist, extension ZIP, checksums,
  machine-readable release manifest).
- Chrome extension now ships with original icon assets (16/32/48/128px).
- New `stock-research-ai render-launchagent` command (installer plumbing).
- Getting-started guide, privacy disclosure, and Chrome Web Store readiness checklist.

## Part 6C-A: Runtime & Reliability Hardening

- SQLite production hardening: WAL mode, bounded busy-timeout, `synchronous=NORMAL`.
- Forward-only schema versioning via `PRAGMA user_version` (no new metadata table).
- macOS Keychain-backed storage for the Anthropic API credential
  (`stock-research-ai configure-analyst`), with environment-variable override
  preserved for development.
- Structured, redacted local logging and a safe `diagnostics` command.
- Local `backup`/`restore` commands using SQLite's own online backup API.
- Pairing brute-force cooldown for the browser extension pairing flow.
- Single, unified application version source (no duplicated version strings).

## Part 6B: Chrome Browser Research Product

- Local HTTP API with pairing/authentication for a companion Chrome extension.
- Chrome MV3 side-panel extension: Screener page detection, pairing UX, research job
  launch/progress/reconnection.
- Full institutional report viewer inside the side panel: evidence drill-down,
  independent challenger findings, document intelligence, and artifact downloads.

## Part 6A and earlier: Research Engine Foundation

- Local-first equity research engine: company registry, Screener/document/market
  evidence acquisition, financial/sector/technical calculations, valuation, the live
  analyst (Part 5B), the independent deterministic challenger (Part 5C), and the final
  institutional report engine (Part 6A).
- `FULL_RESEARCH_V1` end-to-end research plan.
