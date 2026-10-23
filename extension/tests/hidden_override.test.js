import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Release smoke test (2026-10-23, real Chrome): `.button-row { display: flex }` beat the
// `hidden` attribute, so the report download row stayed visible (disabled) on pages with no
// report. A class that sets `display` on a hideable element must keep a `[hidden]` override.
test("every hideable element's display class keeps a [hidden] override", () => {
  const css = readFileSync(new URL("../css/sidepanel.css", import.meta.url), "utf8");
  const html = readFileSync(new URL("../sidepanel.html", import.meta.url), "utf8");
  const hideable = [...html.matchAll(/<[a-z]+\b[^>]*\bhidden\b[^>]*>/g)].map(m => m[0]);
  const used = new Set(hideable.flatMap(tag => (tag.match(/class="([^"]+)"/) || ["", ""])[1]
    .split(/\s+/).filter(Boolean)));
  const displayClasses = [...css.matchAll(/^\.([\w-]+)\s*\{[^}]*\bdisplay\s*:\s*(?!none)/gm)]
    .map(m => m[1]).filter(name => used.has(name));
  assert.ok(displayClasses.includes("button-row"));
  for (const name of displayClasses) {
    assert.match(css, new RegExp(`\\.${name}\\[hidden\\]\\s*\\{\\s*display:\\s*none`), name);
  }
});
