/**
 * Detailed Research (Box 14/15 final report) helpers - pure. The backend owns every fact:
 * this module validates response shapes, builds labels / download names and renders the
 * two panel views of the SAME stored report with textContent only (never innerHTML):
 *
 *   Detailed Research  - Box 14 executive summary points + every section's status
 *   Negatives / Risks  - RED / YELLOW company risks and positive controls, with input
 *                        conflicts, data limitations and open diligence kept separate
 *
 * No research, numbers or identity are derived here. The page identifier is only an
 * opaque Screener page key (never treated as an exchange symbol); the backend matches it
 * against the page key it recorded from the ACQUIRED Screener dataset.
 */
import { FINAL_REPORT_FORMATS } from "./api/client.js";
import { el } from "./report/dom.js";

export const FINAL_REPORT_LABELS = Object.freeze({
  PDF: "PDF", HTML: "HTML", MARKDOWN: "Markdown", JSON: "JSON",
});
const EXTENSIONS = Object.freeze({ PDF: "pdf", HTML: "html", MARKDOWN: "md", JSON: "json" });
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const PAGE = /^[A-Z0-9_-]{1,40}$/;
const JOB_STATUSES = new Set(["QUEUED", "RUNNING", "READY", "PARTIAL", "FAILED"]);

/** Polling for a generation job; tests may set `ms` to 0. */
export const FINAL_REPORT_POLL = { ms: 2000, maxPolls: 1200 };

/** View states the panel shows (one at a time per page). */
export const FinalReportState = Object.freeze({
  IDLE: "IDLE",
  LOOKING: "LOOKING",
  NOT_GENERATED: "NOT_GENERATED",
  GENERATING: "GENERATING",
  READY: "READY",
  PARTIAL: "PARTIAL",
  FAILED: "FAILED",
});

/** Fixed user-facing text per backend failure code - server text is never shown. */
const FAILURE_TEXT = Object.freeze({
  COMPANY_IDENTITY_MISMATCH:
    "The company identity could not be confirmed for this page; no report was produced.",
  SOURCE_ACQUISITION_FAILED: "The company's Screener page could not be acquired. Try again later.",
  UPSTREAM_RESEARCH_FAILED:
    "A research step could not run on this company's data (for example, the page has no financial statements), so no report was produced. Retrying will not help unless the source data changes; Summary may still work.",
  REPORT_COMPOSITION_FAILED: "The final report could not be composed.",
  RENDER_FAILED: "The report could not be rendered in any format.",
  OUTPUT_IO_FAILED: "The report could not be saved locally.",
  INPUT_ERROR: "The research inputs were invalid; no report was produced.",
  GENERATION_TIMEOUT: "Detailed Research took too long and was stopped.",
  FINAL_REPORT_JOB_LIMIT_REACHED: "Another Detailed Research is already running. Try again shortly.",
  NETWORK: "The local service is not reachable.",
  LOOKUP_FAILED: "Could not look up Detailed Research.",
  GENERATION_REQUEST_FAILED: "Could not start Detailed Research.",
  JOB_LOST: "Detailed Research status was lost (the local service restarted). Generate it again.",
  INVALID_VIEW: "The stored report could not be displayed.",
  INVALID_JOB: "Detailed Research returned an unexpected status.",
});

export function failureText(code) {
  return FAILURE_TEXT[code] || "Detailed Research could not be generated.";
}

/** The Screener page key for a page identity (upper case; opaque, not a ticker). */
export function pageKey(pageIdentity) {
  const raw = pageIdentity && typeof pageIdentity.page_identifier === "string"
    ? pageIdentity.page_identifier.toUpperCase() : "";
  return PAGE.test(raw) ? raw : null;
}

/** The newest well-formed report in a /v1/final-reports response, or null. */
export function latestFinalReport(payload) {
  const reports = payload && Array.isArray(payload.reports) ? payload.reports : [];
  for (const r of reports) {
    if (!r || typeof r !== "object") continue;
    if (typeof r.artifact_id !== "string" || !UUID.test(r.artifact_id)) continue;
    if (typeof r.as_of !== "string" || !DAY.test(r.as_of)) continue;
    const formats = Array.isArray(r.formats) ? r.formats.filter(f => FINAL_REPORT_FORMATS.includes(f)) : [];
    const failed = Array.isArray(r.failed_formats)
      ? r.failed_formats.filter(f => FINAL_REPORT_FORMATS.includes(f)) : [];
    return {
      artifactId: r.artifact_id,
      asOf: r.as_of,
      companyName: typeof r.company_name === "string" ? r.company_name : "",
      symbol: typeof r.nse_symbol === "string" ? r.nse_symbol : "",
      status: typeof r.status === "string" ? r.status : "",
      formats,
      failed,
    };
  }
  return null;
}

/** A validated generation job, or null. */
export function parseFinalReportJob(payload) {
  if (!payload || typeof payload !== "object") return null;
  if (typeof payload.job_id !== "string" || !UUID.test(payload.job_id)) return null;
  if (!JOB_STATUSES.has(payload.status)) return null;
  const artifactId = typeof payload.artifact_id === "string" && UUID.test(payload.artifact_id)
    ? payload.artifact_id : null;
  if ((payload.status === "READY" || payload.status === "PARTIAL") && !artifactId) return null;
  const failure = payload.failure && typeof payload.failure.code === "string" ? payload.failure.code : null;
  return {
    jobId: payload.job_id,
    status: payload.status,
    stage: typeof payload.stage === "string" ? payload.stage : "",
    artifactId,
    failureCode: failure,
  };
}

export function jobStageLabel(job) {
  if (!job) return "Generating Detailed Research…";
  if (job.stage === "ACQUIRING") return "Generating Detailed Research — acquiring company data…";
  if (job.stage === "BUILDING") return "Generating Detailed Research — building the report…";
  return "Generating Detailed Research…";
}

/** One-line status for the panel (text only; rendered with textContent). */
export function finalReportLabel(report) {
  if (!report) {
    return "Detailed Research has not been generated for this company yet.";
  }
  const partial = report.status === "PARTIAL_RENDER" && report.failed.length
    ? ` Partial: ${report.failed.map(f => FINAL_REPORT_LABELS[f]).join(", ")} could not be produced.`
    : "";
  return `Detailed Research — ${report.companyName} — as of ${report.asOf}.${partial}`;
}

export function finalReportFilename(report, format) {
  const who = String(report.symbol || "company").replace(/[^A-Za-z0-9_-]+/g, "_").slice(0, 20) || "company";
  return `${who}_${report.asOf}_detailed_research.${EXTENSIONS[format]}`;
}

// --- panel views ----------------------------------------------------------------------------

const str = v => (typeof v === "string" ? v : "");
function items(list) {
  const raw = list && Array.isArray(list.items) ? list.items : [];
  return {
    total: list && Number.isInteger(list.total) ? list.total : raw.length,
    items: raw.filter(i => i && typeof i === "object").map(i => ({
      label: str(i.label), severity: str(i.severity), statement: str(i.statement), detail: str(i.detail),
    })),
  };
}

const REPORTING_BASES = Object.freeze(["CONSOLIDATED", "STANDALONE"]);

/** A reporting basis exactly as Box 14 states it, else null (never guessed). */
function basis(value) {
  return REPORTING_BASES.includes(value) ? value : null;
}

/** The report's analytical basis line; a control basis is labelled as reconciliation only. */
export function basisLine(panel) {
  const analytical = `Analytical basis: ${panel.analyticalBasis || "not stated"}`;
  return panel.controlBasis
    ? `${analytical} · Control basis: ${panel.controlBasis} (reconciliation only, never a primary figure)`
    : analytical;
}

const RESEARCH_STATUSES = Object.freeze(["COMPLETE", "PARTIAL", "UNAVAILABLE"]);

/** Box 14's research-completeness lines: [label, text] string pairs only (bounded). */
function completenessLines(value) {
  if (!Array.isArray(value)) return [];
  return value
    .filter(row => Array.isArray(row) && row.length === 2 && row.every(x => typeof x === "string"))
    .slice(0, 12)
    .map(([label, text]) => ({ label, text }));
}

const RISK_COUNT_KEYS = Object.freeze(["EVALUATED", "APPLICABLE", "GREEN", "YELLOW", "RED",
  "NOT_EVALUATED", "NOT_APPLICABLE"]);

/** Box 14's risk-coverage counts: known keys with non-negative integers only, else null. */
function riskCounts(value) {
  if (!Array.isArray(value)) return null;
  const out = {};
  for (const row of value) {
    if (!Array.isArray(row) || row.length !== 2) continue;
    const [key, n] = row;
    if (RISK_COUNT_KEYS.includes(key) && Number.isInteger(n) && n >= 0) out[key] = n;
  }
  return RISK_COUNT_KEYS.every(k => k in out) ? out : null;
}

/** A validated panel view (GET /v1/final-reports/{id}/view), or null. */
export function parsePanel(payload) {
  if (!payload || typeof payload !== "object") return null;
  if (typeof payload.artifact_id !== "string" || !UUID.test(payload.artifact_id)) return null;
  if (typeof payload.as_of !== "string" || !DAY.test(payload.as_of)) return null;
  if (payload.status !== "COMPLETE" && payload.status !== "PARTIAL_RENDER") return null;
  const sections = Array.isArray(payload.sections) ? payload.sections : [];
  const formats = Array.isArray(payload.formats)
    ? payload.formats.filter(f => FINAL_REPORT_FORMATS.includes(f)) : [];
  if (!formats.includes("JSON")) return null;
  return {
    artifactId: payload.artifact_id,
    companyName: str(payload.company_name),
    symbol: str(payload.nse_symbol),
    asOf: str(payload.as_of),
    formats,
    failed: Array.isArray(payload.failed_formats)
      ? payload.failed_formats.filter(f => FINAL_REPORT_FORMATS.includes(f)) : [],
    status: str(payload.status),
    analyticalBasis: basis(payload.analytical_basis),
    // analysis, apart from `status` (rendering / execution); null = not assessed, never COMPLETE
    researchStatus: RESEARCH_STATUSES.includes(payload.research_status) ? payload.research_status : null,
    basisNote: typeof payload.basis_note === "string" ? payload.basis_note.slice(0, 300) : null,
    researchCompleteness: completenessLines(payload.research_completeness),
    controlBasis: basis(payload.control_basis),
    disclaimer: str(payload.disclaimer),
    summary: items(payload.summary),
    sections: sections.filter(s => s && typeof s === "object").map(s => ({
      title: str(s.title), statusLabel: str(s.status_label), reason: str(s.reason),
    })),
    red: items(payload.red),
    yellow: items(payload.yellow),
    positive: items(payload.positive_controls),
    conflicts: items(payload.input_conflicts),
    limitations: items(payload.data_limitations),
    diligence: items(payload.open_diligence),
    // the canonical risk coverage (null on reports composed before it existed)
    riskCoverage: typeof payload.risk_coverage === "string" ? payload.risk_coverage.slice(0, 600) : null,
    riskCounts: riskCounts(payload.risk_counts),
    evaluatedClear: items(payload.evaluated_clear),
    notEvaluated: items(payload.not_evaluated),
  };
}

function itemList(doc, list, className) {
  const ul = el(doc, "ul", { className: `final-report-items ${className}` });
  for (const item of list.items) {
    const head = item.severity ? `[${item.severity}] ${item.label}` : item.label;
    ul.appendChild(el(doc, "li", { children: [
      el(doc, "strong", { text: head }),
      el(doc, "span", { text: ` ${item.statement}` }),
      item.detail ? el(doc, "span", { className: "muted", text: ` (${item.detail})` }) : null,
    ] }));
  }
  if (list.total > list.items.length) {
    ul.appendChild(el(doc, "li", { className: "muted",
      text: `${list.total - list.items.length} more in the downloaded report.` }));
  }
  return ul;
}

function block(doc, title, list, className, emptyText) {
  return el(doc, "section", { className: `final-report-block ${className}`, children: [
    el(doc, "h3", { text: `${title} (${list.total})` }),
    list.items.length ? itemList(doc, list, className) : el(doc, "p", { className: "muted", text: emptyText }),
  ] });
}

/** The ONE research-completeness conclusion (Box 14's material-requirement ledger). */
export function researchStatusLabel(panel) {
  return `Research completeness: ${panel.researchStatus || "not assessed"}`;
}

function completenessBlock(doc, panel) {
  const ul = el(doc, "ul", { className: "final-report-completeness" });
  for (const row of panel.researchCompleteness) {
    if (row.label === "Overall") continue;
    ul.appendChild(el(doc, "li", { children: [
      el(doc, "strong", { text: `${row.label}: ` }), el(doc, "span", { text: row.text }),
    ] }));
  }
  return el(doc, "section", { className: "final-report-block completeness", children: [
    el(doc, "h3", { text: researchStatusLabel(panel) }),
    panel.researchCompleteness.length ? ul
      : el(doc, "p", { className: "muted", text: "This report predates the completeness ledger." }),
  ] });
}

/** The Detailed Research view: Box 14 executive summary + section statuses. */
export function renderDetailedResearch(doc, panel) {
  const sections = el(doc, "ol", { className: "final-report-sections" });
  for (const s of panel.sections) {
    sections.appendChild(el(doc, "li", { children: [
      el(doc, "strong", { text: s.title }),
      el(doc, "span", { text: ` — ${s.statusLabel}` }),
      s.reason ? el(doc, "span", { className: "muted", text: ` (${s.reason})` }) : null,
    ] }));
  }
  return el(doc, "article", { className: "final-report-detailed", children: [
    el(doc, "h2", { text: `${panel.companyName} — Detailed Research` }),
    el(doc, "p", { className: "final-report-basis", text: basisLine(panel) }),
    panel.basisNote ? el(doc, "p", { className: "muted", text: panel.basisNote }) : null,
    completenessBlock(doc, panel),
    el(doc, "p", { className: "muted", text: `As of ${panel.asOf}. ${panel.disclaimer}` }),
    block(doc, "Executive summary", panel.summary, "summary", "No summary points were supplied."),
    el(doc, "h3", { text: "Sections" }),
    sections,
  ] });
}

/** "Risk rules evaluated: N / M" with every count, ahead of any RED / YELLOW list. */
export function riskCoverageLine(panel) {
  const c = panel.riskCounts;
  if (!c) return "Risk coverage: not stated (report predates the coverage count)";
  return `Risk rules evaluated: ${c.EVALUATED} / ${c.APPLICABLE} · GREEN ${c.GREEN} · ` +
    `YELLOW ${c.YELLOW} · RED ${c.RED} · NOT EVALUATED ${c.NOT_EVALUATED} · ` +
    `NOT APPLICABLE ${c.NOT_APPLICABLE}`;
}

function coverageBlock(doc, panel) {
  return el(doc, "section", { className: "final-report-block risk-coverage", children: [
    el(doc, "h3", { text: riskCoverageLine(panel) }),
    panel.riskCoverage ? el(doc, "p", { text: panel.riskCoverage }) : null,
  ] });
}

/** The Negatives / Risks view: coverage first, company risks apart from data limitations. */
export function renderRisks(doc, panel) {
  // grades are only listed with their denominator: "no RED" never stands alone
  const none = level => panel.riskCounts
    ? `No ${level} company risk among the ${panel.riskCounts.EVALUATED} rules evaluated.`
    : `No ${level} company risk.`;
  return el(doc, "article", { className: "final-report-risks", children: [
    el(doc, "h2", { text: `${panel.companyName} — Negatives / Risks` }),
    el(doc, "p", { className: "final-report-basis",
      text: `Basis: ${panel.analyticalBasis || "not stated"}` }),
    el(doc, "p", { className: "muted", text: `As of ${panel.asOf}. Company risks as graded upstream; data limitations are listed separately and are not company risks.` }),
    coverageBlock(doc, panel),
    block(doc, "RED company risks", panel.red, "risk-red", none("RED")),
    block(doc, "YELLOW company risks", panel.yellow, "risk-yellow", none("YELLOW")),
    block(doc, "Rules NOT EVALUATED (evidence unavailable — not a finding of no risk)",
      panel.notEvaluated, "risk-not-evaluated", "Every applicable rule was evaluated."),
    block(doc, "Rules evaluated — no adverse evidence found", panel.evaluatedClear,
      "risk-clear", "No rule graded GREEN."),
    block(doc, "Positive controls", panel.positive, "risk-positive", "None."),
    el(doc, "h2", { text: "Data limitations (not company risks)" }),
    block(doc, "Input conflicts", panel.conflicts, "limitation-conflict", "No input conflict."),
    block(doc, "Data limitations", panel.limitations, "limitation", "No data limitation."),
    block(doc, "Open diligence items", panel.diligence, "diligence", "No open diligence item."),
  ] });
}
