import { test } from "node:test";
import assert from "node:assert/strict";
import { ScreenerAdapter } from "../src/portals/screener.js";

const adapter = new ScreenerAdapter();

test("supports an alphabetic (NSE-style) company URL", () => {
  assert.equal(adapter.isSupportedUrl("https://www.screener.in/company/INFY/"), true);
});

test("supports a numeric (BSE-style) company URL", () => {
  assert.equal(adapter.isSupportedUrl("https://www.screener.in/company/500209/"), true);
});

test("supports the consolidated variant", () => {
  assert.equal(
    adapter.isSupportedUrl("https://www.screener.in/company/INFY/consolidated/"),
    true,
  );
});

test("rejects the Screener home page", () => {
  assert.equal(adapter.isSupportedUrl("https://www.screener.in/"), false);
});

test("rejects a screens page", () => {
  assert.equal(adapter.isSupportedUrl("https://www.screener.in/screens/"), false);
});

test("rejects the login page", () => {
  assert.equal(adapter.isSupportedUrl("https://www.screener.in/login/"), false);
});

test("rejects search", () => {
  assert.equal(adapter.isSupportedUrl("https://www.screener.in/search/?q=infy"), false);
});

test("rejects a watchlist page", () => {
  assert.equal(adapter.isSupportedUrl("https://www.screener.in/watchlist/"), false);
});

test("rejects a lookalike domain", () => {
  assert.equal(adapter.isSupportedUrl("https://www.screener.in.evil.example/company/INFY/"), false);
});

test("rejects an unrelated URL", () => {
  assert.equal(adapter.isSupportedUrl("https://example.com/company/INFY/"), false);
});

test("rejects a malformed URL", () => {
  assert.equal(adapter.isSupportedUrl("not-a-url"), false);
});

test("rejects http (non-https) scheme", () => {
  assert.equal(adapter.isSupportedUrl("http://www.screener.in/company/INFY/"), false);
});

test("rejects a query string", () => {
  assert.equal(adapter.isSupportedUrl("https://www.screener.in/company/INFY/?x=1"), false);
});

test("rejects credentials in the URL", () => {
  assert.equal(
    adapter.isSupportedUrl("https://user:pass@www.screener.in/company/INFY/"),
    false,
  );
});

test("rejects a non-standard port", () => {
  assert.equal(adapter.isSupportedUrl("https://www.screener.in:8443/company/INFY/"), false);
});

test("getSourceContext returns only portal + source_url for a supported page", () => {
  const url = "https://www.screener.in/company/INFY/";
  assert.deepEqual(adapter.getSourceContext(url), { portal: "SCREENER", source_url: url });
});

test("getSourceContext returns null for an unsupported page", () => {
  assert.equal(adapter.getSourceContext("https://www.screener.in/screens/"), null);
});

test("adapter carries no financial/sector/report semantics on its instance", () => {
  const context = adapter.getSourceContext("https://www.screener.in/company/INFY/");
  assert.deepEqual(Object.keys(context).sort(), ["portal", "source_url"]);
});

// --- getPageIdentity (SUMMARY_V1 Box 1) ---------------------------------------------

for (const [label, identifier, suffix, expectedMode] of [
  ["INFY standalone", "INFY", "", "standalone"],
  ["INFY consolidated", "INFY", "consolidated/", "consolidated"],
  ["TCS standalone", "TCS", "", "standalone"],
  ["HDFCBANK standalone", "HDFCBANK", "", "standalone"],
  ["TATAPOWER standalone", "TATAPOWER", "", "standalone"],
  ["GAIL standalone", "GAIL", "", "standalone"],
  ["ZYDUSLIFE standalone", "ZYDUSLIFE", "", "standalone"],
  ["numeric identifier standalone", "500209", "", "standalone"],
  ["identifier containing a hyphen", "ABC-DEF", "", "standalone"],
  ["identifier containing an underscore", "ABC_DEF", "", "standalone"],
]) {
  test(`getPageIdentity: ${label}`, () => {
    const url = `https://www.screener.in/company/${identifier}/${suffix}`;
    assert.deepEqual(adapter.getPageIdentity(url), {
      portal: "SCREENER",
      source_url: url,
      page_identifier: identifier,
      page_mode: expectedMode,
    });
  });
}

test("getPageIdentity returns null for an unsupported Screener path", () => {
  assert.equal(adapter.getPageIdentity("https://www.screener.in/screens/"), null);
});

test("getPageIdentity returns null for a lookalike domain", () => {
  assert.equal(
    adapter.getPageIdentity("https://www.screener.in.evil.example/company/INFY/"),
    null,
  );
});

test("getPageIdentity returns null for a malformed company URL", () => {
  assert.equal(adapter.getPageIdentity("https://www.screener.in/company//"), null);
});

test("getPageIdentity returns null for a completely malformed URL", () => {
  assert.equal(adapter.getPageIdentity("not-a-url"), null);
});

test("a numeric page_identifier is never labeled as an NSE ticker or exchange symbol", () => {
  const identity = adapter.getPageIdentity("https://www.screener.in/company/500209/");
  const serialized = JSON.stringify(identity);
  assert.doesNotMatch(serialized, /ticker/i);
  assert.doesNotMatch(serialized, /exchange/i);
  assert.doesNotMatch(serialized, /symbol/i);
  assert.equal(identity.page_identifier, "500209");
});

test("getPageIdentity never infers or includes a company name", () => {
  const identity = adapter.getPageIdentity("https://www.screener.in/company/INFY/");
  assert.deepEqual(Object.keys(identity).sort(), [
    "page_identifier",
    "page_mode",
    "portal",
    "source_url",
  ]);
  assert.equal("company_name" in identity, false);
  assert.equal("name" in identity, false);
});

test("getPageIdentity never performs a network fetch", () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = () => {
    throw new Error("getPageIdentity must never fetch");
  };
  try {
    adapter.getPageIdentity("https://www.screener.in/company/INFY/");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("getSourceContext behavior is unchanged by the addition of getPageIdentity", () => {
  const url = "https://www.screener.in/company/TCS/consolidated/";
  assert.deepEqual(adapter.getSourceContext(url), { portal: "SCREENER", source_url: url });
  assert.equal(adapter.getSourceContext("https://www.screener.in/screens/"), null);
});
