import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiClient, ApiError, InvalidServiceUrlError } from "../src/api/client.js";

const BASE_URL = "http://127.0.0.1:8000";

function fakeFetch(handler) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return handler(url, init);
  };
  fn.calls = calls;
  return fn;
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    json: async () => body,
  };
}

test("health() calls exactly GET /health with no auth header", async () => {
  const fetchImpl = fakeFetch((url) => jsonResponse(200, { status: "ok" }));
  const client = new ApiClient(BASE_URL, fetchImpl);
  const result = await client.health();
  assert.equal(result.status, "ok");
  assert.equal(fetchImpl.calls[0].url, "http://127.0.0.1:8000/health");
  assert.equal(fetchImpl.calls[0].init.headers.Authorization, undefined);
});

test("version() calls exactly GET /v1/version", async () => {
  const fetchImpl = fakeFetch(() => jsonResponse(200, { api_version: "v1" }));
  const client = new ApiClient(BASE_URL, fetchImpl);
  await client.version();
  assert.equal(fetchImpl.calls[0].url, "http://127.0.0.1:8000/v1/version");
});

test("capabilities() calls exactly GET /v1/capabilities", async () => {
  const fetchImpl = fakeFetch(() => jsonResponse(200, { analysis_available: false }));
  const client = new ApiClient(BASE_URL, fetchImpl);
  await client.capabilities();
  assert.equal(fetchImpl.calls[0].url, "http://127.0.0.1:8000/v1/capabilities");
});

test("pair() posts exactly {pairing_code} to /v1/pair with no auth header", async () => {
  const fetchImpl = fakeFetch(() => jsonResponse(200, { token: "abc" }));
  const client = new ApiClient(BASE_URL, fetchImpl);
  await client.pair("one-time-code");
  const call = fetchImpl.calls[0];
  assert.equal(call.url, "http://127.0.0.1:8000/v1/pair");
  assert.equal(call.init.method, "POST");
  assert.equal(call.init.headers.Authorization, undefined);
  assert.deepEqual(JSON.parse(call.init.body), { pairing_code: "one-time-code" });
});

test("createResearchJob sends exactly {portal, source_url, research_plan} with Bearer auth", async () => {
  const fetchImpl = fakeFetch(() => jsonResponse(202, { job_id: "job-1", status: "QUEUED" }));
  const client = new ApiClient(BASE_URL, fetchImpl);
  await client.createResearchJob("tok-123", {
    portal: "SCREENER",
    source_url: "https://www.screener.in/company/INFY/",
    research_plan: "FULL_RESEARCH_V1",
  });
  const call = fetchImpl.calls[0];
  assert.equal(call.url, "http://127.0.0.1:8000/v1/research-jobs");
  assert.equal(call.init.headers.Authorization, "Bearer tok-123");
  assert.deepEqual(JSON.parse(call.init.body), {
    portal: "SCREENER",
    source_url: "https://www.screener.in/company/INFY/",
    research_plan: "FULL_RESEARCH_V1",
  });
});

test("getResearchJob uses Bearer auth and never puts the token in the URL", async () => {
  const fetchImpl = fakeFetch(() => jsonResponse(200, { status: "RUNNING" }));
  const client = new ApiClient(BASE_URL, fetchImpl);
  await client.getResearchJob("super-secret-token", "job-42");
  const call = fetchImpl.calls[0];
  assert.equal(call.url, "http://127.0.0.1:8000/v1/research-jobs/job-42");
  assert.ok(!call.url.includes("super-secret-token"));
  assert.equal(call.init.headers.Authorization, "Bearer super-secret-token");
});

test("getReport uses Bearer auth and the token never appears in the URL", async () => {
  const fetchImpl = fakeFetch(() => jsonResponse(200, { id: "report-1" }));
  const client = new ApiClient(BASE_URL, fetchImpl);
  await client.getReport("another-secret", "report-1");
  const call = fetchImpl.calls[0];
  assert.equal(call.url, "http://127.0.0.1:8000/v1/reports/report-1");
  assert.ok(!call.url.includes("another-secret"));
});

test("job/report IDs are URL-encoded (defense against path injection)", async () => {
  const fetchImpl = fakeFetch(() => jsonResponse(200, {}));
  const client = new ApiClient(BASE_URL, fetchImpl);
  await client.getResearchJob("t", "../../etc/passwd");
  assert.equal(
    fetchImpl.calls[0].url,
    "http://127.0.0.1:8000/v1/research-jobs/..%2F..%2Fetc%2Fpasswd",
  );
});

test("a typed {code, message} error body is normalized into ApiError", async () => {
  const fetchImpl = fakeFetch(() =>
    jsonResponse(401, { detail: { code: "UNAUTHORIZED", message: "no" } }),
  );
  const client = new ApiClient(BASE_URL, fetchImpl);
  await assert.rejects(client.getResearchJob("bad", "job-1"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.code, "UNAUTHORIZED");
    assert.equal(error.status, 401);
    return true;
  });
});

test("a plain FastAPI validation-error array body is normalized safely", async () => {
  const fetchImpl = fakeFetch(() =>
    jsonResponse(422, { detail: [{ msg: "field required", loc: ["body", "portal"] }] }),
  );
  const client = new ApiClient(BASE_URL, fetchImpl);
  await assert.rejects(client.pair("x"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.code, "VALIDATION_ERROR");
    return true;
  });
});

test("a network failure (fetch throws) is normalized into ApiError, never left raw", async () => {
  const fetchImpl = async () => {
    throw new TypeError("network down");
  };
  const client = new ApiClient(BASE_URL, fetchImpl);
  await assert.rejects(client.health(), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.code, "NETWORK_ERROR");
    return true;
  });
});

test("an invalid base URL refuses to send anything, including credentials", async () => {
  const fetchImpl = fakeFetch(() => jsonResponse(200, {}));
  const client = new ApiClient("https://evil.example", fetchImpl);
  await assert.rejects(client.createResearchJob("secret-token", {}), InvalidServiceUrlError);
  assert.equal(fetchImpl.calls.length, 0); // fetch was never even attempted
});

test("the client never targets a screener.in URL", async () => {
  const fetchImpl = fakeFetch(() => jsonResponse(200, {}));
  const client = new ApiClient(BASE_URL, fetchImpl);
  await client.health();
  await client.createResearchJob("t", { portal: "SCREENER", source_url: "x" });
  for (const call of fetchImpl.calls) {
    assert.ok(!call.url.includes("screener.in"));
  }
});
