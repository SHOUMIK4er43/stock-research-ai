/**
 * One browser API client (Part 6B-B). Every fetch() the extension makes to the local
 * backend goes through here - no business semantics, no scattered fetch() calls
 * elsewhere. Every method independently re-validates the service base URL is a bare
 * loopback origin BEFORE sending anything, including the Bearer token - a malicious
 * storage mutation (or a stale/misconfigured value) cannot cause credentials to be
 * sent to an arbitrary destination.
 *
 * The extension never fetches Screener directly - only these exact local routes,
 * matching the committed Part 6B-A contract verbatim (no `/api` prefix):
 *   GET  /health
 *   GET  /v1/version
 *   GET  /v1/capabilities
 *   POST /v1/pair
 *   POST /v1/research-jobs
 *   GET  /v1/research-jobs/{job_id}
 *   GET  /v1/reports/{report_id}
 */

const LOOPBACK_HOSTNAMES = new Set(["127.0.0.1"]);

export class InvalidServiceUrlError extends Error {
  constructor(message) {
    super(message);
    this.name = "InvalidServiceUrlError";
  }
}

export class ApiError extends Error {
  constructor(code, message, status) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

/**
 * Throws InvalidServiceUrlError unless `urlString` is a bare `http://127.0.0.1[:port]`
 * origin - no userinfo, no path, no query, no fragment, and no other scheme or host.
 * @param {string} urlString
 * @returns {URL}
 */
export function validateServiceUrl(urlString) {
  let url;
  try {
    url = new URL(urlString);
  } catch {
    throw new InvalidServiceUrlError("Malformed service URL");
  }
  if (url.protocol !== "http:") {
    throw new InvalidServiceUrlError("Service URL must use http:// (loopback only)");
  }
  if (!LOOPBACK_HOSTNAMES.has(url.hostname)) {
    throw new InvalidServiceUrlError("Service URL must target 127.0.0.1");
  }
  if (url.username || url.password) {
    throw new InvalidServiceUrlError("Service URL must not include credentials");
  }
  if (url.search || url.hash) {
    throw new InvalidServiceUrlError("Service URL must not include a query or fragment");
  }
  if (url.pathname !== "/" && url.pathname !== "") {
    throw new InvalidServiceUrlError("Service URL must be a bare origin");
  }
  return url;
}

function normalizeDetail(detail) {
  if (detail == null) return { code: "HTTP_ERROR", message: "Request failed" };
  if (typeof detail === "string") return { code: "HTTP_ERROR", message: detail };
  if (Array.isArray(detail)) {
    const first = detail[0];
    return { code: "VALIDATION_ERROR", message: (first && first.msg) || "Invalid request" };
  }
  if (typeof detail === "object" && typeof detail.code === "string") {
    return { code: detail.code, message: detail.message || "Request failed" };
  }
  return { code: "HTTP_ERROR", message: "Request failed" };
}

export class ApiClient {
  /**
   * @param {string} baseUrl
   * @param {typeof fetch} [fetchImpl]
   */
  constructor(baseUrl, fetchImpl = fetch) {
    this._baseUrl = baseUrl;
    this._fetch = fetchImpl;
  }

  async _request(path, { method = "GET", token = null, body = null } = {}) {
    // Re-validated on every call, not just at construction - see module docstring.
    const origin = validateServiceUrl(this._baseUrl).origin;
    const headers = {};
    if (body !== null) headers["Content-Type"] = "application/json";
    if (token) headers.Authorization = `Bearer ${token}`;
    let response;
    try {
      response = await this._fetch(origin + path, {
        method,
        headers,
        body: body !== null ? JSON.stringify(body) : undefined,
      });
    } catch (cause) {
      throw new ApiError("NETWORK_ERROR", "The local service is not reachable", 0);
    }
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const { code, message } = normalizeDetail(payload && payload.detail);
      throw new ApiError(code, message, response.status);
    }
    return payload;
  }

  health() {
    return this._request("/health");
  }

  version() {
    return this._request("/v1/version");
  }

  capabilities() {
    return this._request("/v1/capabilities");
  }

  pair(pairingCode) {
    return this._request("/v1/pair", { method: "POST", body: { pairing_code: pairingCode } });
  }

  createResearchJob(token, request) {
    return this._request("/v1/research-jobs", { method: "POST", token, body: request });
  }

  getResearchJob(token, jobId) {
    return this._request(`/v1/research-jobs/${encodeURIComponent(jobId)}`, { token });
  }

  getReport(token, reportId) {
    return this._request(`/v1/reports/${encodeURIComponent(reportId)}`, { token });
  }
}
