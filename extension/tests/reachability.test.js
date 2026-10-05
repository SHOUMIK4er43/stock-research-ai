import { test } from "node:test";
import assert from "node:assert/strict";
import { createMemoryBackend } from "../src/storage.js";

const ORIGIN = "chrome-extension://ieplifjjheknpihonflfnnofbdlciepa";

test("actual sidepanel boot reaches pairing when health and version permit its exact origin", async (t) => {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, { hidden: false, textContent: "", addEventListener() {} });
    return elements.get(id);
  };
  const events = {};
  t.mock.method(globalThis, "fetch", async (url, init) => {
    assert.equal(init.headers.Authorization, undefined);
    assert.deepEqual(init.headers, {}); // no API key or bearer needed for readiness
    const path = new URL(url).pathname;
    assert.ok(["/health", "/v1/version"].includes(path));
    // Model a browser enforcing the response CORS contract, not a permissive curl client.
    if (events.allowOrigin !== ORIGIN) throw new TypeError("Failed to fetch: CORS");
    return { ok: true, status: 200, json: async () => path === "/health"
      ? { status: "ok" } : { api_version: "v1", application_version: "0.1.0" } };
  });
  globalThis.document = { getElementById: get };
  globalThis.window = { addEventListener() {} };
  globalThis.chrome = {
    storage: { local: createMemoryBackend() },
    tabs: { query: (_options, callback) => callback([{ url: "https://www.screener.in/company/INFY/" }]),
      onActivated: { addListener(fn) { events.boot = fn; } } },
  };
  t.after(() => { delete globalThis.document; delete globalThis.window; delete globalThis.chrome; });
  await import("../src/sidepanel.js");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(get("service-status-line").textContent, "Local service not reachable");
  events.allowOrigin = ORIGIN;
  await events.boot();
  assert.equal(get("service-status-line").textContent, "Service reachable · not paired");
  assert.equal(get("panel-service-unavailable").hidden, true);
  assert.equal(get("panel-unpaired").hidden, false);
});
