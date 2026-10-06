import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createMemoryBackend } from "../src/storage.js";
import { createFakeDocument } from "./fixtures/fake_dom.js";

// SUMMARY_V1 Box 4: the real side panel controller against a fake local backend. The
// backend stub plays the role of server-side extraction: company names come only from
// its response, keyed by the URL the extension sent - never from extension logic.

const VERSION = { api_version: "v1", application_version: "0.1.0",
  report_schema_version: "6A.1", supported_research_plans: ["FULL_RESEARCH_V1"],
  supported_portals: ["SCREENER"] };
const TOKEN = "synthetic-pairing-token";
const settle = () => new Promise(resolve => setImmediate(resolve));
let sequence = 0;

const PAGES = {
  "https://www.screener.in/company/INFY/": ["Infosys Ltd", "1489", "standalone"],
  "https://www.screener.in/company/TCS/consolidated/": ["Tata Consultancy Services Ltd", "3365", "consolidated"],
  "https://www.screener.in/company/HDFCBANK/consolidated/": ["HDFC Bank Ltd", "1295", "consolidated"],
  "https://www.screener.in/company/TATAPOWER/consolidated/": ["Tata Power Company Ltd", "3361", "consolidated"],
  "https://www.screener.in/company/GAIL/": ["GAIL (India) Ltd", "1163", "standalone"],
  "https://www.screener.in/company/ZYDUSLIFE/consolidated/": ["Zydus Lifesciences Ltd", "3556", "consolidated"],
};

function summaryResult(sourceUrl, overrides = {}) {
  const [name, key, scope] = PAGES[sourceUrl];
  return {
    schema_version: "SUMMARY_RESULT_V1",
    summary_id: "00000000-0000-4000-8000-000000000001",
    company: { name, source_company_key: key, reporting_scope: scope },
    source_url: sourceUrl,
    data_retrieved_at: "2026-10-06T09:00:00Z",
    generated_at: "2026-10-06T09:00:05Z",
    input_sha256: "a".repeat(64),
    prompt_version: "SUMMARY_PROMPT_V1",
    summary_text: `Company overview\n${name} figures from the supplied data.\n\nOverall summary\nNeutral.`,
    limitations: [],
    provider: "aicredits",
    requested_model: "anthropic/claude-sonnet-5",
    reported_model: "claude-sonnet-5",
    finish_reason: "stop",
    usage: { prompt_tokens: 3000, completion_tokens: 600 },
    ...overrides,
  };
}

const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

async function harness(t, { tabUrl = "https://www.screener.in/company/INFY/", summary } = {}) {
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
  const respond = summary || (async body => json(200, summaryResult(body.source_url)));
  t.mock.method(globalThis, "fetch", async (url, init) => {
    const path = new URL(url).pathname;
    if (init.method !== "GET") {
      assert.equal(init.method, "POST");
      assert.equal(path, "/v1/summaries", "the only write this flow may make is a summary request");
      const body = JSON.parse(init.body);
      posts.push({ path, headers: init.headers, body });
      return respond(body);
    }
    const payload = path === "/health" ? { status: "ok" } : path === "/v1/version" ? VERSION : {};
    return json(200, payload);
  });
  let currentUrl = tabUrl;
  let onUpdated = null;
  globalThis.document = { getElementById: get, createElement: tag => doc.createElement(tag) };
  globalThis.window = { addEventListener() {} };
  globalThis.chrome = { storage: { local: storage }, tabs: {
    query: (_o, cb) => cb([{ url: currentUrl }]),
    onActivated: { addListener() {} },
    onUpdated: { addListener(fn) { onUpdated = fn; } },
  } };
  t.after(() => { delete globalThis.document; delete globalThis.window; delete globalThis.chrome; });
  await import(`../src/sidepanel.js?summary-test=${++sequence}`);
  await settle();
  const click = async id => {
    const pending = (get(id)._listeners.click || []).map(fn => fn());
    await settle();
    return Promise.all(pending);
  };
  return {
    get, posts, logs,
    generate: () => click("generate-summary-button"),
    navigateTo: async url => { currentUrl = url; onUpdated(0, { url }); await settle(); await settle(); },
    view: () => ({
      button: get("generate-summary-button").textContent,
      disabled: get("generate-summary-button").disabled,
      loading: !get("summary-loading").hidden,
      error: get("summary-error").hidden ? null : get("summary-error").textContent,
      result: get("summary-result").hidden ? null : get("summary-result").textContent,
    }),
  };
}

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}

for (const [url, [name, , scope]] of Object.entries(PAGES)) {
  test(`${name}: detected page -> one summary request -> backend company rendered`, async t => {
    const h = await harness(t, { tabUrl: url });
    assert.equal(h.get("panel-ready").hidden, false);
    await h.generate();
    assert.equal(h.posts.length, 1);
    assert.deepEqual(h.posts[0].body, { portal: "SCREENER", source_url: url });
    const view = h.view();
    assert.equal(view.loading, false);
    assert.equal(view.error, null);
    assert.match(view.result, new RegExp(name.replace(/[()]/g, "\\$&")));
    assert.match(view.result, scope === "standalone" ? /Standalone/ : /Consolidated/);
    assert.match(view.result, /Overall summary/);
    assert.equal(view.button, "Regenerate Summary");
    assert.deepEqual(h.logs, []);
  });
}

test("idle state before any click, then a visibly different loading state", async t => {
  const gate = deferred();
  const h = await harness(t, { summary: () => gate.promise });
  assert.deepEqual(h.view(), {
    button: "Generate Summary", disabled: false, loading: false, error: null, result: null,
  });
  const pending = h.generate();
  await settle();
  assert.deepEqual(h.view(), {
    button: "Generating…", disabled: true, loading: true, error: null, result: null,
  });
  await h.generate(); // a second click while loading never sends a second request
  assert.equal(h.posts.length, 1);
  gate.resolve(json(200, summaryResult("https://www.screener.in/company/INFY/")));
  await pending;
  await settle();
  assert.equal(h.view().loading, false);
  assert.match(h.view().result, /Infosys Ltd/);
});

for (const [status, code, expected] of [
  [504, "PROVIDER_TIMEOUT", /did not respond in time/],
  [503, "CREDENTIAL_UNAVAILABLE", /credential is not available/],
  [502, "COMPANY_DATA_UNAVAILABLE", /could not be read from Screener/],
  [502, "SUMMARY_TRUNCATED", /could not be used/],
  [502, "SOMETHING_NEW", /unknown reason/],
]) {
  test(`backend error ${code} shows a fixed safe message, never server text`, async t => {
    const h = await harness(t, { summary: async () =>
      json(status, { detail: { code, message: "SERVER-TEXT-MARKER Bearer secret", retryable: true } }) });
    await h.generate();
    const view = h.view();
    assert.match(view.error, expected);
    assert.doesNotMatch(view.error, /SERVER-TEXT-MARKER|Bearer|secret/);
    assert.equal(view.result, null);
    assert.equal(view.loading, false);
    assert.equal(view.button, "Generate Summary");
  });
}

test("unreachable local service shows a safe network message", async t => {
  const h = await harness(t, { summary: async () => { throw new TypeError("socket hang up SECRET"); } });
  await h.generate();
  assert.equal(h.view().error, "The local service is not reachable.");
});

for (const [label, mutate] of [
  ["wrong schema", r => ({ ...r, schema_version: "SUMMARY_RESULT_V2" })],
  ["missing text", r => { const { summary_text, ...rest } = r; return rest; }],
  ["blank text", r => ({ ...r, summary_text: "   " })],
  ["missing company", r => ({ ...r, company: null })],
  ["bad scope", r => ({ ...r, company: { ...r.company, reporting_scope: "both" } })],
  ["other provider", r => ({ ...r, provider: "elsewhere" })],
  ["not an object", () => ["SUMMARY_RESULT_V1"]],
  ["null body", () => null],
]) {
  test(`malformed success response (${label}) is never rendered`, async t => {
    const h = await harness(t, { summary: async body => json(200, mutate(summaryResult(body.source_url))) });
    await h.generate();
    assert.equal(h.view().result, null);
    assert.equal(h.view().error, "The local service returned an unexpected summary.");
  });
}

test("summary text is rendered as inert text, headings as headings", async t => {
  const h = await harness(t, { summary: async body => json(200, summaryResult(body.source_url, {
    summary_text: "Company overview\n<script>alert(1)</script>\n<img src=x onerror=alert(1)>",
  })) });
  await h.generate();
  const article = h.get("summary-result").children[0];
  const html = article.serialize();
  assert.ok(!html.includes("<script>") && html.includes("&lt;script&gt;"));
  const body = article.children[2];
  assert.deepEqual(body.children.map(c => c.tagName), ["H3", "P", "P"]);
});

test("missing page identity: no summary can be requested", async t => {
  const h = await harness(t, { tabUrl: "https://www.screener.in/screens/" });
  assert.equal(h.get("panel-unsupported").hidden, false);
  assert.equal(h.get("panel-ready").hidden, true);
  await h.generate();
  assert.equal(h.posts.length, 0);
});

test("stale navigation: an older company's late summary never renders on the newer page", async t => {
  const gates = [];
  const h = await harness(t, { summary: () => { const g = deferred(); gates.push(g); return g.promise; } });
  const infy = "https://www.screener.in/company/INFY/";
  const tcs = "https://www.screener.in/company/TCS/consolidated/";
  const pending = h.generate();
  await settle();
  assert.equal(h.view().loading, true);

  await h.navigateTo(tcs);
  assert.match(h.get("current-page-label").textContent, /Identifier: TCS/);
  assert.deepEqual(h.view(), {
    button: "Generate Summary", disabled: false, loading: false, error: null, result: null,
  });
  gates[0].resolve(json(200, summaryResult(infy))); // INFY answers late
  await pending;
  await settle();
  assert.equal(h.view().result, null, "the INFY summary must be discarded");

  const second = h.generate();
  await settle();
  gates[1].resolve(json(200, summaryResult(tcs)));
  await second;
  await settle();
  assert.match(h.view().result, /Tata Consultancy Services Ltd/);
  assert.doesNotMatch(h.view().result, /Infosys/);
  assert.deepEqual(h.posts.map(p => p.body.source_url), [infy, tcs]);
});

test("a stale error for the previous page is also discarded", async t => {
  const gate = deferred();
  const h = await harness(t, { summary: () => gate.promise });
  const pending = h.generate();
  await settle();
  await h.navigateTo("https://www.screener.in/company/GAIL/");
  gate.resolve(json(504, { detail: { code: "PROVIDER_TIMEOUT", message: "x" } }));
  await pending;
  await settle();
  assert.equal(h.view().error, null);
  assert.equal(h.view().loading, false);
});

test("the summary for the current page survives a same-page re-boot", async t => {
  const h = await harness(t);
  await h.generate();
  await h.navigateTo("https://www.screener.in/company/INFY/");
  assert.match(h.view().result, /Infosys Ltd/);
});

test("401 from the local service returns to pairing without rendering", async t => {
  const h = await harness(t, { summary: async () => json(401, { detail: { code: "UNAUTHORIZED", message: "x" } }) });
  await h.generate();
  await settle();
  assert.equal(h.get("panel-unpaired").hidden, false);
  assert.equal(h.view().result, null);
});

test("the extension sends only page identity and its own pairing token - never AI credentials", async t => {
  const h = await harness(t);
  await h.generate();
  const [post] = h.posts;
  assert.deepEqual(Object.keys(post.body).sort(), ["portal", "source_url"]);
  assert.deepEqual(post.headers, { "Content-Type": "application/json", Authorization: `Bearer ${TOKEN}` });
  for (const file of sourceFiles()) {
    const text = readFileSync(file, "utf8");
    assert.doesNotMatch(text, /api\.aicredits|api[_-]?key/i, file);
  }
});

test("no company-specific logic exists anywhere in the extension source", () => {
  for (const file of sourceFiles()) {
    const text = readFileSync(file, "utf8");
    assert.doesNotMatch(text, /Infosys|INFY|\bTCS\b|HDFC|Tata|GAIL|Zydus|Yes Bank|YESBANK/i, file);
  }
});

function sourceFiles(dir = fileURLToPath(new URL("../src/", import.meta.url))) {
  return readdirSync(dir).flatMap(name => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? sourceFiles(path) : path.endsWith(".js") ? [path] : [];
  });
}
