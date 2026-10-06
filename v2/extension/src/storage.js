/**
 * chrome.storage.local wrapper (Part 6B-B). ONLY chrome.storage.local is ever used -
 * never chrome.storage.sync (a pairing token must never leave this machine).
 *
 * Stores the minimum: service base URL, pairing token, a lightweight active-job
 * pointer, and a lightweight completed-report pointer. Never a full report, raw
 * evidence, documents, provider keys, cookies, or browsing history.
 *
 * The storage backend is injected so this module is testable without Chrome - pass
 * `chrome.storage.local` in the extension itself, and a small in-memory fake in tests.
 * @typedef {Object} StorageBackend
 * @property {(keys: string | string[]) => Promise<Object>} get
 * @property {(items: Object) => Promise<void>} set
 * @property {(keys: string | string[]) => Promise<void>} remove
 */

export const KEYS = Object.freeze({
  SERVICE_BASE_URL: "serviceBaseUrl",
  PAIRING_TOKEN: "pairingToken",
  ACTIVE_JOB: "activeJob",
  LAST_REPORT: "lastReport",
});

export const DEFAULT_SERVICE_BASE_URL = "http://127.0.0.1:8000";

/**
 * @param {StorageBackend} backend
 */
export function createStorage(backend) {
  return {
    async getServiceBaseUrl() {
      const stored = await backend.get(KEYS.SERVICE_BASE_URL);
      return stored[KEYS.SERVICE_BASE_URL] || DEFAULT_SERVICE_BASE_URL;
    },
    async setServiceBaseUrl(url) {
      await backend.set({ [KEYS.SERVICE_BASE_URL]: url });
    },

    async getToken() {
      const stored = await backend.get(KEYS.PAIRING_TOKEN);
      return stored[KEYS.PAIRING_TOKEN] || null;
    },
    async setToken(token) {
      await backend.set({ [KEYS.PAIRING_TOKEN]: token });
    },
    async clearToken() {
      await backend.remove(KEYS.PAIRING_TOKEN);
    },

    /** @returns {Promise<{jobId: string, portal: string, sourceUrl: string} | null>} */
    async getActiveJob() {
      const stored = await backend.get(KEYS.ACTIVE_JOB);
      return stored[KEYS.ACTIVE_JOB] || null;
    },
    async setActiveJob(job) {
      await backend.set({ [KEYS.ACTIVE_JOB]: job });
    },
    async clearActiveJob() {
      await backend.remove(KEYS.ACTIVE_JOB);
    },

    /** @returns {Promise<{reportId: string, sourceUrl: string} | null>} */
    async getLastReport() {
      const stored = await backend.get(KEYS.LAST_REPORT);
      return stored[KEYS.LAST_REPORT] || null;
    },
    async setLastReport(report) {
      await backend.set({ [KEYS.LAST_REPORT]: report });
    },
  };
}

/** Test-only: a minimal in-memory StorageBackend matching chrome.storage.local's shape. */
export function createMemoryBackend() {
  const data = {};
  return {
    async get(keys) {
      const keyList = Array.isArray(keys) ? keys : [keys];
      const result = {};
      for (const key of keyList) {
        if (key in data) result[key] = data[key];
      }
      return result;
    },
    async set(items) {
      Object.assign(data, items);
    },
    async remove(keys) {
      const keyList = Array.isArray(keys) ? keys : [keys];
      for (const key of keyList) delete data[key];
    },
    _dump: () => ({ ...data }),
  };
}
