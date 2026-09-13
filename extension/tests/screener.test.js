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
