import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiClient, ApiError, FINAL_REPORT_FORMATS } from "../src/api/client.js";
import { finalReportFilename, finalReportLabel, latestFinalReport } from "../src/final_report.js";
import { createMemoryBackend } from "../src/storage.js";
import { createFakeDocument } from "./fixtures/fake_dom.js";

// Box 15: the Detailed Research dossier is a stored, backend-rendered final report. The
// extension only looks it up (on click), lists it and downloads its files.

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

test("latestFinalReport accepts only well-formed entries and closed formats", () => {
  assert.equal(latestFinalReport(null), null);
  assert.equal(latestFinalReport({}), null);
  assert.equal(latestFinalReport({ reports: [{ artifact_id: "../x", as_of: "2026-10-12" }] }), null);
  assert.equal(latestFinalReport({ reports: [{ artifact_id: ID, as_of: "yesterday" }] }), null);
  const r = latestFinalReport(listing({ formats: ["PDF", "EXE", "../report"], failed_formats: ["HTML"] }));
  assert.deepEqual(r.formats, ["PDF"]);
  assert.deepEqual(r.failed, ["HTML"]);
  assert.equal(r.artifactId, ID);
});

test("labels and file names come from backend fields only and are sanitised", () => {
  assert.match(finalReportLabel(null), /No Detailed Research dossier/);
  const r = latestFinalReport(listing({ status: "PARTIAL_RENDER", formats: ["JSON"], failed_formats: ["PDF"] }));
  assert.match(finalReportLabel(r), /Infosys Ltd — as of 2026-10-12\. Partial: PDF could not be produced\./);
  const evil = latestFinalReport(listing({ nse_symbol: '../"\r\nX' }));
  assert.match(finalReportFilename(evil, "PDF"), /^[A-Za-z0-9_-]+_2026-10-12_detailed_research\.pdf$/);
  assert.equal(finalReportFilename(r, "MARKDOWN"), "INFY_2026-10-12_detailed_research.md");
});

test("listFinalReports is one authenticated GET with an encoded identifier", async () => {
  const calls = [];
  const client = new ApiClient(BASE, async (url, init) => { calls.push({ url, init }); return json(200, listing()); });
  const payload = await client.listFinalReports("tok", "M&M/../x");
  assert.equal(payload.reports.length, 1);
  assert.equal(calls[0].url, `${BASE}/v1/final-reports?symbol=M%26M%2F..%2Fx`);
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[0].init.headers.Authorization, "Bearer tok");
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
    return { ok: true, status: 200, blob: async () => blob,
      headers: { get: () => "application/pdf" } };
  });
  const result = await ok.getFinalReportFile("tok", ID, "PDF");
  assert.equal(result.blob, blob);
  assert.equal(result.contentType, "application/pdf");
  assert.equal(calls[0].url, `${BASE}/v1/final-reports/${ID}/files/PDF`);
  assert.equal(calls[0].init.headers.Authorization, "Bearer tok");
  const missing = new ApiClient(BASE, async () => json(404, { detail: {
    code: "FINAL_REPORT_FILE_UNAVAILABLE", message: "x" } }));
  await assert.rejects(missing.getFinalReportFile("tok", ID, "HTML"),
    e => e instanceof ApiError && e.status === 404 && e.code === "FINAL_REPORT_FILE_UNAVAILABLE");
  const bad = new ApiClient("https://evil.example", async () => json(200, {}));
  await assert.rejects(bad.getFinalReportFile("tok", ID, "PDF"));
});

async function harness(t, { tabUrl = "https://www.screener.in/company/INFY/", list } = {}) {
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
  const respond = list || (async () => json(200, listing()));
  t.mock.method(globalThis, "fetch", async (url, init) => {
    const parsed = new URL(url);
    requests.push({ path: parsed.pathname, search: parsed.search, init });
    assert.equal(init.method, "GET", "the dossier flow never writes");
    if (parsed.pathname === "/v1/final-reports") return respond(parsed);
    if (parsed.pathname.startsWith("/v1/final-reports/")) {
      return { ok: true, status: 200, blob: async () => new Blob(["x"]), headers: { get: () => "" } };
    }
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
  const click = async id => {
    const pending = (get(id)._listeners.click || []).map(fn => fn());
    await settle();
    await Promise.all(pending);
    await settle();
  };
  return {
    get, requests, downloads, logs, click,
    navigateTo: async url => { currentUrl = url; onUpdated(0, { url }); await settle(); await settle(); },
  };
}

test("boot makes no dossier request; a click looks it up for this page and enables downloads", async t => {
  const h = await harness(t);
  assert.equal(h.get("panel-ready").hidden, false);
  assert.ok(h.requests.every(r => !r.path.startsWith("/v1/final-reports")), "boot stays unchanged");
  assert.equal(h.get("final-report-downloads").hidden, true);
  await h.click("final-report-button");
  const lookups = h.requests.filter(r => r.path === "/v1/final-reports");
  assert.equal(lookups.length, 1);
  assert.equal(lookups[0].search, "?symbol=INFY");
  assert.equal(lookups[0].init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.match(h.get("final-report-status").textContent, /Infosys Ltd — as of 2026-10-12/);
  assert.equal(h.get("final-report-downloads").hidden, false);
  for (const id of ["final-report-pdf", "final-report-html", "final-report-markdown", "final-report-json"]) {
    assert.equal(h.get(id).disabled, false);
  }
  await h.click("final-report-pdf");
  const file = h.requests.find(r => r.path.endsWith("/files/PDF"));
  assert.equal(file.path, `/v1/final-reports/${ID}/files/PDF`);
  assert.equal(h.downloads[0].download, "INFY_2026-10-12_detailed_research.pdf");
  assert.deepEqual(h.logs, []);
});

test("a partial dossier disables only the missing formats", async t => {
  const h = await harness(t, { list: async () => json(200, listing({
    status: "PARTIAL_RENDER", formats: ["JSON", "MARKDOWN", "HTML"], failed_formats: ["PDF"] })) });
  await h.click("final-report-button");
  assert.equal(h.get("final-report-pdf").disabled, true);
  assert.equal(h.get("final-report-html").disabled, false);
  assert.match(h.get("final-report-status").textContent, /Partial: PDF could not be produced/);
});

test("no dossier and an unknown identifier both say so without an error", async t => {
  const empty = await harness(t, { list: async () => json(200, { symbol: "INFY", reports: [] }) });
  await empty.click("final-report-button");
  assert.match(empty.get("final-report-status").textContent, /No Detailed Research dossier/);
  assert.equal(empty.get("final-report-downloads").hidden, true);
  assert.equal(empty.get("final-report-error").hidden, true);
});

test("a dossier never follows the user to another company's page", async t => {
  const h = await harness(t);
  await h.click("final-report-button");
  assert.equal(h.get("final-report-downloads").hidden, false);
  await h.navigateTo("https://www.screener.in/company/TCS/consolidated/");
  assert.equal(h.get("final-report-downloads").hidden, true);
  assert.equal(h.get("final-report-status").hidden, true);
  await h.click("final-report-pdf");
  assert.ok(!h.requests.some(r => r.path.includes("/files/")), "no stale download");
});

test("401 on lookup returns to pairing", async t => {
  const h = await harness(t, { list: async () => json(401, { detail: { code: "UNAUTHORIZED", message: "x" } }) });
  await h.click("final-report-button");
  assert.equal(h.get("panel-unpaired").hidden, false);
});

test("a backend failure is shown as a fixed message, never raw server text", async t => {
  const h = await harness(t, { list: async () => json(500, { detail: { code: "X", message: "<script>secret path</script>" } }) });
  await h.click("final-report-button");
  assert.equal(h.get("final-report-error").hidden, false);
  assert.equal(h.get("final-report-error").textContent, "Could not look up the dossier.");
});
