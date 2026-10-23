# Privacy Disclosure — Stock Research AI (Pilot)

This document accurately describes what Stock Research AI actually does today. It is
written for the pilot program described in [`getting-started.md`](getting-started.md);
it will be revisited before any Chrome Web Store submission (see
[`chrome-web-store-readiness.md`](chrome-web-store-readiness.md)).

## What runs where

Stock Research AI has two parts:

1. **A local backend service** that runs entirely on your own Mac, bound only to
   `127.0.0.1` (your own machine — never reachable from your network or the internet).
2. **A Chrome extension** that talks only to that local backend and to the Screener.in
   pages you're browsing (to detect a supported company page); it never talks to any
   other server.

## Where your research data lives

- Your research database, evidence, and generated reports are stored locally on your
  Mac at `~/.stock-research-ai/` (or wherever you've explicitly configured).
- This data is never uploaded, synced, or shared anywhere by Stock Research AI itself.
- It leaves your machine only if you explicitly export/share a report file yourself.

## What the Chrome extension does and does not do

The extension:

- reads only the currently active tab's URL, to decide whether it's a supported
  Screener company page (it never reads or scrapes the page's content/DOM)
- talks only to your local backend at `127.0.0.1` and to `https://www.screener.in`
  pages you navigate to yourself in your own browser
- stores only a small amount of state in `chrome.storage.local` (your local service
  address, your pairing token, and a pointer to your last report) — **never**
  `chrome.storage.sync`, so nothing syncs to your Google account or any other device
- has **no telemetry, no analytics, and no crash-reporting SDK** of any kind
- **never contacts an AI provider directly** — only your local backend does that

## What the local backend does (V1)

**Summary** (when you click *Generate Summary*): the backend reads the public Screener company
page and sends a bounded set of that page's figures (the company's own reported numbers and
labels, no personal data) to the AI provider **you configured** (AI Credits, with your own key)
to write the summary. This is the only step that sends data to an AI provider.

**Detailed Research** (when you click *Generate Detailed Research*): no AI provider is called.
The backend:

- acquires the public company page (standalone and consolidated views), Screener's peer table
  and the listed peers' public pages from Screener.in
- acquires public documents linked from that page (for example annual reports and filings on
  BSE / NSE / the company's own website, credit-rating releases) within fixed size and count
  limits
- acquires public exchange data (NSE / BSE filings and announcements) for the company, and, only
  if you configured your own Kite credentials, market data from that account
- computes every number, check and report section locally and deterministically
- stores the resulting report and evidence locally

These requests go only to the public sources named above; nothing is sent to any analytics,
telemetry or advertising service.

## Your AI provider key

- In production/pilot use, your key is stored in your Mac's own **Keychain** — never
  in a plain text file, never in the installed LaunchAgent configuration, never in any
  log file, and never in the local research database.
- `stock-research-ai diagnostics` reports only whether a credential is configured and
  where it came from (environment variable or Keychain) — **never the key value
  itself**.

## Logs

- Stock Research AI writes a local, rotating log file at
  `~/.stock-research-ai/logs/service.log` (bounded in size) to help with
  troubleshooting.
- Known secret values (your AI provider key, if it were ever passed via an environment
  variable during development) and `Authorization: Bearer` header values are actively
  redacted from anything written to this log.
- This log is never uploaded anywhere automatically.

## Backups

- `stock-research-ai backup` copies your local database and evidence to a location you
  choose. It never includes your AI provider key.
- Backups are stored wherever you direct them — Stock Research AI does not upload a
  backup anywhere on your behalf.

## Sharing your own data

If you choose to download an Institutional Report, Investor Brief, or the raw research
JSON and then share that file yourself (for example, by emailing it), that sharing is
entirely your own action — Stock Research AI has no part in it and no visibility into
what you do with an exported file afterward.

## What we do not do

- We do not run a cloud service that receives your research data.
- We do not use your data to train any model.
- We do not sell or share your data with any third party. The only data sent to an AI
  provider is the bounded set of public company-page figures the Summary view sends to the
  provider you configured yourself.
- We do not track you across websites.
- We do not collect analytics about your usage of the extension or backend.
