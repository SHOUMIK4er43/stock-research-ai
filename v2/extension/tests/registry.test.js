import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import {
  registerPortalAdapter,
  resolvePortal,
  clearRegistry,
  registeredCount,
} from "../src/portals/registry.js";
import { ScreenerAdapter } from "../src/portals/screener.js";

beforeEach(() => {
  clearRegistry();
});

test("resolves a Screener adapter for a supported Screener URL", () => {
  registerPortalAdapter(new ScreenerAdapter());
  const resolved = resolvePortal("https://www.screener.in/company/INFY/");
  assert.ok(resolved);
  assert.equal(resolved.id, "SCREENER");
});

test("returns null for an unsupported page", () => {
  registerPortalAdapter(new ScreenerAdapter());
  assert.equal(resolvePortal("https://example.com/"), null);
});

test("returns null when no adapters are registered", () => {
  assert.equal(resolvePortal("https://www.screener.in/company/INFY/"), null);
  assert.equal(registeredCount(), 0);
});

test("only one portal is registered in V1 (no future portal implemented)", () => {
  registerPortalAdapter(new ScreenerAdapter());
  assert.equal(registeredCount(), 1);
});
