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
import {
  FINAL_REPORT_LABELS,
  FINAL_REPORT_POLL,
  FinalReportState,
  failureText,
  finalReportFilename,
  jobStageLabel,
  latestFinalReport,
  pageKey,
  parseFinalReportJob,
  parsePanel,
  renderDetailedResearch,
  renderRisks,
  researchStatusLabel,
} from "./final_report.js";
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
  tabs: {
    SUMMARY: document.getElementById("tab-summary"),
    DETAILED: document.getElementById("tab-detailed"),
    RISKS: document.getElementById("tab-risks"),
  },
  views: {
    SUMMARY: document.getElementById("view-summary"),
    DETAILED: document.getElementById("view-detailed"),
    RISKS: document.getElementById("view-risks"),
  },
  finalReportContent: document.getElementById("final-report-content"),
  risksStatus: document.getElementById("risks-status"),
  risksGenerateButton: document.getElementById("risks-generate-button"),
  risksContent: document.getElementById("risks-content"),
  finalReportButton: document.getElementById("final-report-button"),
  finalReportStatus: document.getElementById("final-report-status"),
  finalReportDownloads: document.getElementById("final-report-downloads"),
  finalReportError: document.getElementById("final-report-error"),
  finalReportFormats: {
    PDF: document.getElementById("final-report-pdf"),
    HTML: document.getElementById("final-report-html"),
    MARKDOWN: document.getElementById("final-report-markdown"),
    JSON: document.getElementById("final-report-json"),
  },
};

// The three views. Detailed Research and Negatives / Risks are two views of the SAME
// Box 14/15 final report for this page. Nothing is requested at boot; opening either view
// looks the report up (and starts generation when none exists). Everything is bound to the
// page URL it was requested for, like SUMMARY_V1.
let activeView = "SUMMARY";
const IDLE_FINAL_REPORT = Object.freeze({
  sourceUrl: null, state: FinalReportState.IDLE, panel: null, job: null, failureCode: null,
});
let finalReport = IDLE_FINAL_REPORT;
let finalReportSeq = 0;

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
      state === States.LAUNCHING ? "Starting…" : "Run legacy research";
    els.analystWarning.hidden = data.analysisAvailable !== false;
    if (data.analysisAvailable === false) {
      els.analystWarning.textContent =
        "The live analyst is not configured on your local service. Research cannot complete yet.";
    }
    els.viewLastResearchButton.hidden = !data.lastReportForThisPage;
    renderSummaryView();
    renderFinalReportView();
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
  resetFinalReportIfPageChanged();

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
    if (error instanceof ApiError && error.status === 401) {
      // Expired, revoked or replaced pairing: drop the stale token and ask to pair again.
      await requirePairingAfterUnauthorized();
      return;
    }
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

function resetFinalReportIfPageChanged() {
  if (finalReport.sourceUrl !== null && finalReport.sourceUrl !== page.sourceUrl) {
    finalReport = IDLE_FINAL_REPORT;
    finalReportSeq += 1; // any in-flight lookup / poll for the previous page is now stale
    els.finalReportError.hidden = true;
    renderFinalReportView();
    if (activeView !== "SUMMARY") ensureFinalReport({ generate: false });
  }
}

function showView(view) {
  activeView = view;
  for (const [name, tab] of Object.entries(els.tabs)) {
    if (tab && typeof tab.setAttribute === "function") {
      tab.setAttribute("aria-selected", String(name === view));
    }
  }
  for (const [name, panel] of Object.entries(els.views)) {
    if (panel) panel.hidden = name !== view;
  }
}

function selectView(view) {
  showView(view);
  if (view !== "SUMMARY") ensureFinalReport({ generate: true });
}

function isCurrentFinalReport(requestId, requestedUrl) {
  return requestId === finalReportSeq && page.sourceUrl === requestedUrl;
}

function setFinalReport(update) {
  finalReport = { ...finalReport, ...update };
  renderFinalReportView();
}

function renderFinalReportView() {
  const current = finalReport.sourceUrl !== null && finalReport.sourceUrl === page.sourceUrl;
  const state = current ? finalReport.state : FinalReportState.IDLE;
  const panel = current ? finalReport.panel : null;
  const busy = state === FinalReportState.LOOKING || state === FinalReportState.GENERATING;
  let status = "";
  if (state === FinalReportState.LOOKING) status = "Looking for Detailed Research…";
  else if (state === FinalReportState.NOT_GENERATED) status = "Detailed Research has not been generated for this company yet.";
  else if (state === FinalReportState.GENERATING) status = jobStageLabel(finalReport.job);
  else if (panel) {
    const partial = panel.failed.length
      ? ` Partial: ${panel.failed.map(f => FINAL_REPORT_LABELS[f] || f).join(", ")} could not be produced.` : "";
    status = `Ready — ${panel.companyName} — as of ${panel.asOf}. ${researchStatusLabel(panel)}.${partial}`;
  }
  els.finalReportStatus.textContent = status;
  els.finalReportStatus.hidden = !status;
  const failed = state === FinalReportState.FAILED;
  els.finalReportError.textContent = failed ? failureText(finalReport.failureCode) : "";
  els.finalReportError.hidden = !failed;
  els.finalReportButton.disabled = busy || !page.pageIdentity;
  els.finalReportButton.textContent = panel ? "Regenerate Detailed Research" : "Generate Detailed Research";
  clear(els.finalReportContent);
  clear(els.risksContent);
  els.finalReportContent.hidden = !panel;
  els.risksContent.hidden = !panel;
  if (panel) {
    els.finalReportContent.appendChild(renderDetailedResearch(document, panel));
    els.risksContent.appendChild(renderRisks(document, panel));
  }
  els.finalReportDownloads.hidden = !panel;
  for (const [format, button] of Object.entries(els.finalReportFormats)) {
    button.disabled = !panel || !panel.formats.includes(format);
  }
  els.risksStatus.textContent = panel ? "" : failed ? failureText(finalReport.failureCode)
    : status || "Negatives / Risks come from the Detailed Research report.";
  els.risksGenerateButton.hidden = !(state === FinalReportState.NOT_GENERATED || failed);
  els.risksGenerateButton.disabled = busy;
}

async function finalReportFailure(error, requestId, requestedUrl, fallbackCode) {
  if (!isCurrentFinalReport(requestId, requestedUrl)) return;
  if (error instanceof ApiError && error.status === 401) {
    await requirePairingAfterUnauthorized();
    return;
  }
  const code = error instanceof ApiError && error.code === "FINAL_REPORT_JOB_LIMIT_REACHED"
    ? error.code : error instanceof ApiError && error.status !== 0 ? fallbackCode : "NETWORK";
  setFinalReport({ state: FinalReportState.FAILED, failureCode: code, job: null });
}

async function loadFinalReportPanel(client, token, artifactId, requestId, requestedUrl) {
  const panel = parsePanel(await client.getFinalReportView(token, artifactId));
  if (!isCurrentFinalReport(requestId, requestedUrl)) return;
  if (!panel) {
    setFinalReport({ state: FinalReportState.FAILED, failureCode: "INVALID_VIEW", panel: null });
    return;
  }
  setFinalReport({
    state: panel.failed.length ? FinalReportState.PARTIAL : FinalReportState.READY,
    panel, job: null, failureCode: null,
  });
}

/** Look this page's report up; when none exists and `generate`, start generation. */
async function ensureFinalReport({ generate }) {
  if (!page.pageIdentity || !page.sourceUrl) return;
  const key = pageKey(page.pageIdentity);
  if (!key) return;
  if (finalReport.sourceUrl === page.sourceUrl && finalReport.state !== FinalReportState.IDLE
      && finalReport.state !== FinalReportState.NOT_GENERATED) return;
  if (finalReport.sourceUrl === page.sourceUrl && finalReport.state === FinalReportState.NOT_GENERATED) {
    if (generate) await generateFinalReport();
    return;
  }
  const requestedUrl = page.sourceUrl;
  const requestId = ++finalReportSeq;
  finalReport = { ...IDLE_FINAL_REPORT, sourceUrl: requestedUrl };
  setFinalReport({ state: FinalReportState.LOOKING });
  const client = new ApiClient(await storage.getServiceBaseUrl());
  const token = await storage.getToken();
  try {
    const report = latestFinalReport(await client.listFinalReportsForPage(token, key));
    if (!isCurrentFinalReport(requestId, requestedUrl)) return;
    if (report) {
      await loadFinalReportPanel(client, token, report.artifactId, requestId, requestedUrl);
      return;
    }
    setFinalReport({ state: FinalReportState.NOT_GENERATED });
  } catch (error) {
    await finalReportFailure(error, requestId, requestedUrl, "LOOKUP_FAILED");
    return;
  }
  if (generate) await generateFinalReport();
}

/** Start (or join) backend generation for this page and follow it to a terminal state. */
async function generateFinalReport() {
  if (!page.pageIdentity || !page.sourceUrl || !pageKey(page.pageIdentity)) return;
  if (finalReport.sourceUrl === page.sourceUrl && (finalReport.state === FinalReportState.GENERATING
      || finalReport.state === FinalReportState.LOOKING)) return;
  const requestedUrl = page.sourceUrl;
  const requestId = ++finalReportSeq;
  finalReport = { ...finalReport, sourceUrl: requestedUrl };
  setFinalReport({ state: FinalReportState.GENERATING, job: null, failureCode: null });
  const client = new ApiClient(await storage.getServiceBaseUrl());
  const token = await storage.getToken();
  let job;
  try {
    job = parseFinalReportJob(await client.createFinalReport(token, requestedUrl));
  } catch (error) {
    await finalReportFailure(error, requestId, requestedUrl, "GENERATION_REQUEST_FAILED");
    return;
  }
  for (let polls = 0; job && polls < FINAL_REPORT_POLL.maxPolls; polls += 1) {
    if (!isCurrentFinalReport(requestId, requestedUrl)) return;
    setFinalReport({ job });
    if (job.status === "READY" || job.status === "PARTIAL") {
      try {
        await loadFinalReportPanel(client, token, job.artifactId, requestId, requestedUrl);
      } catch (error) {
        await finalReportFailure(error, requestId, requestedUrl, "LOOKUP_FAILED");
      }
      return;
    }
    if (job.status === "FAILED") {
      setFinalReport({ state: FinalReportState.FAILED, failureCode: job.failureCode, job: null });
      return;
    }
    await new Promise(resolve => setTimeout(resolve, FINAL_REPORT_POLL.ms));
    if (!isCurrentFinalReport(requestId, requestedUrl)) return;
    try {
      job = parseFinalReportJob(await client.getFinalReportJob(token, job.jobId));
    } catch (error) {
      await finalReportFailure(error, requestId, requestedUrl,
        error instanceof ApiError && error.status === 404 ? "JOB_LOST" : "GENERATION_REQUEST_FAILED");
      return;
    }
  }
  if (isCurrentFinalReport(requestId, requestedUrl)) {
    setFinalReport({ state: FinalReportState.FAILED, failureCode: job ? "GENERATION_TIMEOUT" : "INVALID_JOB", job: null });
  }
}

async function downloadFinalReport(format) {
  const panel = finalReport.sourceUrl === page.sourceUrl ? finalReport.panel : null;
  if (!panel || !panel.formats.includes(format)) return;
  els.finalReportError.hidden = true;
  const client = new ApiClient(await storage.getServiceBaseUrl());
  const token = await storage.getToken();
  try {
    const { blob } = await client.getFinalReportFile(token, panel.artifactId, format);
    const url = URL.createObjectURL(blob);
    try {
      const link = document.createElement("a");
      link.href = url;
      link.download = finalReportFilename({ symbol: panel.symbol, asOf: panel.asOf }, format);
      document.body.appendChild(link);
      link.click();
      link.remove();
    } finally {
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await requirePairingAfterUnauthorized();
      return;
    }
    els.finalReportError.textContent =
      error instanceof ApiError ? "Could not retrieve this report file." : "The local service is not reachable.";
    els.finalReportError.hidden = false;
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
    if (error instanceof ApiError && error.status === 401) {
      viewingReportId = null;
      activeReport = null;
      await requirePairingAfterUnauthorized();
      return;
    }
    els.artifactError.textContent =
      error instanceof ApiError ? "Could not retrieve this artifact." : "The local service is not reachable.";
    els.artifactError.hidden = false;
  }
}

els.researchButton.addEventListener("click", onResearchClick);
els.generateSummaryButton.addEventListener("click", onGenerateSummaryClick);
els.finalReportButton.addEventListener("click", () => generateFinalReport());
els.risksGenerateButton.addEventListener("click", () => generateFinalReport());
for (const [view, tab] of Object.entries(els.tabs)) {
  tab.addEventListener("click", () => selectView(view));
}
showView("SUMMARY"); // the initial view; nothing is requested until a view is opened
for (const [format, button] of Object.entries(els.finalReportFormats)) {
  button.addEventListener("click", () => downloadFinalReport(format));
}
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
