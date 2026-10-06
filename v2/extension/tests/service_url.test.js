import { test } from "node:test";
import assert from "node:assert/strict";
import { validateServiceUrl, InvalidServiceUrlError } from "../src/api/client.js";

test("accepts a bare loopback origin with a valid port", () => {
  const url = validateServiceUrl("http://127.0.0.1:8000");
  assert.equal(url.hostname, "127.0.0.1");
  assert.equal(url.port, "8000");
});

test("accepts loopback with default port (no explicit port)", () => {
  assert.doesNotThrow(() => validateServiceUrl("http://127.0.0.1"));
});

test("rejects a non-loopback host", () => {
  assert.throws(() => validateServiceUrl("http://192.168.1.5:8000"), InvalidServiceUrlError);
});

test("rejects an external https host", () => {
  assert.throws(() => validateServiceUrl("https://evil.example"), InvalidServiceUrlError);
});

test("rejects a file URL", () => {
  assert.throws(() => validateServiceUrl("file:///etc/passwd"), InvalidServiceUrlError);
});

test("rejects a javascript: URL", () => {
  assert.throws(() => validateServiceUrl("javascript:alert(1)"), InvalidServiceUrlError);
});

test("rejects https even against 127.0.0.1 (contract is http-loopback only)", () => {
  assert.throws(() => validateServiceUrl("https://127.0.0.1:8000"), InvalidServiceUrlError);
});

test("rejects localhost (not part of the backend contract)", () => {
  assert.throws(() => validateServiceUrl("http://localhost:8000"), InvalidServiceUrlError);
});

test("rejects a malformed port via a non-numeric authority", () => {
  assert.throws(() => validateServiceUrl("http://127.0.0.1:notaport"), InvalidServiceUrlError);
});

test("rejects a path, query, or fragment on the service URL", () => {
  assert.throws(() => validateServiceUrl("http://127.0.0.1:8000/v1/pair"));
  assert.throws(() => validateServiceUrl("http://127.0.0.1:8000/?x=1"));
  assert.throws(() => validateServiceUrl("http://127.0.0.1:8000/#frag"));
});

test("rejects embedded credentials", () => {
  assert.throws(() => validateServiceUrl("http://user:pass@127.0.0.1:8000"));
});

test("rejects a completely malformed string", () => {
  assert.throws(() => validateServiceUrl("not a url"), InvalidServiceUrlError);
});
