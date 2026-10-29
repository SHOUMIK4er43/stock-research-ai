import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiClient, ApiError, FINAL_REPORT_FORMATS } from "../src/api/client.js";
import {
  FINAL_REPORT_POLL,
  finalReportFilename,
  finalReportLabel,
  latestFinalReport,
  pageKey,
  parseFinalReportJob,
  parsePanel,
  renderDetailedResearch,
  renderRisks,
  researchStatusLabel,
} from "../src/final_report.js";
import { createMemoryBackend } from "../src/storage.js";
import { createFakeDocument } from "./fixtures/fake_dom.js";

// Detailed Research is the Box 14/15 final report. Opening the Detailed Research or the
// Negatives / Risks view looks this PAGE's report up (by the opaque Screener page key, never
// as a symbol) and, when none exists, asks the backend to generate it - no CLI, no stage.
FINAL_REPORT_POLL.ms = 0;

const BASE = "http://127.0.0.1:8000";
const ID = "21e8c4c9-9e9a-53b0-bade-afa329e92924";
const TOKEN = "synthetic-pairing-token";
const VERSION = { api_version: "v1", application_version: "0.1.0",
  report_schema_version: "6A.1", supported_research_plans: ["FULL_RESEARCH_V1"],
  supported_portals: ["SCREENER"] };
const settle = () => new Promise(resolve => setImmediate(resolve));
const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
let sequence = 0;

function listing(overrides = {}) {
  return { symbol: "INFY", reports: [{
    artifact_id: ID, report_id: "0784994d-d84f-5cea-b7eb-8058ad00dde1",
    company_name: "Infosys Ltd", nse_symbol: "INFY", as_of: "2026-10-12", status: "COMPLETE",
    formats: ["JSON", "MARKDOWN", "HTML", "PDF"], failed_formats: [], box14_sha256: "a".repeat(64),
    ...overrides }] };
}

const JOB = "5b0f3a52-6d0c-4f7e-9a51-0c1d2e3f4a5b";

function panelPayload(overrides = {}) {
  const list = (items) => ({ items, total: items.length });
  return {
    artifact_id: ID, report_id: "0784994d-d84f-5cea-b7eb-8058ad00dde1", company_name: "Infosys Ltd",
    nse_symbol: "INFY", isin: null, screener_page: "INFY", as_of: "2026-10-12", status: "COMPLETE",
    formats: ["JSON", "MARKDOWN", "HTML", "PDF"], failed_formats: [],
    disclaimer: "Research and information only. Not investment advice.",
    summary: list([{ label: "Financial trajectory", statement: "Revenue reported 100 INR CRORES.", detail: "FACT" }]),
    sections: [{ order: 0, title: "Due-Diligence Status", status: "COMPLETE", status_label: "Complete", reason: "ok" },
      { order: 4, title: "Industry & Competitive Position", status: "PARTIAL", status_label: "Partial", reason: "peers" }],
    red: list([{ label: "HIGH_ACCRUALS", severity: "RED", statement: "HIGH_ACCRUALS: RED <script>x</script>", detail: "Accounting · EVALUATED" }]),
    yellow: list([{ label: "AUDITOR_SIGNAL", severity: "YELLOW", statement: "AUDITOR_SIGNAL: YELLOW", detail: null }]),
    positive_controls: list([]),
    input_conflicts: list([{ label: "PLEDGE", statement: "Pledge inputs conflict", detail: "Input conflict - a data limitation, not a company risk" }]),
    data_limitations: list([{ label: "BLOCKED_SOURCE", statement: "Provider unavailable: blocked source", detail: "BOX12" }]),
    open_diligence: list([{ label: "MEDIUM", statement: "What are net debt and EBITDA?", detail: "Audited P&L" }]),
    recommendation: "NONE",
    ...overrides,
  };
}

/** A fake backend: page lookups, generation jobs, views and files. */
function backend({ reports = [listing().reports[0]], jobs = null, view = panelPayload(), create = null } = {}) {
  const state = { reports, polls: 0 };
  return {
    state,
    async handle(parsed, init) {
      if (parsed.pathname === "/v1/final-reports" && init.method === "POST") {
        if (create) return create();
        return json(202, { job_id: JOB, status: "QUEUED", stage: "QUEUED", artifact_id: null, failure: null });
      }
      if (parsed.pathname === "/v1/final-reports") return json(200, { page: "INFY", reports: state.reports });
      if (parsed.pathname === `/v1/final-report-jobs/${JOB}`) {
        const seq = jobs || [{ status: "RUNNING", stage: "ACQUIRING" }, { status: "READY", stage: "DONE", artifact_id: ID }];
        const next = seq[Math.min(state.polls++, seq.length - 1)];
        return json(200, { job_id: JOB, artifact_id: null, failure: null, ...next });
      }
      if (parsed.pathname === `/v1/final-reports/${ID}/view`) return typeof view === "function" ? view() : json(200, view);
      if (parsed.pathname.startsWith("/v1/final-reports/")) {
        return { ok: true, status: 200, blob: async () => new Blob(["x"]), headers: { get: () => "" } };
      }
      return null;
    },
  };
}

test("helpers: listing, page key, labels and file names are validated and sanitised", () => {
  assert.equal(latestFinalReport(null), null);
  assert.equal(latestFinalReport({ reports: [{ artifact_id: "../x", as_of: "2026-10-12" }] }), null);
  const r = latestFinalReport(listing({ formats: ["PDF", "EXE", "../report"], failed_formats: ["HTML"] }));
  assert.deepEqual(r.formats, ["PDF"]);
  assert.equal(pageKey({ page_identifier: "rvnl" }), "RVNL");
  assert.equal(pageKey({ page_identifier: "../x" }), null);
  assert.equal(parseFinalReportJob({ job_id: JOB, status: "READY" }), null,
    "a ready job must carry its report id");
  assert.equal(parsePanel(panelPayload({ as_of: "tomorrow" })), null,
    "download metadata has a closed date format");
  assert.equal(parsePanel(panelPayload({ formats: ["HTML"] })), null,
    "the interactive view requires the verified JSON artifact");
  assert.match(finalReportLabel(null), /has not been generated/);
  assert.doesNotMatch(finalReportLabel(null), /stock-research-ai|--stage|stage\.json/);
  const evil = latestFinalReport(listing({ nse_symbol: '../"\r\nX' }));
  assert.match(finalReportFilename(evil, "PDF"), /^[A-Za-z0-9_-]+_2026-10-12_detailed_research\.pdf$/);
});

test("client: page lookup, generation, job and view calls are authenticated and validated", async () => {
  const calls = [];
  const client = new ApiClient(BASE, async (url, init) => { calls.push({ url, init }); return json(200, {}); });
  await assert.rejects(client.listFinalReportsForPage("tok", "M&M/../x"), ApiError);
  assert.equal(calls.length, 0, "an invalid page key never reaches the network");
  await client.listFinalReportsForPage("tok", "BAJAJ-AUTO");
  assert.equal(calls[0].url, `${BASE}/v1/final-reports?page=BAJAJ-AUTO`);
  assert.equal(calls[0].init.headers.Authorization, "Bearer tok");
  await client.createFinalReport("tok", "https://www.screener.in/company/INFY/");
  assert.equal(calls[1].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[1].init.body), { source_url: "https://www.screener.in/company/INFY/" });
  await client.getFinalReportJob("tok", JOB);
  assert.equal(calls[2].url, `${BASE}/v1/final-report-jobs/${JOB}`);
  await client.getFinalReportView("tok", ID);
  assert.equal(calls[3].url, `${BASE}/v1/final-reports/${ID}/view`);
  await assert.rejects(client.getFinalReportJob("tok", "../x"), ApiError);
  await assert.rejects(client.getFinalReportView("tok", "../x"), ApiError);
  assert.equal(calls.length, 4);
});

test("getFinalReportFile refuses an unknown format or id without calling fetch", async () => {
  let called = false;
  const client = new ApiClient(BASE, async () => { called = true; return json(200, {}); });
  await assert.rejects(client.getFinalReportFile("t", ID, "EXE"), ApiError);
  await assert.rejects(client.getFinalReportFile("t", "../../etc/passwd", "PDF"), ApiError);
  assert.equal(called, false);
  assert.deepEqual([...FINAL_REPORT_FORMATS], ["PDF", "HTML", "MARKDOWN", "JSON"]);
});

test("getFinalReportFile returns the binary body and maps errors", async () => {
  const calls = [];
  const blob = new Blob([new Uint8Array([0x25, 0x50, 0x44, 0x46])], { type: "application/pdf" });
  const ok = new ApiClient(BASE, async (url, init) => {
    calls.push({ url, init });
    return { ok: true, status: 200, blob: async () => blob, headers: { get: () => "application/pdf" } };
  });
  const result = await ok.getFinalReportFile("tok", ID, "PDF");
  assert.equal(result.blob, blob);
  assert.equal(calls[0].url, `${BASE}/v1/final-reports/${ID}/files/PDF`);
  const missing = new ApiClient(BASE, async () => json(404, { detail: { code: "FINAL_REPORT_FILE_UNAVAILABLE", message: "x" } }));
  await assert.rejects(missing.getFinalReportFile("tok", ID, "HTML"),
    e => e instanceof ApiError && e.status === 404 && e.code === "FINAL_REPORT_FILE_UNAVAILABLE");
});

test("the risks view leads with coverage and never shows 'no RED' without its denominator", () => {
  const list = (items) => ({ items, total: items.length });
  const panel = parsePanel(panelPayload({
    red: list([]), yellow: list([]),
    risk_coverage: "Risk rules evaluated: 11 of 25 applicable (RED 0, YELLOW 0, GREEN 11); 14 not evaluated because their evidence is unavailable - not a finding of no risk.",
    risk_counts: [["EVALUATED", 11], ["APPLICABLE", 25], ["GREEN", 11], ["YELLOW", 0], ["RED", 0],
      ["NOT_EVALUATED", 14], ["NOT_APPLICABLE", 3]],
    evaluated_clear: list([{ label: "Weak interest cover", severity: "GREEN", statement: "Evaluated - no adverse evidence found", detail: "Leverage · WEAK_INTEREST_COVER" }]),
    not_evaluated: { items: [{ label: "Promoter pledge", statement: "Not evaluated - promoter pledge data not available", detail: "Governance · PLEDGE_DATA_NOT_SUPPLIED" }], total: 14 },
  }));
  const html = renderRisks(createFakeDocument(), panel).serialize();
  assert.match(html, /Risk rules evaluated: 11 \/ 25 · GREEN 11 · YELLOW 0 · RED 0 · NOT EVALUATED 14 · NOT APPLICABLE 3/);
  assert.ok(html.indexOf("Risk rules evaluated") < html.indexOf("RED company risks"));
  assert.match(html, /No RED company risk among the 11 rules evaluated\./);
  assert.match(html, /Rules NOT EVALUATED[^<]*\(14\)/);
  assert.match(html, /Promoter pledge/);
  assert.match(html, /Weak interest cover/);
  // an older report without coverage says so instead of implying a clean result
  const old = renderRisks(createFakeDocument(), parsePanel(panelPayload())).serialize();
  assert.match(old, /Risk coverage: not stated/);
  // malformed counts are never shown
  assert.equal(parsePanel(panelPayload({ risk_counts: [["RED", -1]] })).riskCounts, null);
});

test("the risks view keeps company risks apart from data limitations and escapes text", () => {
  const doc = createFakeDocument();
  const node = renderRisks(doc, parsePanel(panelPayload()));
  const html = node.serialize();
  assert.ok(html.indexOf("RED company risks") < html.indexOf("YELLOW company risks"));
  assert.ok(html.indexOf("YELLOW company risks") < html.indexOf("Data limitations (not company risks)"));
  assert.ok(html.indexOf("Data limitations (not company risks)") < html.indexOf("Input conflicts"));
  assert.ok(!html.includes("<script>"), "statement text is never markup");
  assert.match(html, /&lt;script&gt;/);
});

async function harness(t, { tabUrl = "https://www.screener.in/company/INFY/", api = backend() } = {}) {
  const doc = createFakeDocument();
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) {
      const node = doc.createElement("div");
      node.hidden = true;
      elements.set(id, node);
    }
    return elements.get(id);
  };
  const storage = createMemoryBackend();
  await storage.set({ pairingToken: TOKEN });
  const requests = [];
  const downloads = [];
  const logs = [];
  for (const method of ["log", "warn", "error"]) t.mock.method(console, method, (...a) => logs.push(a));
  t.mock.method(globalThis, "fetch", async (url, init) => {
    const parsed = new URL(url);
    requests.push({ path: parsed.pathname, search: parsed.search, init });
    const handled = await api.handle(parsed, init);
    if (handled) return handled;
    const payload = parsed.pathname === "/health" ? { status: "ok" }
      : parsed.pathname === "/v1/version" ? VERSION : {};
    return json(200, payload);
  });
  t.mock.method(URL, "createObjectURL", () => "blob:synthetic");
  t.mock.method(URL, "revokeObjectURL", () => {});
  let currentUrl = tabUrl;
  let onUpdated = null;
  const body = { appendChild: node => { downloads.push(node); node.click = () => {}; node.remove = () => {}; } };
  globalThis.document = { getElementById: get, createElement: tag => doc.createElement(tag), body };
  globalThis.window = { addEventListener() {} };
  globalThis.chrome = { storage: { local: storage }, tabs: {
    query: (_o, cb) => cb([{ url: currentUrl }]),
    onActivated: { addListener() {} },
    onUpdated: { addListener(fn) { onUpdated = fn; } },
  } };
  t.after(() => { delete globalThis.document; delete globalThis.window; delete globalThis.chrome; });
  await import(`../src/sidepanel.js?final-report-test=${++sequence}`);
  await settle();
  const flush = async () => { for (let i = 0; i < 30; i += 1) await new Promise(r => setTimeout(r, 0)); };
  const click = async id => {
    for (const fn of get(id)._listeners.click || []) fn();
    await flush();
  };
  return {
    get, requests, downloads, logs, click, flush,
    finalRequests: () => requests.filter(r => r.path.includes("final-report")),
    navigateTo: async url => { currentUrl = url; onUpdated(0, { url }); await flush(); },
  };
}

test("boot makes no report request; Summary is the default view", async t => {
  const h = await harness(t);
  assert.equal(h.get("panel-ready").hidden, false);
  assert.deepEqual(h.finalRequests(), [], "boot stays unchanged");
  assert.equal(h.get("view-summary").hidden, false);
  assert.equal(h.get("view-detailed").hidden, true);
  assert.equal(h.get("view-risks").hidden, true);
  assert.equal(h.get("research-button").textContent, "Run legacy research");
});

test("Detailed Research finds this page's report by page key and shows it with downloads", async t => {
  const h = await harness(t);
  await h.click("tab-detailed");
  assert.equal(h.get("view-detailed").hidden, false);
  const lookups = h.requests.filter(r => r.path === "/v1/final-reports");
  assert.equal(lookups.length, 1);
  assert.equal(lookups[0].search, "?page=INFY");
  assert.ok(!h.requests.some(r => r.search.includes("symbol=")), "the page key is never sent as a symbol");
  assert.equal(lookups[0].init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.ok(!h.requests.some(r => r.init.method === "POST"), "an existing report is not regenerated");
  assert.match(h.get("final-report-status").textContent, /Ready — Infosys Ltd — as of 2026-10-12/);
  assert.equal(h.get("final-report-content").hidden, false);
  assert.match(h.get("final-report-content").textContent, /Revenue reported 100 INR CRORES/);
  assert.equal(h.get("final-report-downloads").hidden, false);
  for (const id of ["final-report-pdf", "final-report-html", "final-report-markdown", "final-report-json"]) {
    assert.equal(h.get(id).disabled, false);
  }
  for (const format of ["PDF", "HTML", "MARKDOWN", "JSON"]) {
    await h.click(`final-report-${format.toLowerCase()}`);
    assert.ok(h.requests.some(r => r.path === `/v1/final-reports/${ID}/files/${format}`));
  }
  assert.equal(h.downloads[0].download, "INFY_2026-10-12_detailed_research.pdf");
  assert.deepEqual(h.logs, []);
});

test("no report: opening Detailed Research generates it in the backend and shows Ready", async t => {
  const api = backend({ reports: [] });
  const h = await harness(t, { api });
  await h.click("tab-detailed");
  const post = h.requests.find(r => r.init.method === "POST");
  assert.equal(post.path, "/v1/final-reports");
  assert.deepEqual(JSON.parse(post.init.body), { source_url: "https://www.screener.in/company/INFY/" });
  assert.equal(post.init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.ok(h.requests.some(r => r.path === `/v1/final-report-jobs/${JOB}`), "the job is polled");
  assert.match(h.get("final-report-status").textContent, /Ready — Infosys Ltd/);
  assert.equal(h.get("final-report-downloads").hidden, false);
  assert.equal(h.get("final-report-error").hidden, true);
  await h.click("tab-risks");
  assert.equal(h.get("view-risks").hidden, false);
  assert.match(h.get("risks-content").textContent, /HIGH_ACCRUALS/);
  assert.match(h.get("risks-content").textContent, /Data limitations \(not company risks\)/);
  assert.equal(h.requests.filter(r => r.init.method === "POST").length, 1, "the risks view reuses the report");
});

test("generation shows progress, and a failed generation is a fixed typed message", async t => {
  const api = backend({ reports: [], jobs: [{ status: "RUNNING", stage: "BUILDING" },
    { status: "FAILED", stage: "DONE", failure: { code: "COMPANY_IDENTITY_MISMATCH", message: "<b>server text</b>" } }] });
  const h = await harness(t, { api });
  await h.click("tab-risks");
  assert.equal(h.get("final-report-error").hidden, false);
  assert.match(h.get("final-report-error").textContent, /identity could not be confirmed/);
  assert.doesNotMatch(h.get("final-report-error").textContent, /server text/);
  assert.match(h.get("risks-status").textContent, /identity could not be confirmed/);
  assert.equal(h.get("risks-generate-button").hidden, false);
  assert.equal(h.get("final-report-downloads").hidden, true);
  assert.equal(h.get("risks-content").hidden, true);
});

test("a partial report disables only the missing formats", async t => {
  const api = backend({ view: panelPayload({ status: "PARTIAL_RENDER", formats: ["JSON", "MARKDOWN", "HTML"], failed_formats: ["PDF"] }) });
  const h = await harness(t, { api });
  await h.click("tab-detailed");
  assert.equal(h.get("final-report-pdf").disabled, true);
  assert.equal(h.get("final-report-html").disabled, false);
  assert.match(h.get("final-report-status").textContent, /Partial: PDF could not be produced/);
  await h.click("final-report-pdf");
  assert.ok(!h.requests.some(r => r.path.endsWith("/files/PDF")), "no dead PDF download");
});

test("navigation RVNL -> INFY -> IRCON never shows a previous company's report or status", async t => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const views = { RVNL: panelPayload({ company_name: "Rail Vikas Nigam Ltd", screener_page: "RVNL" }) };
  const api = {
    async handle(parsed, init) {
      if (parsed.pathname === "/v1/final-reports" && init.method === "GET") {
        const page = parsed.searchParams.get("page");
        return json(200, { page, reports: page === "RVNL" ? [listing().reports[0]] : [] });
      }
      if (parsed.pathname === "/v1/final-reports" && init.method === "POST") {
        await gate; // INFY's generation is still starting when the user moves on
        return json(202, { job_id: JOB, status: "QUEUED", stage: "QUEUED", artifact_id: null, failure: null });
      }
      if (parsed.pathname === `/v1/final-report-jobs/${JOB}`) {
        return json(200, { job_id: JOB, status: "READY", stage: "DONE", artifact_id: ID, failure: null });
      }
      if (parsed.pathname === `/v1/final-reports/${ID}/view`) return json(200, views.RVNL);
      return null;
    },
  };
  const h = await harness(t, { tabUrl: "https://www.screener.in/company/RVNL/", api });
  await h.click("tab-detailed");
  assert.match(h.get("final-report-status").textContent, /Rail Vikas Nigam Ltd/);
  await h.navigateTo("https://www.screener.in/company/INFY/");
  assert.doesNotMatch(h.get("final-report-status").textContent, /Rail Vikas/);
  assert.equal(h.get("final-report-downloads").hidden, true);
  assert.equal(h.get("final-report-content").hidden, true);
  assert.match(h.get("final-report-status").textContent, /has not been generated/);
  h.get("final-report-button")._listeners.click.forEach(fn => fn());
  await h.flush();
  assert.match(h.get("final-report-status").textContent, /Generating Detailed Research/);
  await h.navigateTo("https://www.screener.in/company/IRCON/");
  release();
  await h.flush();
  assert.match(h.get("final-report-status").textContent, /has not been generated/,
    "INFY's late job never paints on the IRCON page");
  assert.doesNotMatch(h.get("final-report-status").textContent, /Rail Vikas|Infosys|Ready/);
  assert.equal(h.get("final-report-downloads").hidden, true);
  await h.click("final-report-pdf");
  assert.ok(!h.requests.some(r => r.path.includes("/files/")), "no stale download");
  const pages = h.requests.filter(r => r.path === "/v1/final-reports" && r.init.method === "GET")
    .map(r => r.search);
  assert.deepEqual(pages, ["?page=RVNL", "?page=INFY", "?page=IRCON"]);
});

test("401 on lookup returns to pairing", async t => {
  const api = { async handle(parsed) {
    if (parsed.pathname === "/v1/final-reports") return json(401, { detail: { code: "UNAUTHORIZED", message: "x" } });
    return null;
  } };
  const h = await harness(t, { api });
  await h.click("tab-detailed");
  assert.equal(h.get("panel-unpaired").hidden, false);
  assert.ok(!h.requests.some(r => r.init.method === "POST"), "no generation without pairing");
});

test("a backend failure is shown as a fixed message, never raw server text", async t => {
  const api = { async handle(parsed) {
    if (parsed.pathname === "/v1/final-reports") return json(500, { detail: { code: "X", message: "<script>secret path</script>" } });
    return null;
  } };
  const h = await harness(t, { api });
  await h.click("tab-detailed");
  assert.equal(h.get("final-report-error").hidden, false);
  assert.equal(h.get("final-report-error").textContent, "Could not look up Detailed Research.");
});

test("Detailed Research and Negatives state the ONE analytical basis and label the control basis", () => {
  const panel = parsePanel(panelPayload({ analytical_basis: "CONSOLIDATED", control_basis: "STANDALONE" }));
  assert.equal(panel.analyticalBasis, "CONSOLIDATED");
  assert.equal(panel.controlBasis, "STANDALONE");
  const detailed = renderDetailedResearch(createFakeDocument(), panel).serialize();
  assert.match(detailed, /Analytical basis: CONSOLIDATED · Control basis: STANDALONE \(reconciliation only, never a primary figure\)/);
  const risks = renderRisks(createFakeDocument(), panel).serialize();
  assert.match(risks, /Basis: CONSOLIDATED/);
  assert.ok(!risks.includes("STANDALONE"), "the control basis is never presented as the risks' basis");
});

test("an unknown or missing reporting basis is stated as such, never guessed", () => {
  const forged = parsePanel(panelPayload({ analytical_basis: "consolidated-ish", control_basis: "X" }));
  assert.equal(forged.analyticalBasis, null);
  assert.equal(forged.controlBasis, null);
  const missing = parsePanel(panelPayload());
  const detailed = renderDetailedResearch(createFakeDocument(), missing).serialize();
  assert.match(detailed, /Analytical basis: not stated/);
  assert.ok(!detailed.includes("Control basis"));
  assert.match(renderRisks(createFakeDocument(), missing).serialize(), /Basis: not stated/);
});

const COMPLETENESS = [
  ["Overall", "PARTIAL (business model NON_BANK_FINANCIAL; COMPLETE only when every material applicable requirement is complete)"],
  ["Complete", "Historical financials; Financial analytics (Box 5 core metrics)"],
  ["Partial", "Forecast (Box 10 core lines) (COMMON_EQUITY_NOT_FORECAST)"],
  ["Unavailable", "Peer analysis (Box 9 core benchmarks) (NO_ELIGIBLE_PEERS)"],
  ["Not applicable", "None"],
  ["Optional (not counted)", "Order book: NOT_APPLICABLE"],
];

test("research completeness is parsed apart from the rendering status and shown in Detailed Research", () => {
  const panel = parsePanel(panelPayload({
    status: "COMPLETE", research_status: "PARTIAL", research_completeness: COMPLETENESS,
    analytical_basis: "CONSOLIDATED", control_basis: "STANDALONE",
  }));
  assert.equal(panel.status, "COMPLETE", "rendering / execution status is unchanged");
  assert.equal(panel.researchStatus, "PARTIAL", "research completeness is its own field");
  assert.equal(researchStatusLabel(panel), "Research completeness: PARTIAL");
  const html = renderDetailedResearch(createFakeDocument(), panel).serialize();
  assert.match(html, /Research completeness: PARTIAL/);
  assert.match(html, /Unavailable: <\/strong><span>Peer analysis \(Box 9 core benchmarks\) \(NO_ELIGIBLE_PEERS\)/);
  assert.match(html, /Analytical basis: CONSOLIDATED · Control basis: STANDALONE/, "basis labels intact");
});

test("a malformed or missing research status is never shown as COMPLETE", () => {
  for (const research_status of ["complete", "DONE", 1, null, undefined]) {
    const panel = parsePanel(panelPayload({ status: "COMPLETE", research_status,
      research_completeness: [["Overall", 5], "junk", ["Complete", "x", "y"]] }));
    assert.equal(panel.researchStatus, null);
    assert.deepEqual(panel.researchCompleteness, []);
    assert.equal(researchStatusLabel(panel), "Research completeness: not assessed");
    const html = renderDetailedResearch(createFakeDocument(), panel).serialize();
    assert.ok(!html.includes("Research completeness: COMPLETE"));
    assert.match(html, /predates the completeness ledger/);
  }
});

test("the ready line states execution and research completeness separately", async t => {
  const api = backend({ view: panelPayload({ status: "COMPLETE", research_status: "PARTIAL",
    research_completeness: COMPLETENESS }) });
  const h = await harness(t, { api });
  await h.click("tab-detailed");
  assert.match(h.get("final-report-status").textContent,
    /Ready — Infosys Ltd — as of 2026-10-12\. Research completeness: PARTIAL\./);
  assert.match(h.get("final-report-content").textContent, /Research completeness: PARTIAL/);
});

test("a non-default basis choice is explained in Detailed Research", () => {
  const panel = parsePanel(panelPayload({ analytical_basis: "STANDALONE",
    basis_note: "Standalone selected because the consolidated statements are older (consolidated FY ends 2022-03-31, standalone 2026-03-31)." }));
  const html = renderDetailedResearch(createFakeDocument(), panel).serialize();
  assert.match(html, /Analytical basis: STANDALONE/);
  assert.match(html, /Standalone selected because the consolidated statements are older/);
  assert.equal(parsePanel(panelPayload({ basis_note: 7 })).basisNote, null);
});
