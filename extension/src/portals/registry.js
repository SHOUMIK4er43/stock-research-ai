/**
 * One portal registry (Part 6B-B). Adding a future portal (e.g. TradingView) requires
 * exactly one adapter + one registration here - never a change to pairing, the API
 * client, job polling, progress, or security/auth.
 */

/** @type {import('./portal_adapter.js').PortalAdapter[]} */
const adapters = [];

/** @param {import('./portal_adapter.js').PortalAdapter} adapter */
export function registerPortalAdapter(adapter) {
  adapters.push(adapter);
}

/**
 * @param {string} urlString
 * @returns {import('./portal_adapter.js').PortalAdapter | null}
 */
export function resolvePortal(urlString) {
  for (const adapter of adapters) {
    if (adapter.isSupportedUrl(urlString)) return adapter;
  }
  return null;
}

/** Test-only: reset registry state between test cases. */
export function clearRegistry() {
  adapters.length = 0;
}

/** Test-only: number of registered adapters. */
export function registeredCount() {
  return adapters.length;
}
