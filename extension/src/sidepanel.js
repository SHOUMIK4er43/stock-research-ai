/**
 * Side panel controller (Part 6B-B). Wires the pure modules (state, portals, storage,
 * API client, job flow) to the DOM. Contains no research/financial/valuation/sector
 * semantics of its own - it only reflects backend-reported state.
 */
import { ApiClient, ApiError } from "./api/client.js";
import { registerPortalAdapter, resolvePortal } from "./portals/registry.js";
import { ScreenerAdapter } from "./portals/screener.js";
import { createStorage } from "./storage.js";
import { States, deriveState, stageLabel, failureLabel } from "./state.js";
import { storageActionsForJobUpdate, interpretJobFetchError, isReportForCurrentPage } from "./job_flow.js";
import { clear } from "./report/dom.js";
import { SummaryStatus, parseSummaryResult, renderSummaryResult, summaryErrorLabel } from "./summary.js";
import { findSection } from "./report/view_model.js";
import {
  groupSections,
  materialChallengedStatements,
  renderNav,
  renderSection,
  renderExecutiveSnapshot,
  renderSynthesis,
  renderDocumentCorpus,
  renderMonitoring,
  renderDataGaps,
} from "./report/render.js";
import { challengeOutcomeLabel } from "./report/labels.js";

const START_COMMAND = "stock-research-ai serve-http";
const PAIR_CODE_COMMAND = "stock-research-ai pair-code";
const POLL_INTERVAL_MS = 1500;

registerPortalAdapter(new ScreenerAdapter());

const storage = createStorage(chrome.storage.local);

/** @type {ReturnType<typeof setTimeout> | null} */
let pollTimer = null;
let currentJobId = null;
// Only the latest boot may publish readiness or pairing state.
let bootGeneration = 0;

// Report-view UI state (Part 6B-C). Kept separate from the job/page facts above: a
// report is keyed by report_snapshot_id, not by job lifetime, and stays viewable across
// a page change or a lost/restarted job (Part 6B-A's persistence boundary). Sticky until
// the user explicitly clicks Back.
let viewingReportId = null;
let activeReport = null;
let activeSectionId = null;

// SUMMARY_V1 (Box 4): one summary, bound to the page URL it was requested for. Any
// navigation to another page resets it and invalidates an in-flight response, so an
// older company's summary can never render on a newer page.
const IDLE_SUMMARY = Object.freeze({ status: SummaryStatus.IDLE, sourceUrl: null, view: null, errorCode: null });
let summary = IDLE_SUMMARY;
let summaryRequestSeq = 0;

const els = {
  serviceStatusLine: document.getElementById("service-status-line"),
  panels: {
    [States.SERVICE_UNAVAILABLE]: document.getElementById("panel-service-unavailable"),
    [States.INCOMPATIBLE]: document.getElementById("panel-incompatible"),
    [States.UNPAIRED]: document.getElementById("panel-unpaired"),
    [States.UNSUPPORTED_PAGE]: document.getElementById("panel-unsupported"),
    [States.READY]: document.getElementById("panel-ready"),
    [States.LAUNCHING]: document.getElementById("panel-ready"),
    [States.RUNNING]: document.getElementById("panel-running"),
    [States.COMPLETED]: document.getElementById("panel-completed"),
    [States.FAILED]: document.getElementById("panel-failed"),
    [States.REPORT_LOADING]: document.getElementById("panel-report-loading"),
    [States.REPORT_READY]: document.getElementById("panel-report-ready"),
    [States.REPORT_NOT_FOUND]: document.getElementById("panel-report-not-found"),
    [States.REPORT_INTEGRITY_ERROR]: document.getElementById("panel-report-integrity-error"),
    [States.REPORT_LOAD_FAILED]: document.getElementById("panel-report-load-failed"),
  },
  startCommand: document.getElementById("start-command"),
  pairingStartCommand: document.getElementById("pairing-start-command"),
  pairCodeCommand: document.getElementById("pair-code-command"),
  pairingCodeInput: document.getElementById("pairing-code-input"),
  pairButton: document.getElementById("pair-button"),
  pairingError: document.getElementById("pairing-error"),
  currentPageLabel: document.getElementById("current-page-label"),
  researchButton: document.getElementById("research-button"),
  viewLastResearchButton: document.getElementById("view-last-research-button"),
  analystWarning: document.getElementById("analyst-warning"),
  stageChip: document.getElementById("stage-chip"),
  startedAtLabel: document.getElementById("started-at-label"),
  viewResearchButton: document.getElementById("view-research-button"),
  researchAgainButton: document.getElementById("research-again-button"),
  failureLabel: document.getElementById("failure-label"),
  failureDetails: document.getElementById("failure-details"),
  failureTechnical: document.getElementById("failure-technical"),
  retryButton: document.getElementById("retry-button"),
  retryServiceButton: document.getElementById("retry-service"),
  copyStartCommandButton: document.getElementById("copy-start-command"),
  repairButton: document.getElementById("repair-button"),
  versionLabel: document.getElementById("version-label"),
  incompatibleDetail: document.getElementById("incompatible-detail"),
  reportLoadFailedDetail: document.getElementById("report-load-failed-detail"),
  reportBackButton: document.getElementById("report-back-button"),
  reportNotFoundBack: document.getElementById("report-not-found-back"),
  reportIntegrityBack: document.getElementById("report-integrity-back"),
  reportLoadFailedBack: document.getElementById("report-load-failed-back"),
  reportLoadFailedRetry: document.getElementById("report-load-failed-retry"),
  reportNav: document.getElementById("report-nav"),
  reportContent: document.getElementById("report-content"),
  reportChallengesBanner: document.getElementById("report-challenges-banner"),
  artifactError: document.getElementById("artifact-error"),
  artifactInstitutionalReport: document.getElementById("artifact-institutional-report"),
  artifactInvestorBrief: document.getElementById("artifact-investor-brief"),
  artifactResearchJson: document.getElementById("artifact-research-json"),
  generateSummaryButton: document.getElementById("generate-summary-button"),
  summaryLoading: document.getElementById("summary-loading"),
  summaryError: document.getElementById("summary-error"),
  summaryResult: document.getElementById("summary-result"),
};

els.startCommand.textContent = START_COMMAND;
els.pairingStartCommand.textContent = START_COMMAND;
els.pairCodeCommand.textContent = PAIR_CODE_COMMAND;

/** @type {{sourceUrl: string|null, adapter: import('./portals/portal_adapter.js').PortalAdapter|null, pageIdentity: {page_identifier: string, page_mode: "standalone"|"consolidated"}|null}} */
const page = { sourceUrl: null, adapter: null, pageIdentity: null };

/** SUMMARY_V1 Box 1 label only - never invents a company name; it is always backend-sourced. */
const PAGE_MODE_LABELS = Object.freeze({ standalone: "Standalone", consolidated: "Consolidated" });

function renderState(state, data = {}) {
  for (const panel of Object.values(els.panels)) {
    if (panel) panel.hidden = true;
  }
  const panel = els.panels[state];
  if (panel) panel.hidden = false;

  if (state === States.UNSUPPORTED_PAGE || state === States.READY) {
    // UNSUPPORTED_PAGE never has a page identity (its adapter is always null), so this
    // branch only ever applies on READY - no extra state check needed.
    if (page.pageIdentity) {
      const { page_identifier, page_mode } = page.pageIdentity;
      els.currentPageLabel.textContent =
        `Screener company detected\n` +
        `Identifier: ${page_identifier}\n` +
        `Page: ${PAGE_MODE_LABELS[page_mode] || page_mode}\n` +
        `Company name: Pending data fetch`;
    } else {
      els.currentPageLabel.textContent = page.sourceUrl
        ? `Current page: ${page.sourceUrl}`
        : "Open a Screener company page to research a company.";
    }
  }

  if (state === States.READY || state === States.LAUNCHING) {
    els.researchButton.disabled = state === States.LAUNCHING;
    els.researchButton.textContent =
      state === States.LAUNCHING ? "Starting…" : "Research Company";
    els.analystWarning.hidden = data.analysisAvailable !== false;
    if (data.analysisAvailable === false) {
      els.analystWarning.textContent =
        "The live analyst is not configured on your local service. Research cannot complete yet.";
    }
    els.viewLastResearchButton.hidden = !data.lastReportForThisPage;
    renderSummaryView();
  }

  if (state === States.RUNNING) {
    els.stageChip.textContent = stageLabel(data.job && data.job.current_stage);
    els.startedAtLabel.textContent =
      data.job && data.job.started_at
        ? `Started ${new Date(data.job.started_at).toLocaleTimeString()}`
        : "";
  }

  if (state === States.FAILED) {
    const failure = (data.job && data.job.failure) || null;
    els.failureLabel.textContent = failureLabel(failure && failure.code);
    if (failure && failure.message) {
      els.failureTechnical.textContent = failure.message;
      els.failureDetails.hidden = false;
    } else {
      els.failureDetails.hidden = true;
    }
  }

  if (state === States.INCOMPATIBLE) {
    els.incompatibleDetail.textContent = data.incompatibleDetail || "";
  }
}

function setServiceStatusLine(text) {
  els.serviceStatusLine.textContent = text;
}

async function getActiveTabUrl() {
  return new Promise((resolve) => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tab = tabs && tabs[0];
      resolve((tab && tab.url) || null);
    });
  });
}

function stopPolling() {
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

async function refreshPage(isCurrent) {
  const url = await getActiveTabUrl();
  // A superseded boot's late tab lookup must never touch shared page state.
  if (!isCurrent()) return;
  page.sourceUrl = url;
  page.adapter = url ? resolvePortal(url) : null;
  // Recomputed fresh on every call (boot(), tab activation, tab URL update) - never
  // retains the previous page's identity across a navigation (SUMMARY_V1 Box 1).
  page.pageIdentity =
    page.adapter && typeof page.adapter.getPageIdentity === "function"
      ? page.adapter.getPageIdentity(url)
      : null;
}

async function boot() {
  if (viewingReportId) {
    // A report view is sticky: page/tab activity must never silently replace it.
    // Only the explicit Back button (closeReportView) leaves this mode.
    return;
  }
  const generation = ++bootGeneration;
  const isCurrent = () => generation === bootGeneration;
  const baseUrl = await storage.getServiceBaseUrl();
  if (!isCurrent()) return;
  const client = new ApiClient(baseUrl);
  els.versionLabel.textContent = "";

  await refreshPage(isCurrent);
  if (!isCurrent()) return;
  resetSummaryIfPageChanged();

  const facts = {
    serviceReachable: false,
    versionCompatible: false,
    paired: false,
    pageSupported: !!page.adapter,
  };
  const data = {};

  let health = null;
  try {
    health = await client.health();
  } catch {
    health = null;
  }
  if (!isCurrent()) return;
  facts.serviceReachable = !!(health && health.status === "ok");
  if (!facts.serviceReachable) {
    setServiceStatusLine("Local service not reachable");
    renderState(deriveState(facts), data);
    return;
  }

  let version = null;
  try {
    version = await client.version();
  } catch {
    version = null;
  }
  if (!isCurrent()) return;
  if (!version) {
    facts.serviceReachable = false;
    setServiceStatusLine("Local service not reachable");
    renderState(deriveState(facts), data);
    return;
  }
  els.versionLabel.textContent = `v${version.application_version}`;
  facts.versionCompatible = version.api_version === "v1";
  if (!facts.versionCompatible) {
    setServiceStatusLine("Service reachable · incompatible version");
    data.incompatibleDetail = `Extension expects API v1, service reports ${version.api_version}.`;
    renderState(deriveState(facts), data);
    return;
  }

  const token = await storage.getToken();
  if (!isCurrent()) return;
  facts.paired = !!token;
  if (!facts.paired) {
    setServiceStatusLine("Service reachable · not paired");
    renderState(deriveState(facts), data);
    return;
  }

  let capabilities = null;
  try {
    capabilities = await client.capabilities();
  } catch {
    capabilities = null;
  }
  if (!isCurrent()) return;
  data.analysisAvailable = !!(capabilities && capabilities.analysis_available);
  setServiceStatusLine("Service reachable · paired");

  const activeJob = await storage.getActiveJob();
  if (!isCurrent()) return;
  if (activeJob && activeJob.jobId) {
    await reconnectJob(client, token, activeJob.jobId, facts, isCurrent);
    return;
  }

  // A persisted report pointer (Part 6B-C) survives independently of any active job -
  // including a server restart that lost it. Only offered here when it belongs to the
  // page currently open, so an old company's report is never mislabeled as this page's.
  const lastReport = await storage.getLastReport();
  if (!isCurrent()) return;
  data.lastReportForThisPage = isReportForCurrentPage(lastReport, page.sourceUrl) ? lastReport : null;

  renderState(deriveState(facts), data);
}

async function requirePairingAfterUnauthorized() {
  stopPolling();
  currentJobId = null;
  const generation = ++bootGeneration;
  // Keep active-job and report pointers: a 401 says nothing about their existence.
  await storage.clearToken();
  if (generation !== bootGeneration) return;
  setServiceStatusLine("Service reachable · not paired");
  renderState(States.UNPAIRED, {});
  els.pairingError.textContent = "Pairing authorization was rejected. Pair again to reconnect your research.";
  els.pairingError.hidden = false;
}

async function reconnectJob(client, token, jobId, facts, isCurrent) {
  try {
    const job = await client.getResearchJob(token, jobId);
    if (!isCurrent()) return;
    currentJobId = jobId;
    await handleJobUpdate(client, token, job, facts);
  } catch (error) {
    if (!isCurrent()) return;
    if (error instanceof ApiError && error.status === 401) {
      await requirePairingAfterUnauthorized();
      return;
    }
    const outcome = interpretJobFetchError(error instanceof ApiError ? error : null);
    if (outcome === "LOST") {
      // Honest: never pretend a lost job is still running.
      await storage.clearActiveJob();
      if (!isCurrent()) return;
      renderState(deriveState(facts), {});
      return;
    }
    setServiceStatusLine("Local service not reachable");
    renderState(States.SERVICE_UNAVAILABLE, {});
  }
}

async function handleJobUpdate(client, token, job, facts) {
  const { clearActiveJob, lastReport } = storageActionsForJobUpdate(job);
  if (clearActiveJob) {
    stopPolling();
    if (lastReport) {
      await storage.setLastReport(lastReport);
    }
    await storage.clearActiveJob();
    renderState(deriveState({ ...facts, job }), { job });
    return;
  }
  renderState(deriveState({ ...facts, job }), { job });
  stopPolling();
  pollTimer = setTimeout(() => pollJob(client, token, facts), POLL_INTERVAL_MS);
}

async function pollJob(client, token, facts) {
  if (!currentJobId) return;
  try {
    const job = await client.getResearchJob(token, currentJobId);
    await handleJobUpdate(client, token, job, facts);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await requirePairingAfterUnauthorized();
      return;
    }
    const outcome = interpretJobFetchError(error instanceof ApiError ? error : null);
    if (outcome === "LOST") {
      await storage.clearActiveJob();
      currentJobId = null;
      renderState(deriveState(facts), {});
      return;
    }
    // Transient network hiccup: keep the loop alive rather than declaring failure.
    pollTimer = setTimeout(() => pollJob(client, token, facts), POLL_INTERVAL_MS);
  }
}

async function onResearchClick() {
  if (!page.adapter || !page.sourceUrl) return;
  const context = page.adapter.getSourceContext(page.sourceUrl);
  if (!context) return;

  const facts = {
    serviceReachable: true,
    versionCompatible: true,
    paired: true,
    pageSupported: true,
  };
  renderState(States.LAUNCHING, {});
  const baseUrl = await storage.getServiceBaseUrl();
  const client = new ApiClient(baseUrl);
  const token = await storage.getToken();
  try {
    const job = await client.createResearchJob(token, {
      portal: context.portal,
      source_url: context.source_url,
      research_plan: "FULL_RESEARCH_V1",
    });
    currentJobId = job.job_id;
    await storage.setActiveJob({
      jobId: job.job_id,
      portal: context.portal,
      sourceUrl: context.source_url,
    });
    await handleJobUpdate(client, token, job, facts);
  } catch (error) {
    renderState(States.FAILED, {
      job: {
        failure: {
          code: error instanceof ApiError ? error.code : "INTERNAL_ERROR",
          message: error && error.message,
        },
      },
    });
  }
}

function resetSummaryIfPageChanged() {
  if (summary.status !== SummaryStatus.IDLE && summary.sourceUrl !== page.sourceUrl) {
    summary = IDLE_SUMMARY;
    summaryRequestSeq += 1; // invalidates any in-flight request for the previous page
  }
}

function renderSummaryView() {
  const loading = summary.status === SummaryStatus.LOADING;
  els.generateSummaryButton.disabled = loading || !page.pageIdentity;
  els.generateSummaryButton.textContent = loading
    ? "Generating…"
    : summary.status === SummaryStatus.SUCCESS
      ? "Regenerate Summary"
      : "Generate Summary";
  els.summaryLoading.hidden = !loading;
  els.summaryError.hidden = summary.status !== SummaryStatus.ERROR;
  els.summaryError.textContent =
    summary.status === SummaryStatus.ERROR ? summaryErrorLabel(summary.errorCode) : "";
  clear(els.summaryResult);
  els.summaryResult.hidden = summary.status !== SummaryStatus.SUCCESS;
  if (summary.status === SummaryStatus.SUCCESS) {
    els.summaryResult.appendChild(renderSummaryResult(document, summary.view));
  }
}

async function onGenerateSummaryClick() {
  if (summary.status === SummaryStatus.LOADING) return;
  if (!page.adapter || !page.sourceUrl || !page.pageIdentity) return;
  const context = page.adapter.getSourceContext(page.sourceUrl);
  if (!context) return;
  const requestId = ++summaryRequestSeq;
  const requestedUrl = page.sourceUrl;
  const isCurrentSummary = () => requestId === summaryRequestSeq && page.sourceUrl === requestedUrl;
  summary = { status: SummaryStatus.LOADING, sourceUrl: requestedUrl, view: null, errorCode: null };
  renderSummaryView();

  let next;
  try {
    const baseUrl = await storage.getServiceBaseUrl();
    const token = await storage.getToken();
    const payload = await new ApiClient(baseUrl).createSummary(token, {
      portal: context.portal,
      source_url: context.source_url,
    });
    const view = parseSummaryResult(payload);
    next = view
      ? { status: SummaryStatus.SUCCESS, sourceUrl: requestedUrl, view, errorCode: null }
      : { status: SummaryStatus.ERROR, sourceUrl: requestedUrl, view: null, errorCode: "MALFORMED_RESPONSE" };
  } catch (error) {
    if (!isCurrentSummary()) return;
    if (error instanceof ApiError && error.status === 401) {
      summary = IDLE_SUMMARY;
      await requirePairingAfterUnauthorized();
      return;
    }
    next = {
      status: SummaryStatus.ERROR,
      sourceUrl: requestedUrl,
      view: null,
      errorCode: error instanceof ApiError ? error.code : "INTERNAL_ERROR",
    };
  }
  if (!isCurrentSummary()) return; // stale: the user navigated away meanwhile
  summary = next;
  renderSummaryView();
}

async function onPairClick() {
  const code = els.pairingCodeInput.value.trim();
  els.pairingError.hidden = true;
  if (!code) return;
  const baseUrl = await storage.getServiceBaseUrl();
  const client = new ApiClient(baseUrl);
  try {
    const result = await client.pair(code);
    await storage.setToken(result.token);
    els.pairingCodeInput.value = "";
    await boot();
  } catch (error) {
    els.pairingError.textContent =
      error instanceof ApiError
        ? "Pairing failed. Check the code and try again."
        : "Could not reach the local service.";
    els.pairingError.hidden = false;
  }
}

async function onRepairClick() {
  stopPolling();
  currentJobId = null;
  await storage.clearToken();
  await storage.clearActiveJob();
  await boot();
}

function onCopyStartCommand() {
  navigator.clipboard.writeText(START_COMMAND).catch(() => {});
}

function renderReportBody() {
  if (!activeReport) return;
  clear(els.reportNav);
  clear(els.reportContent);
  clear(els.reportChallengesBanner);

  const groups = groupSections(activeReport);
  els.reportNav.appendChild(renderNav(document, groups, activeSectionId, onSectionSelect));

  const challenged = materialChallengedStatements(activeReport);
  if (challenged.length > 0) {
    const p = document.createElement("p");
    p.textContent = `${challenged.length} statement(s) in this report were ${challengeOutcomeLabel(
      "CHALLENGED",
    )} or ${challengeOutcomeLabel("INVALID_AGAINST_BUNDLE")} by the independent challenger.`;
    els.reportChallengesBanner.appendChild(p);
    els.reportChallengesBanner.hidden = false;
  } else {
    els.reportChallengesBanner.hidden = true;
  }

  if (activeSectionId === "EXECUTIVE_RESEARCH_SNAPSHOT" && activeReport.executive_snapshot) {
    els.reportContent.appendChild(renderExecutiveSnapshot(document, activeReport.executive_snapshot));
    return;
  }
  if (activeSectionId === "RESEARCH_SYNTHESIS" && activeReport.synthesis) {
    els.reportContent.appendChild(renderSynthesis(document, activeReport.synthesis));
    return;
  }
  if (activeSectionId === "DOCUMENT_CORPUS_AND_EVIDENCE_COVERAGE") {
    els.reportContent.appendChild(renderDocumentCorpus(document, activeReport));
    return;
  }
  if (activeSectionId === "MONITORING_DASHBOARD") {
    els.reportContent.appendChild(renderMonitoring(document, activeReport.monitoring));
    return;
  }
  if (activeSectionId === "DATA_GAPS_AND_LIMITATIONS") {
    els.reportContent.appendChild(renderDataGaps(document, activeReport.limitations));
    return;
  }

  const section = findSection(activeReport, activeSectionId);
  if (section) {
    const entry = groups.flatMap((g) => g.sections).find((s) => s.id === activeSectionId);
    els.reportContent.appendChild(renderSection(document, { section, label: entry ? entry.label : activeSectionId }));
  } else {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "This section is not present in this report.";
    els.reportContent.appendChild(p);
  }
}

function onSectionSelect(sectionId) {
  activeSectionId = sectionId;
  renderReportBody();
}

async function openReport(reportId) {
  viewingReportId = reportId;
  activeReport = null;
  stopPolling();
  renderState(States.REPORT_LOADING, {});

  const baseUrl = await storage.getServiceBaseUrl();
  const client = new ApiClient(baseUrl);
  const token = await storage.getToken();
  try {
    const report = await client.getReport(token, reportId);
    activeReport = report;
    const groups = groupSections(report);
    activeSectionId = groups.length && groups[0].sections.length ? groups[0].sections[0].id : null;
    renderState(States.REPORT_READY, {});
    renderReportBody();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      viewingReportId = null;
      await storage.clearToken();
      await boot();
      return;
    }
    if (error instanceof ApiError && error.status === 404) {
      renderState(States.REPORT_NOT_FOUND, {});
      return;
    }
    if (error instanceof ApiError && error.status >= 500) {
      renderState(States.REPORT_INTEGRITY_ERROR, {});
      return;
    }
    els.reportLoadFailedDetail.textContent =
      error instanceof ApiError ? error.message : "The local service is not reachable.";
    renderState(States.REPORT_LOAD_FAILED, {});
  }
}

async function closeReportView() {
  viewingReportId = null;
  activeReport = null;
  activeSectionId = null;
  clear(els.reportNav);
  clear(els.reportContent);
  await boot();
}

function sanitizeFilenamePart(value) {
  return String(value || "report").replace(/[^A-Za-z0-9_-]+/g, "_").slice(0, 60);
}

const ARTIFACT_FILE_INFO = Object.freeze({
  INSTITUTIONAL_REPORT: { suffix: "institutional_report", ext: "md" },
  INVESTOR_BRIEF: { suffix: "investor_brief", ext: "md" },
  RESEARCH_JSON: { suffix: "research", ext: "json" },
});

async function downloadArtifact(artifactKind) {
  if (!viewingReportId) return;
  els.artifactError.hidden = true;
  const baseUrl = await storage.getServiceBaseUrl();
  const client = new ApiClient(baseUrl);
  const token = await storage.getToken();
  try {
    const { text, contentType } = await client.getArtifact(token, viewingReportId, artifactKind);
    const info = ARTIFACT_FILE_INFO[artifactKind];
    const filename = `${sanitizeFilenamePart(viewingReportId)}_${info.suffix}.${info.ext}`;
    const blob = new Blob([text], { type: contentType || "text/plain" });
    const url = URL.createObjectURL(blob);
    try {
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
    } finally {
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  } catch (error) {
    els.artifactError.textContent =
      error instanceof ApiError ? "Could not retrieve this artifact." : "The local service is not reachable.";
    els.artifactError.hidden = false;
  }
}

els.researchButton.addEventListener("click", onResearchClick);
els.generateSummaryButton.addEventListener("click", onGenerateSummaryClick);
els.viewLastResearchButton.addEventListener("click", async () => {
  const report = await storage.getLastReport();
  if (report && report.reportId) {
    await openReport(report.reportId);
  }
});
els.researchAgainButton.addEventListener("click", onResearchClick);
els.retryButton.addEventListener("click", () => boot());
els.retryServiceButton.addEventListener("click", () => boot());
els.pairButton.addEventListener("click", onPairClick);
els.repairButton.addEventListener("click", onRepairClick);
els.copyStartCommandButton.addEventListener("click", onCopyStartCommand);
els.viewResearchButton.addEventListener("click", async () => {
  const report = await storage.getLastReport();
  if (report && report.reportId) {
    await openReport(report.reportId);
  }
});
els.reportBackButton.addEventListener("click", closeReportView);
els.reportNotFoundBack.addEventListener("click", closeReportView);
els.reportIntegrityBack.addEventListener("click", closeReportView);
els.reportLoadFailedBack.addEventListener("click", closeReportView);
els.reportLoadFailedRetry.addEventListener("click", () => {
  if (viewingReportId) openReport(viewingReportId);
});
els.artifactInstitutionalReport.addEventListener("click", () => downloadArtifact("INSTITUTIONAL_REPORT"));
els.artifactInvestorBrief.addEventListener("click", () => downloadArtifact("INVESTOR_BRIEF"));
els.artifactResearchJson.addEventListener("click", () => downloadArtifact("RESEARCH_JSON"));

window.addEventListener("pagehide", stopPolling);

if (chrome.tabs && chrome.tabs.onActivated) {
  chrome.tabs.onActivated.addListener(() => boot());
}
if (chrome.tabs && chrome.tabs.onUpdated) {
  chrome.tabs.onUpdated.addListener((_tabId, changeInfo) => {
    if (changeInfo.url) boot();
  });
}

boot();
