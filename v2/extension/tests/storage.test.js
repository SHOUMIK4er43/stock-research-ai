import { test } from "node:test";
import assert from "node:assert/strict";
import { createStorage, createMemoryBackend, DEFAULT_SERVICE_BASE_URL } from "../src/storage.js";

test("service base URL defaults to the loopback default when unset", async () => {
  const storage = createStorage(createMemoryBackend());
  assert.equal(await storage.getServiceBaseUrl(), DEFAULT_SERVICE_BASE_URL);
});

test("service base URL can be overridden and read back", async () => {
  const storage = createStorage(createMemoryBackend());
  await storage.setServiceBaseUrl("http://127.0.0.1:9000");
  assert.equal(await storage.getServiceBaseUrl(), "http://127.0.0.1:9000");
});

test("token is absent until set, then present, then cleared", async () => {
  const storage = createStorage(createMemoryBackend());
  assert.equal(await storage.getToken(), null);
  await storage.setToken("secret-token-value");
  assert.equal(await storage.getToken(), "secret-token-value");
  await storage.clearToken();
  assert.equal(await storage.getToken(), null);
});

test("active job pointer round-trips and clears", async () => {
  const storage = createStorage(createMemoryBackend());
  assert.equal(await storage.getActiveJob(), null);
  const job = { jobId: "job-1", portal: "SCREENER", sourceUrl: "https://www.screener.in/company/INFY/" };
  await storage.setActiveJob(job);
  assert.deepEqual(await storage.getActiveJob(), job);
  await storage.clearActiveJob();
  assert.equal(await storage.getActiveJob(), null);
});

test("last-report pointer survives independently of the active job pointer", async () => {
  const storage = createStorage(createMemoryBackend());
  const report = { reportId: "report-1", sourceUrl: "https://www.screener.in/company/INFY/" };
  await storage.setLastReport(report);
  await storage.setActiveJob({ jobId: "job-2", portal: "SCREENER", sourceUrl: "x" });
  await storage.clearActiveJob();
  assert.deepEqual(await storage.getLastReport(), report);
});

test("only chrome.storage.local-shaped keys are ever written (no full report/raw evidence)", async () => {
  const backend = createMemoryBackend();
  const storage = createStorage(backend);
  await storage.setToken("t");
  await storage.setActiveJob({ jobId: "1", portal: "SCREENER", sourceUrl: "x" });
  await storage.setLastReport({ reportId: "r", sourceUrl: "x" });
  await storage.setServiceBaseUrl(DEFAULT_SERVICE_BASE_URL);
  const dump = backend._dump();
  assert.deepEqual(
    Object.keys(dump).sort(),
    ["activeJob", "lastReport", "pairingToken", "serviceBaseUrl"].sort(),
  );
});
