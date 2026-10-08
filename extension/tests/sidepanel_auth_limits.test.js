import { test } from "node:test";
import assert from "node:assert/strict";
import { createMemoryBackend } from "../src/storage.js";
import { createFakeDocument } from "./fixtures/fake_dom.js";
import { failureLabel } from "../src/state.js";
import { summaryErrorLabel } from "../src/summary.js";

// P1 security remediation: an expired, revoked or replaced pairing token (401) on any
// protected call returns the panel to "pair again" and forgets the token; the server's
// typed 429 limits render as clear messages. The token never reaches the DOM or logs.

const VERSION = { api_version: "v1", application_version: "0.1.0",
  report_schema_version: "6A.1", supported_research_plans: ["FULL_RESEARCH_V1"],
  supported_portals: ["SCREENER"] };
const TOKEN = "synthetic-stale-pairing-token";
const settle = () => new Promise(resolve => setImmediate(resolve));
const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
let sequence = 0;

async function harness(t, respond) {
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
  const posts = [];
  const logs = [];
  for (const method of ["log", "warn", "error"]) t.mock.method(console, method, (...a) => logs.push(a));
  t.mock.method(globalThis, "fetch", async (url, init) => {
    const path = new URL(url).pathname;
    if (init.method !== "GET") {
      posts.push({ path, headers: init.headers });
      return respond(path);
    }
    return json(200, path === "/health" ? { status: "ok" } : path === "/v1/version" ? VERSION : {});
  });
  globalThis.document = { getElementById: get, createElement: tag => doc.createElement(tag) };
  globalThis.window = { addEventListener() {} };
  globalThis.chrome = { storage: { local: storage }, tabs: {
    query: (_o, cb) => cb([{ url: "https://www.screener.in/company/INFY/" }]),
    onActivated: { addListener() {} },
    onUpdated: { addListener() {} },
  } };
  t.after(() => { delete globalThis.document; delete globalThis.window; delete globalThis.chrome; });
  await import(`../src/sidepanel.js?auth-limits-test=${++sequence}`);
  await settle();
  const click = async id => {
    await Promise.all((get(id)._listeners.click || []).map(fn => fn()));
    await settle();
  };
  const renderedText = () => [...elements.values()].map(node => node.textContent).join("\n");
  return { get, posts, logs, storage, click, renderedText };
}

test("401 on research launch (expired/revoked token) clears it and returns to pairing", async t => {
  const h = await harness(t, async () => json(401, { detail: { code: "UNAUTHORIZED", message: "x" } }));
  assert.equal(h.get("panel-ready").hidden, false);
  await h.click("research-button");
  assert.equal(h.posts.length, 1);
  assert.equal(h.posts[0].path, "/v1/research-jobs");
  assert.equal(h.get("panel-unpaired").hidden, false);
  assert.equal(h.get("panel-failed").hidden, true);
  assert.equal(await h.storage.get("pairingToken").then(s => s.pairingToken), undefined);
  assert.match(h.get("pairing-error").textContent, /Pair again/);
  assert.doesNotMatch(h.renderedText(), new RegExp(TOKEN));
  assert.deepEqual(h.logs, []);
});

test("401 on a summary also clears the stale token", async t => {
  const h = await harness(t, async () => json(401, { detail: { code: "UNAUTHORIZED", message: "x" } }));
  await h.click("generate-summary-button");
  assert.equal(h.get("panel-unpaired").hidden, false);
  assert.equal(await h.storage.get("pairingToken").then(s => s.pairingToken), undefined);
  assert.doesNotMatch(h.renderedText(), new RegExp(TOKEN));
});

test("429 research limit keeps pairing and shows a clear message", async t => {
  const h = await harness(t, async () =>
    json(429, { detail: { code: "RESEARCH_JOB_LIMIT_REACHED", message: "At most 2" } }));
  await h.click("research-button");
  assert.equal(h.get("panel-failed").hidden, false);
  assert.equal(h.get("failure-label").textContent, failureLabel("RESEARCH_JOB_LIMIT_REACHED"));
  assert.match(h.get("failure-label").textContent, /already running/);
  assert.equal(await h.storage.get("pairingToken").then(s => s.pairingToken), TOKEN);
});

test("429 summary rate limit keeps pairing and shows a clear message", async t => {
  const h = await harness(t, async () =>
    json(429, { detail: { code: "SUMMARY_RATE_LIMITED", message: "Too many", retryable: true } }));
  await h.click("generate-summary-button");
  assert.equal(h.get("summary-error").hidden, false);
  assert.equal(h.get("summary-error").textContent, summaryErrorLabel("SUMMARY_RATE_LIMITED"));
  assert.match(h.get("summary-error").textContent, /Too many summaries/);
  assert.equal(await h.storage.get("pairingToken").then(s => s.pairingToken), TOKEN);
});
