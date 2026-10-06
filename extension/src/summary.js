/**
 * SUMMARY_V1 (Box 4): pure helpers for the one-summary flow. No Chrome APIs, no network.
 *
 * The backend owns extraction, credentials and the model call; this module only
 * validates the returned SummaryResult shape and renders it with the safe `el()` helper
 * (textContent only - model text can never become markup). Error text shown to the user
 * comes from the fixed map below, never from a server or exception message.
 */
import { el } from "./report/dom.js";

export const SummaryStatus = Object.freeze({
  IDLE: "IDLE",
  LOADING: "LOADING",
  SUCCESS: "SUCCESS",
  ERROR: "ERROR",
});

const SCOPE_LABELS = Object.freeze({
  standalone: "Standalone",
  consolidated: "Consolidated",
  unknown: "Scope not stated",
});

/** The fixed SUMMARY_PROMPT_V1 section headings; any other line is body text. */
const SECTION_HEADINGS = new Set([
  "Company overview",
  "Financial snapshot",
  "Growth and profitability",
  "Balance sheet and cash flow",
  "Valuation context",
  "Positives in the data",
  "Risks and gaps in the data",
  "Overall summary",
]);

const UNUSABLE_AI_RESPONSE = "The AI response could not be used. You can try again.";
const MAY_HAVE_RUN =
  "The request may still have completed on the provider; try again only if needed.";

export const SUMMARY_ERROR_LABELS = Object.freeze({
  UNSUPPORTED_SOURCE_URL: "This is not a supported Screener company page.",
  COMPANY_DATA_UNAVAILABLE: "Company data could not be read from Screener. Try again shortly.",
  PROVIDER_NOT_CONFIGURED: "The AI summary provider is not configured on your local service.",
  CREDENTIAL_UNAVAILABLE: "The AI summary credential is not available on your local service.",
  PROVIDER_UNAVAILABLE: "Could not reach the AI provider. Try again shortly.",
  PROVIDER_HTTP_ERROR: "The AI provider rejected the request (for example, insufficient credits).",
  PROVIDER_TIMEOUT: `The AI provider did not respond in time. ${MAY_HAVE_RUN}`,
  PROVIDER_CONNECTION_LOST: `The connection to the AI provider was lost. ${MAY_HAVE_RUN}`,
  INVALID_PROVIDER_RESPONSE: UNUSABLE_AI_RESPONSE,
  SUMMARY_TRUNCATED: UNUSABLE_AI_RESPONSE,
  SUMMARY_EMPTY: UNUSABLE_AI_RESPONSE,
  SUMMARY_TOO_LARGE: UNUSABLE_AI_RESPONSE,
  UNSAFE_CONTENT: UNUSABLE_AI_RESPONSE,
  MODEL_MISMATCH: UNUSABLE_AI_RESPONSE,
  UNSAFE_REQUEST: "The summary request failed a safety check.",
  SUMMARY_INPUT_TOO_LARGE: "This company's data is too large to summarise.",
  SUMMARY_REQUEST_TOO_LARGE: "This company's data is too large to summarise.",
  NETWORK_ERROR: "The local service is not reachable.",
  MALFORMED_RESPONSE: "The local service returned an unexpected summary.",
});

/** @param {string | null | undefined} code @returns {string} */
export function summaryErrorLabel(code) {
  return SUMMARY_ERROR_LABELS[code] ?? "Summary failed for an unknown reason.";
}

function boundedString(value, max) {
  return typeof value === "string" && value.trim().length > 0 && value.length <= max;
}

/**
 * Validates the SUMMARY_RESULT_V1 fields the panel displays. Anything else -> null.
 * @returns {{companyName: string, scopeLabel: string, summaryText: string, generatedAt: string, model: string} | null}
 */
export function parseSummaryResult(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  if (payload.schema_version !== "SUMMARY_RESULT_V1" || payload.provider !== "aicredits") return null;
  const company = payload.company;
  if (!company || typeof company !== "object") return null;
  if (!boundedString(company.name, 120) || !boundedString(company.source_company_key, 120)) return null;
  if (!Object.hasOwn(SCOPE_LABELS, company.reporting_scope)) return null;
  if (!boundedString(payload.summary_text, 8000)) return null;
  if (!boundedString(payload.requested_model, 120)) return null;
  if (!boundedString(payload.generated_at, 64) || Number.isNaN(Date.parse(payload.generated_at))) {
    return null;
  }
  return {
    companyName: company.name,
    scopeLabel: SCOPE_LABELS[company.reporting_scope],
    summaryText: payload.summary_text,
    generatedAt: payload.generated_at,
    model: payload.requested_model,
  };
}

/** Builds the summary DOM: company name, safe metadata line, then the text by line. */
export function renderSummaryResult(doc, view) {
  const generated = new Date(view.generatedAt);
  const meta = `${view.scopeLabel} · Generated ${generated.toLocaleString()} · ${view.model}`;
  const body = el(doc, "div", { className: "summary-text" });
  for (const line of view.summaryText.split("\n")) {
    const text = line.trim();
    if (!text) continue;
    body.appendChild(el(doc, SECTION_HEADINGS.has(text) ? "h3" : "p", { text }));
  }
  return el(doc, "article", {
    className: "summary-report",
    children: [
      el(doc, "h2", { className: "summary-company", text: view.companyName }),
      el(doc, "p", { className: "muted summary-meta", text: meta }),
      body,
    ],
  });
}
