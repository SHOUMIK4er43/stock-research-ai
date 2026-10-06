import { test } from "node:test";
import assert from "node:assert/strict";
import { el, safeLink, clear } from "../src/report/dom.js";
import { createFakeDocument } from "./fixtures/fake_dom.js";

test("el() sets text via textContent, never as markup", () => {
  const doc = createFakeDocument();
  const node = el(doc, "p", { text: "hello world" });
  assert.equal(node.textContent, "hello world");
  assert.equal(node.children.length, 0);
});

test("el() rejects script injection: a <script> string stays inert text, never a real element", () => {
  const doc = createFakeDocument();
  const malicious = "<script>alert(1)</script>";
  const node = el(doc, "p", { text: malicious });
  // The fake DOM's serialize() HTML-escapes text content exactly like a real DOM would -
  // proving the payload is never interpreted as markup.
  assert.equal(node.serialize(), "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>");
  assert.equal(node.textContent, malicious);
  assert.equal(node.children.length, 0, "text must never be parsed into child nodes");
});

test("el() sets className and attributes without touching innerHTML", () => {
  const doc = createFakeDocument();
  const node = el(doc, "div", { className: "badge", attrs: { id: "x", "data-foo": "bar" } });
  assert.equal(node.className, "badge");
  assert.equal(node.getAttribute("id"), "x");
  assert.equal(node.getAttribute("data-foo"), "bar");
});

test("el() skips null/undefined attribute values and falsy children", () => {
  const doc = createFakeDocument();
  const node = el(doc, "div", { attrs: { "data-x": null, "data-y": undefined }, children: [null, undefined, false] });
  assert.equal(node.getAttribute("data-x"), null);
  assert.equal(node.getAttribute("data-y"), null);
  assert.equal(node.children.length, 0);
});

test("el() appends real child nodes in order", () => {
  const doc = createFakeDocument();
  const child1 = el(doc, "span", { text: "a" });
  const child2 = el(doc, "span", { text: "b" });
  const parent = el(doc, "div", { children: [child1, child2] });
  assert.equal(parent.children.length, 2);
  assert.equal(parent.textContent, "ab");
});

test("safeLink() accepts https: and sets target/rel for safety", () => {
  const doc = createFakeDocument();
  const link = safeLink(doc, { href: "https://example.com/report", text: "Source" });
  assert.ok(link);
  assert.equal(link.getAttribute("href"), "https://example.com/report");
  assert.equal(link.getAttribute("target"), "_blank");
  assert.equal(link.getAttribute("rel"), "noopener noreferrer");
});

test("safeLink() accepts http: as well as https:", () => {
  const doc = createFakeDocument();
  const link = safeLink(doc, { href: "http://127.0.0.1:8000/x" });
  assert.ok(link);
});

test("safeLink() refuses javascript: URLs", () => {
  const doc = createFakeDocument();
  assert.equal(safeLink(doc, { href: "javascript:alert(1)" }), null);
});

test("safeLink() refuses data: and file: URLs", () => {
  const doc = createFakeDocument();
  assert.equal(safeLink(doc, { href: "data:text/html,<script>alert(1)</script>" }), null);
  assert.equal(safeLink(doc, { href: "file:///etc/passwd" }), null);
});

test("safeLink() refuses a malformed URL rather than throwing", () => {
  const doc = createFakeDocument();
  assert.equal(safeLink(doc, { href: "not a url" }), null);
});

test("clear() removes all children without using innerHTML", () => {
  const doc = createFakeDocument();
  const parent = el(doc, "div", { children: [el(doc, "span", { text: "a" }), el(doc, "span", { text: "b" })] });
  clear(parent);
  assert.equal(parent.children.length, 0);
});
