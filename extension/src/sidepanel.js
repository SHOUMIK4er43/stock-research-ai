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
import { storageActionsForJobUpdate, interpretJobFetchError } from "./job_flow.js";

const START_COMMAND = "stock-research-ai serve-http";
const PAIR_CODE_COMMAND = "stock-research-ai pair-code";
const POLL_INTERVAL_MS = 1500;

registerPortalAdapter(new ScreenerAdapter());

const storage = createStorage(chrome.storage.local);

/** @type {ReturnType<typeof setTimeout> | null} */
let pollTimer = null;
let currentJobId = null;

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
  },
  startCommand: document.getElementById("start-command"),
  pairingStartCommand: document.getElementById("pairing-start-command"),
  pairCodeCommand: document.getElementById("pair-code-command"),
  pairingCodeInput: document.getElementById("pairing-code-input"),
  pairButton: document.getElementById("pair-button"),
  pairingError: document.getElementById("pairing-error"),
  currentPageLabel: document.getElementById("current-page-label"),
  researchButton: document.getElementById("research-button"),
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
};

els.startCommand.textContent = START_COMMAND;
els.pairingStartCommand.textContent = START_COMMAND;
els.pairCodeCommand.textContent = PAIR_CODE_COMMAND;

/** @type {{sourceUrl: string|null, adapter: import('./portals/portal_adapter.js').PortalAdapter|null}} */
const page = { sourceUrl: null, adapter: null };

function renderState(state, data = {}) {
  for (const panel of Object.values(els.panels)) {
    if (panel) panel.hidden = true;
  }
  const panel = els.panels[state];
  if (panel) panel.hidden = false;

  if (state === States.UNSUPPORTED_PAGE || state === States.READY) {
    els.currentPageLabel.textContent = page.sourceUrl
      ? `Current page: ${page.sourceUrl}`
      : "Open a Screener company page to research a company.";
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

async function refreshPage() {
  const url = await getActiveTabUrl();
  page.sourceUrl = url;
  page.adapter = url ? resolvePortal(url) : null;
}

async function boot() {
  const baseUrl = await storage.getServiceBaseUrl();
  const client = new ApiClient(baseUrl);
  els.versionLabel.textContent = "";

  await refreshPage();

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
  if (!version) {
    facts.serviceReachable = false;
    setServiceStatusLine("Local service not reachable");
    renderState(deriveState(facts), data);
    return;
  }
  els.versionLabel.textContent = `v${version.application_version}`;
  facts.versionCompatible = version.api_version === "v1";
  if (!facts.versionCompatible) {
    data.incompatibleDetail = `Extension expects API v1, service reports ${version.api_version}.`;
    renderState(deriveState(facts), data);
    return;
  }

  const token = await storage.getToken();
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
  data.analysisAvailable = !!(capabilities && capabilities.analysis_available);
  setServiceStatusLine("Service reachable · paired");

  const activeJob = await storage.getActiveJob();
  if (activeJob && activeJob.jobId) {
    await reconnectJob(client, token, activeJob.jobId, facts);
    return;
  }

  renderState(deriveState(facts), data);
}

async function reconnectJob(client, token, jobId, facts) {
  try {
    const job = await client.getResearchJob(token, jobId);
    currentJobId = jobId;
    await handleJobUpdate(client, token, job, facts);
  } catch (error) {
    const outcome = interpretJobFetchError(error instanceof ApiError ? error : null);
    if (outcome === "LOST") {
      // Honest: never pretend a lost job is still running.
      await storage.clearActiveJob();
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

els.researchButton.addEventListener("click", onResearchClick);
els.researchAgainButton.addEventListener("click", onResearchClick);
els.retryButton.addEventListener("click", () => boot());
els.retryServiceButton.addEventListener("click", () => boot());
els.pairButton.addEventListener("click", onPairClick);
els.repairButton.addEventListener("click", onRepairClick);
els.copyStartCommandButton.addEventListener("click", onCopyStartCommand);
els.viewResearchButton.addEventListener("click", async () => {
  const report = await storage.getLastReport();
  if (report) {
    els.viewResearchButton.textContent = `Report ready (${report.reportId.slice(0, 8)}…)`;
  }
});

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
