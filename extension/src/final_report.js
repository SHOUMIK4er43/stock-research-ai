/**
 * Box 15 Detailed Research dossier helpers (pure). The backend owns every fact: this module
 * only validates the list response shape and builds labels / download names from it. No
 * research, numbers or identity are derived here.
 */
import { FINAL_REPORT_FORMATS } from "./api/client.js";

export const FINAL_REPORT_LABELS = Object.freeze({
  PDF: "PDF", HTML: "HTML", MARKDOWN: "Markdown", JSON: "JSON",
});
const EXTENSIONS = Object.freeze({ PDF: "pdf", HTML: "html", MARKDOWN: "md", JSON: "json" });
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;

/** The newest well-formed dossier in a /v1/final-reports response, or null. */
export function latestFinalReport(payload) {
  const reports = payload && Array.isArray(payload.reports) ? payload.reports : [];
  for (const r of reports) {
    if (!r || typeof r !== "object") continue;
    if (typeof r.artifact_id !== "string" || !UUID.test(r.artifact_id)) continue;
    if (typeof r.as_of !== "string" || !DAY.test(r.as_of)) continue;
    const formats = Array.isArray(r.formats) ? r.formats.filter(f => FINAL_REPORT_FORMATS.includes(f)) : [];
    const failed = Array.isArray(r.failed_formats)
      ? r.failed_formats.filter(f => FINAL_REPORT_FORMATS.includes(f)) : [];
    return {
      artifactId: r.artifact_id,
      asOf: r.as_of,
      companyName: typeof r.company_name === "string" ? r.company_name : "",
      symbol: typeof r.nse_symbol === "string" ? r.nse_symbol : "",
      status: typeof r.status === "string" ? r.status : "",
      formats,
      failed,
    };
  }
  return null;
}

/** One-line status for the panel (text only; rendered with textContent). */
export function finalReportLabel(report) {
  if (!report) {
    return "No Detailed Research dossier has been generated locally for this company yet " +
      "(run: stock-research-ai detailed-research --stage <stage.json>).";
  }
  const partial = report.status === "PARTIAL_RENDER" && report.failed.length
    ? ` Partial: ${report.failed.map(f => FINAL_REPORT_LABELS[f]).join(", ")} could not be produced.`
    : "";
  return `Detailed Research dossier — ${report.companyName} — as of ${report.asOf}.${partial}`;
}

export function finalReportFilename(report, format) {
  const who = String(report.symbol || "company").replace(/[^A-Za-z0-9_-]+/g, "_").slice(0, 20) || "company";
  return `${who}_${report.asOf}_detailed_research.${EXTENSIONS[format]}`;
}
