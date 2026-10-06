import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiClient, ApiError, InvalidServiceUrlError, ARTIFACT_KINDS } from "../src/api/client.js";

import { createStorage, createMemoryBackend } from "../src/storage.js";

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
  const result = await client.pair("one-time-code");
  assert.deepEqual(result, { token: "abc" });
  const call = fetchImpl.calls[0];
  assert.equal(call.url, "http://127.0.0.1:8000/v1/pair");
  assert.equal(call.init.method, "POST");
  assert.equal(call.init.headers.Authorization, undefined);
  assert.deepEqual(JSON.parse(call.init.body), { pairing_code: "one-time-code" });
  assert.equal(Object.hasOwn(JSON.parse(call.init.body), "code"), false);
  assert.deepEqual(call.init.headers, { "Content-Type": "application/json" });
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

function textResponse(status, { text = "", contentType = "text/markdown; charset=utf-8" } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    text: async () => text,
    json: async () => {
      throw new Error("not json");
    },
    headers: { get: (name) => (name.toLowerCase() === "content-type" ? contentType : null) },
  };
}

test("ARTIFACT_KINDS is exactly the closed 3-value backend enum", () => {
  assert.deepEqual([...ARTIFACT_KINDS].sort(), ["INSTITUTIONAL_REPORT", "INVESTOR_BRIEF", "RESEARCH_JSON"].sort());
});

test("getArtifact calls exactly GET /v1/reports/{id}/artifacts/{kind} with Bearer auth", async () => {
  const fetchImpl = fakeFetch(() => textResponse(200, { text: "# Report\n" }));
  const client = new ApiClient(BASE_URL, fetchImpl);
  const result = await client.getArtifact("secret-tok", "report-1", "INSTITUTIONAL_REPORT");
  const call = fetchImpl.calls[0];
  assert.equal(call.url, "http://127.0.0.1:8000/v1/reports/report-1/artifacts/INSTITUTIONAL_REPORT");
  assert.equal(call.init.headers.Authorization, "Bearer secret-tok");
  assert.ok(!call.url.includes("secret-tok"));
  assert.equal(result.text, "# Report\n");
  assert.equal(result.contentType, "text/markdown; charset=utf-8");
});

test("getArtifact returns raw text, never attempting JSON parsing of a markdown body", async () => {
  const fetchImpl = fakeFetch(() => textResponse(200, { text: "not json at all: {{{", contentType: "text/markdown; charset=utf-8" }));
  const client = new ApiClient(BASE_URL, fetchImpl);
  const result = await client.getArtifact("t", "report-1", "RESEARCH_JSON");
  assert.equal(result.text, "not json at all: {{{");
});

test("getArtifact refuses an artifact kind outside the closed enum without ever calling fetch", async () => {
  const fetchImpl = fakeFetch(() => textResponse(200, {}));
  const client = new ApiClient(BASE_URL, fetchImpl);
  await assert.rejects(client.getArtifact("t", "report-1", "../../etc/passwd"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.code, "INVALID_ARTIFACT_KIND");
    return true;
  });
  assert.equal(fetchImpl.calls.length, 0);
});

test("getArtifact still URL-encodes the report id", async () => {
  const fetchImpl = fakeFetch(() => textResponse(200, { text: "x" }));
  const client = new ApiClient(BASE_URL, fetchImpl);
  await client.getArtifact("t", "../../etc/passwd", "RESEARCH_JSON");
  assert.equal(
    fetchImpl.calls[0].url,
    "http://127.0.0.1:8000/v1/reports/..%2F..%2Fetc%2Fpasswd/artifacts/RESEARCH_JSON",
  );
});

test("getArtifact normalizes a 404 into ApiError", async () => {
  const fetchImpl = fakeFetch(() =>
    textResponse(404, { text: "" }),
  );
  fetchImpl.calls; // no-op reference for lint clarity
  const client = new ApiClient(BASE_URL, async (...args) => {
    const res = await fetchImpl(...args);
    res.json = async () => ({ detail: "Unknown report_id" });
    return res;
  });
  await assert.rejects(client.getArtifact("t", "missing", "RESEARCH_JSON"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 404);
    return true;
  });
});

test("getArtifact refuses to target a non-loopback base URL", async () => {
  const fetchImpl = fakeFetch(() => textResponse(200, { text: "x" }));
  const client = new ApiClient("https://evil.example", fetchImpl);
  await assert.rejects(client.getArtifact("t", "report-1", "RESEARCH_JSON"), InvalidServiceUrlError);
  assert.equal(fetchImpl.calls.length, 0);
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


test("pairing response token round-trips through existing storage into research Bearer auth", async () => {
  const backend = createMemoryBackend();
  const storage = createStorage(backend);
  const fetchImpl = fakeFetch((url) =>
    url.endsWith("/v1/pair")
      ? jsonResponse(200, { token: "test-browser-token" })
      : jsonResponse(202, { job_id: "test-job" }),
  );
  const client = new ApiClient(BASE_URL, fetchImpl);
  const result = await client.pair("test-pairing-code");
  await storage.setToken(result.token);
  assert.deepEqual(backend._dump(), { pairingToken: "test-browser-token" });
  await client.createResearchJob(await storage.getToken(), {
    portal: "SCREENER",
    source_url: "https://www.screener.in/company/INFY/",
    research_plan: "FULL_RESEARCH_V1",
  });
  assert.equal(fetchImpl.calls.length, 2);
  assert.deepEqual(JSON.parse(fetchImpl.calls[0].init.body), { pairing_code: "test-pairing-code" });
  assert.deepEqual(fetchImpl.calls[1].init.headers, {
    "Content-Type": "application/json",
    Authorization: "Bearer test-browser-token",
  });
  for (const call of fetchImpl.calls) {
    assert.ok(call.url.startsWith(BASE_URL + "/v1/"));
    assert.ok(!call.url.includes("test-browser-token"));
    assert.ok(!call.init.body.includes("test-browser-token"));
    assert.ok(!Object.keys(JSON.parse(call.init.body)).some((key) => /api.?key/i.test(key)));
  }
});

for (const message of ["Invalid pairing code", "Pairing code has expired"]) {
  test(`pairing rejection remains an error with no fallback: ${message}`, async () => {
    const fetchImpl = fakeFetch(() => jsonResponse(401, {
      detail: { code: "PAIRING_FAILED", message },
    }));
    const client = new ApiClient(BASE_URL, fetchImpl);
    await assert.rejects(client.pair("test-invalid-code"), (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 401);
      assert.equal(error.code, "PAIRING_FAILED");
      return true;
    });
    assert.equal(fetchImpl.calls.length, 1);
    assert.deepEqual(JSON.parse(fetchImpl.calls[0].init.body), {
      pairing_code: "test-invalid-code",
    });
  });
}

for (const status of [401, 403, 409, 422]) {
  test(`HTTP ${status} remains an application error, not network unreachable`, async () => {
    const client = new ApiClient(BASE_URL, fakeFetch(() => jsonResponse(status, {
      detail: { code: `TEST_${status}`, message: "Request rejected" },
    })));
    await assert.rejects(client.pair("synthetic-invalid-code"), error => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, status);
      assert.equal(error.code, `TEST_${status}`);
      assert.equal(error.message, "Request rejected");
      return true;
    });
  });
}

for (const operation of ["health", "version", "getArtifact"]) {
  test(`${operation} invokes injected fetch detached from the ApiClient receiver`, async () => {
    let calls = 0;
    let receiver;
    async function browserLikeFetch(url, init) {
      calls += 1;
      receiver = this;
      if (this !== undefined) throw new TypeError("Illegal invocation");
      assert.equal(init.method, "GET");
      if (operation === "getArtifact") {
        assert.equal(url, `${BASE_URL}/v1/reports/report-1/artifacts/INSTITUTIONAL_REPORT`);
        assert.equal(init.headers.Authorization, "Bearer synthetic-token");
        return { ok: true, status: 200, text: async () => "report text",
          headers: { get: () => "text/markdown" } };
      }
      assert.equal(url, BASE_URL + (operation === "health" ? "/health" : "/v1/version"));
      assert.deepEqual(init.headers, {});
      return jsonResponse(200, operation === "health" ? { status: "ok" } : { api_version: "v1" });
    }
    const client = new ApiClient(BASE_URL, browserLikeFetch);
    const result = operation === "getArtifact"
      ? await client.getArtifact("synthetic-token", "report-1", "INSTITUTIONAL_REPORT")
      : await client[operation]();
    assert.equal(calls, 1);
    assert.equal(receiver, undefined);
    assert.deepEqual(result, operation === "getArtifact"
      ? { text: "report text", contentType: "text/markdown" }
      : operation === "health" ? { status: "ok" } : { api_version: "v1" });
  });
}
