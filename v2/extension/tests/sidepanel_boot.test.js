import { test } from "node:test";
import assert from "node:assert/strict";
import { createMemoryBackend } from "../src/storage.js";

const VERSION = { api_version: "v1", application_version: "0.1.0",
  report_schema_version: "6A.1", supported_research_plans: ["FULL_RESEARCH_V1"],
  supported_portals: ["SCREENER"] };
let sequence = 0;
const settle = () => new Promise(resolve => setImmediate(resolve));

async function bootHarness(t, options = {}) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, { hidden: true, textContent: "", events: {},
      addEventListener(event, fn) { this.events[event] = fn; } });
    return elements.get(id);
  };
  const storage = createMemoryBackend();
  await storage.set(options.stored || {});
  const requests = [];
  const logs = [];
  for (const method of ["log", "warn", "error"]) t.mock.method(console, method, (...args) => logs.push(args));
  const state = { health: { status: "ok" }, version: VERSION, ...options };
  t.mock.method(globalThis, "fetch", async (url, init) => {
    const path = new URL(url).pathname;
    requests.push({ path, ...init });
    assert.equal(init.method, "GET", "boot must never submit research or pair");
    if (state.fetch) return state.fetch(path, init);
    if (state.offline) throw new TypeError("offline");
    const payload = path === "/health" ? state.health : path === "/v1/version" ? state.version : {};
    return { ok: true, status: 200, json: async () => payload };
  });
  let tabUrl = options.tabUrl || "https://www.screener.in/company/INFY/";
  let onUpdatedListener = null;
  globalThis.document = { getElementById: get };
  globalThis.window = { addEventListener() {} };
  globalThis.chrome = { storage: { local: storage }, tabs: {
    query: (_options, cb) => cb([{ url: tabUrl }]),
    onActivated: { addListener() {} },
    onUpdated: { addListener(fn) { onUpdatedListener = fn; } },
  } };
  t.after(() => { delete globalThis.document; delete globalThis.window; delete globalThis.chrome; });
  await import(`../src/sidepanel.js?boot-test=${++sequence}`);
  await settle();
  return { get, state, requests, storage, logs, retry: () => get("retry-service").events.click(),
    // Simulates the active tab's URL changing (SUMMARY_V1 Box 1 navigation), the same
    // way a real chrome.tabs.onUpdated event with a new url would trigger a re-boot.
    navigateTo: async url => {
      tabUrl = url;
      if (onUpdatedListener) await onUpdatedListener(0, { url });
      await settle();
    } };
}

function unpaired(h) {
  assert.equal(h.get("service-status-line").textContent, "Service reachable · not paired");
  assert.equal(h.get("panel-unpaired").hidden, false);
  assert.equal(h.get("panel-service-unavailable").hidden, true);
}

test("real controller accepts exact live health/version payloads and renders UNPAIRED", async t => {
  const h = await bootHarness(t);
  unpaired(h);
  assert.deepEqual(h.requests.map(r => r.path), ["/health", "/v1/version"]);
  assert.ok(h.requests.every(r => Object.keys(r.headers).length === 0));
  assert.deepEqual(h.logs, []);
});

test("actual Retry button performs a complete boot and recovers", async t => {
  const h = await bootHarness(t, { offline: true });
  assert.equal(h.get("panel-service-unavailable").hidden, false);
  h.state.offline = false;
  await h.retry();
  unpaired(h);
});

for (const delayedPath of ["/health", "/v1/version"]) {
test(`an older failed ${delayedPath} probe cannot overwrite a successful retry`, async t => {
  let failOld;
  let first = true;
  const h = await bootHarness(t, { fetch: async path => {
    if (first && path === delayedPath) {
      first = false;
      await new Promise((_resolve, reject) => { failOld = reject; });
    }
    return { ok: true, status: 200, json: async () => path === "/health" ? { status: "ok" } : VERSION };
  } });
  await h.retry();
  unpaired(h);
  failOld(new TypeError("old failed request"));
  await settle();
  unpaired(h);
});
}

test("incompatible response after unavailable shows reachable/incompatible", async t => {
  const h = await bootHarness(t, { offline: true });
  h.state.offline = false;
  h.state.version = { ...VERSION, api_version: "v2" };
  await h.retry();
  assert.equal(h.get("panel-incompatible").hidden, false);
  assert.equal(h.get("panel-service-unavailable").hidden, true);
  assert.equal(h.get("service-status-line").textContent, "Service reachable · incompatible version");
});

for (const [name, response] of [["malformed health", { health: {} }], ["null health", { health: null }],
  ["null version", { version: null }], ["malformed version", { version: {} }]]) {
  test(`${name} never reaches paired/research state`, async t => {
    const h = await bootHarness(t, response);
    assert.equal(h.get("panel-ready").hidden, true);
    assert.equal(h.get("panel-unpaired").hidden, true);
    assert.ok(!h.get("panel-incompatible").hidden || !h.get("panel-service-unavailable").hidden);
    assert.ok(h.requests.every(r => r.method === "GET"));
  });
}

test("stored token retains paired path without printing credentials or submitting jobs", async t => {
  const h = await bootHarness(t, { stored: { pairingToken: "synthetic-token" } });
  assert.equal(h.get("panel-ready").hidden, false);
  assert.equal(h.get("service-status-line").textContent, "Service reachable · paired");
  assert.deepEqual(h.requests.map(r => r.path), ["/health", "/v1/version", "/v1/capabilities"]);
  assert.deepEqual(h.logs, []);
});

test("401 report handling still clears invalid token and returns to pairing", async t => {
  const h = await bootHarness(t, { stored: { pairingToken: "synthetic-expired-token",
    lastReport: { reportId: "test-report", sourceUrl: "https://www.screener.in/company/INFY/" } },
    fetch: async (path, init) => {
      if (path.startsWith("/v1/reports/")) {
        assert.equal(init.headers.Authorization, "Bearer synthetic-expired-token");
        return { ok: false, status: 401, json: async () => ({ detail: "Unauthorized" }) };
      }
      return { ok: true, status: 200, json: async () => path === "/health" ? { status: "ok" } : path === "/v1/version" ? VERSION : {} };
    } });
  await h.get("view-last-research-button").events.click();
  unpaired(h);
  assert.equal((await h.storage.get("pairingToken")).pairingToken, undefined);
  assert.deepEqual(h.logs, []);
});

test("reconnect 401 selects pairing and preserves active-job/report recovery pointers", async t => {
  const activeJob = { jobId: "existing-job", sourceUrl: "https://www.screener.in/company/INFY/" };
  const lastReport = { reportId: "previous-report", sourceUrl: activeJob.sourceUrl };
  const h = await bootHarness(t, {
    stored: { pairingToken: "synthetic-expired", activeJob, lastReport },
    fetch: async path => {
      if (path.startsWith("/v1/research-jobs/")) {
        return { ok: false, status: 401, json: async () => ({ detail: { code: "UNAUTHORIZED" } }) };
      }
      return { ok: true, status: 200, json: async () => path === "/health"
        ? { status: "ok" } : path === "/v1/version" ? VERSION : {} };
    },
  });
  unpaired(h);
  const stored = await h.storage.get(["activeJob", "lastReport", "pairingToken"]);
  assert.deepEqual(stored.activeJob, activeJob);
  assert.deepEqual(stored.lastReport, lastReport);
  assert.equal(stored.pairingToken, undefined);
  assert.deepEqual(h.logs, []);
});

test("polling 401 stops polling and keeps recovery pointers for re-pairing", async t => {
  let poll;
  let scheduled = 0;
  let jobReads = 0;
  t.mock.method(globalThis, "setTimeout", fn => { poll = fn; scheduled += 1; return 42; });
  t.mock.method(globalThis, "clearTimeout", () => {});
  const activeJob = { jobId: "existing-job", sourceUrl: "https://www.screener.in/company/INFY/" };
  const h = await bootHarness(t, {
    stored: { pairingToken: "synthetic-expired", activeJob },
    fetch: async path => {
      if (path.startsWith("/v1/research-jobs/")) {
        jobReads += 1;
        return jobReads === 1
          ? { ok: true, status: 200, json: async () => ({ status: "RUNNING" }) }
          : { ok: false, status: 401, json: async () => ({ detail: { code: "UNAUTHORIZED" } }) };
      }
      return { ok: true, status: 200, json: async () => path === "/health"
        ? { status: "ok" } : path === "/v1/version" ? VERSION : {} };
    },
  });
  assert.equal(h.get("panel-running").hidden, false);
  await poll();
  unpaired(h);
  assert.equal(scheduled, 1);
  assert.deepEqual((await h.storage.get("activeJob")).activeJob, activeJob);
  assert.deepEqual(h.logs, []);
});

// --- SUMMARY_V1 Box 1: generic Screener page identity -------------------------------

function assertNoResearchOrPairSubmission(h) {
  // The global fetch mock already asserts every single request is GET; this is an
  // additional, explicit proof that no research-job or pairing endpoint was ever hit.
  for (const r of h.requests) {
    assert.ok(!r.path.startsWith("/v1/research-jobs"), "must never submit a research job");
    assert.ok(r.path !== "/v1/pair", "must never submit a pairing code");
  }
}

test("READY page shows identifier/mode/pending-company-name for a standalone page", async t => {
  const h = await bootHarness(t, {
    stored: { pairingToken: "synthetic-token" },
    tabUrl: "https://www.screener.in/company/INFY/",
  });
  assert.equal(h.get("panel-ready").hidden, false);
  assert.equal(
    h.get("current-page-label").textContent,
    "Screener company detected\nIdentifier: INFY\nPage: Standalone\nCompany name: Pending data fetch",
  );
  assertNoResearchOrPairSubmission(h);
});

test("READY page shows Consolidated mode for a consolidated page", async t => {
  const h = await bootHarness(t, {
    stored: { pairingToken: "synthetic-token" },
    tabUrl: "https://www.screener.in/company/TCS/consolidated/",
  });
  assert.equal(
    h.get("current-page-label").textContent,
    "Screener company detected\nIdentifier: TCS\nPage: Consolidated\nCompany name: Pending data fetch",
  );
  assertNoResearchOrPairSubmission(h);
});

test("no company name is ever invented on the READY page label", async t => {
  const h = await bootHarness(t, {
    stored: { pairingToken: "synthetic-token" },
    tabUrl: "https://www.screener.in/company/INFY/",
  });
  const text = h.get("current-page-label").textContent;
  assert.match(text, /Pending data fetch/);
  assert.doesNotMatch(text, /Infosys/i);
});

test("active tab change from one supported company to another replaces the identity", async t => {
  const h = await bootHarness(t, {
    stored: { pairingToken: "synthetic-token" },
    tabUrl: "https://www.screener.in/company/INFY/",
  });
  assert.match(h.get("current-page-label").textContent, /Identifier: INFY/);

  await h.navigateTo("https://www.screener.in/company/TCS/");
  const text = h.get("current-page-label").textContent;
  assert.match(text, /Identifier: TCS/);
  assert.doesNotMatch(text, /INFY/, "stale previous company identity must not remain");
  assertNoResearchOrPairSubmission(h);
});

test("navigating from a consolidated page to a standalone page updates the mode", async t => {
  const h = await bootHarness(t, {
    stored: { pairingToken: "synthetic-token" },
    tabUrl: "https://www.screener.in/company/INFY/consolidated/",
  });
  assert.match(h.get("current-page-label").textContent, /Page: Consolidated/);

  await h.navigateTo("https://www.screener.in/company/INFY/");
  assert.match(h.get("current-page-label").textContent, /Page: Standalone/);
  assertNoResearchOrPairSubmission(h);
});

test("navigating from a supported company page to an unsupported page clears the identity", async t => {
  const h = await bootHarness(t, {
    stored: { pairingToken: "synthetic-token" },
    tabUrl: "https://www.screener.in/company/INFY/",
  });
  assert.equal(h.get("panel-ready").hidden, false);

  await h.navigateTo("https://www.screener.in/screens/");
  assert.equal(h.get("panel-unsupported").hidden, false);
  assert.equal(h.get("panel-ready").hidden, true);
  const text = h.get("current-page-label").textContent;
  assert.doesNotMatch(text, /Identifier:/);
  assert.doesNotMatch(text, /INFY/);
  assertNoResearchOrPairSubmission(h);
});

test("no request is submitted while refreshing page context across a navigation", async t => {
  const h = await bootHarness(t, {
    stored: { pairingToken: "synthetic-token" },
    tabUrl: "https://www.screener.in/company/INFY/",
  });
  await h.navigateTo("https://www.screener.in/company/TCS/");
  assert.ok(h.requests.length > 0, "boot must still probe health/version/capabilities");
  assertNoResearchOrPairSubmission(h);
  assert.deepEqual(h.logs, []);
});

test("an older tab lookup cannot replace the company used by a newer boot", async t => {
  const h = await bootHarness(t, {
    stored: { pairingToken: "synthetic-token" },
    tabUrl: "https://www.screener.in/company/INFY/",
  });
  const lookups = [];
  globalThis.chrome.tabs.query = (_options, cb) => { lookups.push(cb); };
  let releaseHealth = null;
  h.state.fetch = async path => {
    if (path === "/health" && !releaseHealth) await new Promise(resolve => { releaseHealth = resolve; });
    return { ok: true, status: 200, json: async () => path === "/health"
      ? { status: "ok" } : path === "/v1/version" ? VERSION : {} };
  };

  await h.navigateTo("https://www.screener.in/company/INFY/"); // older lookup starts
  await h.navigateTo("https://www.screener.in/company/TCS/");  // newer lookup starts
  assert.equal(lookups.length, 2);

  lookups[1]([{ url: "https://www.screener.in/company/TCS/" }]); // newer resolves first...
  await settle();
  assert.ok(releaseHealth, "newer boot must be waiting on its next async step");
  lookups[0]([{ url: "https://www.screener.in/company/INFY/" }]); // ...stale older resolves late
  await settle();
  releaseHealth();
  await settle();
  await settle();

  const text = h.get("current-page-label").textContent;
  assert.match(text, /Identifier: TCS/);
  assert.doesNotMatch(text, /INFY/, "a stale lookup must never overwrite the newer page identity");
  assertNoResearchOrPairSubmission(h);
  assert.ok(h.requests.every(r => !r.path.startsWith("/v1/summaries")), "must never submit a summary");
});
