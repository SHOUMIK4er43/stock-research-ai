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
 *   GET  /v1/reports/{report_id}/artifacts/{artifact_kind}
 *   POST /v1/summaries
 *   POST /v1/final-reports                                    (Detailed Research: generate)
 *   GET  /v1/final-report-jobs/{job_id}
 *   GET  /v1/final-reports?page={Screener page key}           (the acquired page's reports)
 *   GET  /v1/final-reports/{artifact_id}/view
 *   GET  /v1/final-reports/{artifact_id}/files/{format}
 */

/** Closed ArtifactKind enum (Part 6B-A) - the client only ever sends one of these three
 * exact values, never a path or filename, so there is no way to request an arbitrary
 * file from the local service. */
export const ARTIFACT_KINDS = Object.freeze(["INSTITUTIONAL_REPORT", "INVESTOR_BRIEF", "RESEARCH_JSON"]);

/** Closed Box 15 final-report format enum - never a path or a filename. */
export const FINAL_REPORT_FORMATS = Object.freeze(["PDF", "HTML", "MARKDOWN", "JSON"]);
const ARTIFACT_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

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
      // Native browser fetch must not receive the ApiClient as its receiver.
      const fetchImpl = this._fetch;
      response = await fetchImpl(origin + path, {
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

  /** SUMMARY_V1: one synchronous summary for the detected page ({portal, source_url}). */
  createSummary(token, request) {
    return this._request("/v1/summaries", { method: "POST", token, body: request });
  }

  /**
   * Detailed Research reports whose ACQUIRED primary Screener page is this page. The key is
   * the opaque Screener path segment - never sent or treated as an exchange symbol.
   */
  listFinalReportsForPage(token, pageKey) {
    if (typeof pageKey !== "string" || !/^[A-Z0-9_-]{1,40}$/.test(pageKey)) {
      return Promise.reject(new ApiError("INVALID_FINAL_REPORT_PAGE", "Unknown company page", 0));
    }
    return this._request(`/v1/final-reports?page=${encodeURIComponent(pageKey)}`, { token });
  }

  /** Start (or join) Detailed Research generation for this Screener page. */
  createFinalReport(token, sourceUrl) {
    return this._request("/v1/final-reports", { method: "POST", token, body: { source_url: sourceUrl } });
  }

  getFinalReportJob(token, jobId) {
    if (typeof jobId !== "string" || !ARTIFACT_ID_PATTERN.test(jobId)) {
      return Promise.reject(new ApiError("INVALID_FINAL_REPORT_JOB", "Unknown job", 0));
    }
    return this._request(`/v1/final-report-jobs/${jobId}`, { token });
  }

  getFinalReportView(token, artifactId) {
    if (typeof artifactId !== "string" || !ARTIFACT_ID_PATTERN.test(artifactId)) {
      return Promise.reject(new ApiError("INVALID_FINAL_REPORT_ID", "Unknown report", 0));
    }
    return this._request(`/v1/final-reports/${artifactId}/view`, { token });
  }

  /**
   * One stored dossier file as a Blob (PDF is binary). Closed format enum and a UUID-shaped
   * artifact id only; same loopback re-validation and Bearer handling as every call.
   * @returns {Promise<{blob: Blob, contentType: string}>}
   */
  async getFinalReportFile(token, artifactId, format) {
    if (!FINAL_REPORT_FORMATS.includes(format)) {
      throw new ApiError("INVALID_FINAL_REPORT_FORMAT", "Unknown report format", 0);
    }
    if (typeof artifactId !== "string" || !ARTIFACT_ID_PATTERN.test(artifactId)) {
      throw new ApiError("INVALID_FINAL_REPORT_ID", "Unknown report", 0);
    }
    const origin = validateServiceUrl(this._baseUrl).origin;
    const path = `/v1/final-reports/${artifactId}/files/${format}`;
    let response;
    try {
      const fetchImpl = this._fetch;
      response = await fetchImpl(origin + path, {
        method: "GET",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
    } catch {
      throw new ApiError("NETWORK_ERROR", "The local service is not reachable", 0);
    }
    if (!response.ok) {
      let payload = null;
      try {
        payload = await response.json();
      } catch {
        payload = null;
      }
      const { code, message } = normalizeDetail(payload && payload.detail);
      throw new ApiError(code, message, response.status);
    }
    const blob = await response.blob();
    const contentType = (response.headers && response.headers.get && response.headers.get("content-type")) || "";
    return { blob, contentType };
  }

  getReport(token, reportId) {
    return this._request(`/v1/reports/${encodeURIComponent(reportId)}`, { token });
  }

  /**
   * Artifacts (markdown or JSON-as-text) are never JSON-decoded response bodies, so this
   * bypasses `_request`'s JSON parsing and returns the raw text plus the content-type the
   * server actually sent - but still re-validates the loopback origin and attaches the
   * Bearer token exactly like every other authenticated call.
   * @param {string} token
   * @param {string} reportId
   * @param {"INSTITUTIONAL_REPORT"|"INVESTOR_BRIEF"|"RESEARCH_JSON"} artifactKind
   * @returns {Promise<{text: string, contentType: string}>}
   */
  async getArtifact(token, reportId, artifactKind) {
    if (!ARTIFACT_KINDS.includes(artifactKind)) {
      throw new ApiError("INVALID_ARTIFACT_KIND", "Unknown artifact kind", 0);
    }
    const origin = validateServiceUrl(this._baseUrl).origin;
    const path = `/v1/reports/${encodeURIComponent(reportId)}/artifacts/${encodeURIComponent(artifactKind)}`;
    let response;
    try {
      // Native browser fetch must not receive the ApiClient as its receiver.
      const fetchImpl = this._fetch;
      response = await fetchImpl(origin + path, {
        method: "GET",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
    } catch {
      throw new ApiError("NETWORK_ERROR", "The local service is not reachable", 0);
    }
    if (!response.ok) {
      let payload = null;
      try {
        payload = await response.json();
      } catch {
        payload = null;
      }
      const { code, message } = normalizeDetail(payload && payload.detail);
      throw new ApiError(code, message, response.status);
    }
    const text = await response.text();
    const contentType = (response.headers && response.headers.get && response.headers.get("content-type")) || "";
    return { text, contentType };
  }
}
