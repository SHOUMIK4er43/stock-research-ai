# Stock Research AI — V1 user guide

Stock Research AI researches an Indian listed company from its Screener page (plus any
official documents and exchange data it can acquire) and shows the result in a Chrome side
panel. It is **research and information only**: it makes no recommendation, rating or price
objective, and it never invents a number it does not have.

Installation is covered in [getting-started.md](getting-started.md); privacy in
[privacy-disclosure.md](privacy-disclosure.md).

## The three views

| View | What it is | Needs |
|---|---|---|
| **Summary** | A short written summary of the company's Screener data, grounded in cited figures only. | The local service and a configured Summary provider (below). |
| **Detailed Research** | The full deterministic report (Boxes 4–14): financials, analytics, peers, forecasts, valuation, risks, governance, sources. Downloadable as PDF, HTML, Markdown and JSON. | The local service only (no model call). |
| **Negatives / Risks** | The report's company risks, kept apart from data limitations. | The same report as Detailed Research. |

Open a company page such as `https://www.screener.in/company/INFY/` (either the standalone or
the `/consolidated/` view: both give the same research), then use the side panel's
**Summary** and **Detailed Research** tabs.

## Reading a report

The top of every Detailed Research report states, before anything else:

- **Analytical basis** — the one reporting basis every comparable figure uses. CONSOLIDATED
  whenever consolidated statements exist and are current; STANDALONE otherwise. When a company's
  consolidated statements are older than its standalone ones, the report says so ("Standalone
  selected because the consolidated statements are older").
- **Control basis** — the other basis, shown only as a labelled reconciliation layer; never a
  primary figure.
- **Business classification** — the one classification every layer uses (for example
  FINANCIAL with an unresolved subtype). Methods that do not apply to it are marked *Not
  applicable*, never counted as missing.
- **Research completeness** — COMPLETE only when every *material* requirement of the
  company's business model is met; otherwise PARTIAL or UNAVAILABLE, with the requirements that
  are complete, partial, unavailable and not applicable listed in section 0.
- **Forecast** and **Valuation** availability, each with the reason when it is not complete.

Two different statuses appear and mean different things:

| Shown as | Means |
|---|---|
| "Ready — … — as of …" in the panel / report generation COMPLETE | The report was produced and rendered. |
| **Research completeness: PARTIAL** | The research itself is not complete: some material evidence is missing. |

Status words in sections and tables:

| Word | Meaning |
|---|---|
| Complete | Every required input for this part is available. |
| Partial | Some required input is missing; what is shown is still correct for what it covers. |
| Data unavailable | The evidence needed is not in the acquired data. It is **never** shown as zero. |
| Not applicable | The item does not belong to this company's business model (for example EBITDA for a lender, or an order book for a bank). |
| Not evaluable (governance / risk rules) | A rule could not run on the available evidence. This is **not** a "no risk" finding. |

Forecast years end in **E** (FY2027E): they are scenario estimates, never reported history.
Each claim's period says whether it is a quarter, a fiscal year or a balance-sheet date. Values
in the reports are shown to at most six decimals; the JSON export keeps full precision.

Governance is an **evidence ledger**, not a score: each topic shows what the reviewed sources
contain, whether a registered rule flagged it, and the source. "No adverse evidence in reviewed
source" covers only the sources reviewed; it is not a finding of good governance.

## Configuration (Summary provider)

Detailed Research needs no model credential. Summary uses the AI Credits provider and needs, in
the service's environment:

- `STOCK_RESEARCH_AI_ANALYST_PROVIDER=aicredits`
- `STOCK_RESEARCH_AI_AICREDITS_MODEL=<model id>`
- a key, either `STOCK_RESEARCH_AI_AICREDITS_API_KEY` or stored in the macOS Keychain with
  `stock-research-ai configure-analyst`

`stock-research-ai diagnostics` reports whether a credential is configured (never the key).
Never commit a key or a `.env` file; only `.env.example` (variable names, no values) is part
of the repository.

## Known V1 limitations

These are deliberate fail-closed behaviours: the product reports the gap instead of guessing.

- **Valuation requires supplied assumptions.** P/E, P/B, EV and DCF methods need explicitly
  supplied multiples / assumptions; V1 never assumes a default multiple and never turns a peer
  median into one. Without them valuation is *Data unavailable (MULTIPLE_MISSING)*.
- **Governance needs official documents.** The audit-opinion topic is evaluated from a retained,
  issuer-verified annual report; pledge, management-change, board and regulatory topics need
  structured filings or exchange data. Without them those topics are *Not evaluable*.
- **Some bank metrics need filings.** Gross / net NPA ratios and a PAT forecast for banks cannot
  be derived from Screener pages alone.
- **Lender finance cost is not inferred** from Screener's own Revenue / Interest / Financing
  Profit split for non-bank lenders, so cost of borrowings may be unavailable.
- **Unresolved financial subtypes limit peers.** When a lender's subtype (NBFC, insurer …) is
  unresolved, only business-model-neutral peer benchmarks (ROE, PAT growth …) are compared.
- **Pages without financial statements fail closed.** A Screener page with no statements (for
  example a newly listed SME) produces no Detailed Research; Summary may still describe the page.
- **Older reports** composed before the completeness ledger show *Research completeness: not
  assessed*.
- Revenue definitions stay distinct: a provider's "Revenue", a filing's revenue from operations
  and total income are never substituted for one another, so figures from different sources can
  legitimately differ.

## When something fails

| Message | What happened | Retry? |
|---|---|---|
| The local service is not reachable | The backend is not running. | Start it (the installed background service, or `stock-research-ai serve-http`), then retry. |
| The company's Screener page could not be acquired | Network or Screener unavailable. | Yes, later. |
| A research step could not run on this company's data | The data cannot support the research (e.g. no statements). | Not unless the source data changes. |
| The AI provider … / not configured | Summary provider problem. Detailed Research still works. | After fixing the configuration. |
| Another Detailed Research is already running | One report at a time. | Yes, shortly. |

No stack trace or internal error text is ever shown in the panel.

## Release checklist (V1)

- [ ] `uv run stock-research-ai serve-http` (or the installed service) starts; `curl
  http://127.0.0.1:8000/health` returns `{"status":"ok"}`
- [ ] the extension loads unpacked from `extension/` (or the release ZIP) and pairs
      (`stock-research-ai pair-code`)
- [ ] Summary works on a configured provider; Detailed Research and Negatives open for HUDCO,
      INFY, YESBANK and an industrial company; PDF / HTML / Markdown / JSON download
- [ ] an Aptus-like page and a stopped backend fail with the messages above
- [ ] `uv run pytest`, `cd extension && npm test`, `ruff check src tests`, `mypy --strict src`
      all pass
- [ ] `gitleaks git .` reports no leaks; no database, PDF report, log or `.env` file is tracked
      (`git status --short`, `git ls-files`)
- [ ] this guide and getting-started.md match the release
