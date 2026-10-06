import { test } from "node:test";
import assert from "node:assert/strict";
import { describeSection, knownSectionIds, GROUPS, GROUP_LABELS } from "../src/report/section_registry.js";

test("describeSection resolves every known canonical section id", () => {
  for (const id of knownSectionIds()) {
    const described = describeSection(id);
    assert.ok(GROUPS.includes(described.group), `${id} maps to a known group`);
    assert.equal(typeof described.label, "string");
    assert.ok(described.label.length > 0);
    assert.equal(typeof described.order, "number");
  }
});

test("describeSection never crashes on an unknown future section id and falls back safely", () => {
  const described = describeSection("SOME_FUTURE_SECTION_NOT_YET_KNOWN");
  assert.equal(described.group, "OTHER");
  assert.equal(described.label, "SOME_FUTURE_SECTION_NOT_YET_KNOWN");
  assert.equal(described.order, Number.MAX_SAFE_INTEGER);
});

test("registry covers exactly the 42 canonical ReportSectionId values with no duplicates", () => {
  const ids = knownSectionIds();
  assert.equal(ids.length, 42);
  assert.equal(new Set(ids).size, 42);
});

test("every group in GROUPS has a human label", () => {
  for (const group of GROUPS) {
    assert.equal(typeof GROUP_LABELS[group], "string");
  }
});
