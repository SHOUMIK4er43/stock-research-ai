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
  globalThis.document = { getElementById: get };
  globalThis.window = { addEventListener() {} };
  globalThis.chrome = { storage: { local: storage }, tabs: {
    query: (_options, cb) => cb([{ url: "https://www.screener.in/company/INFY/" }]),
    onActivated: { addListener() {} },
  } };
  t.after(() => { delete globalThis.document; delete globalThis.window; delete globalThis.chrome; });
  await import(`../src/sidepanel.js?boot-test=${++sequence}`);
  await settle();
  return { get, state, requests, storage, logs, retry: () => get("retry-service").events.click() };
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
