/**
 * Screener.in portal adapter (Part 6B-B).
 *
 * Mirrors the exact, existing, frozen backend URL contract
 * (src/stock_research_ai/adapters/screener/source.py::validate_url) for UX purposes
 * only - conservative and deterministic, but never authoritative. The backend
 * independently re-validates every request; this adapter's only job is to decide
 * whether to offer the "Research Company" action for the current page, and to build
 * the minimal transport context the backend actually needs.
 *
 * Never scrapes price, P/E, company name, ratios, financial tables, sector, or symbol
 * from the DOM - the only trusted output is {portal, source_url}.
 */

const SCREENER_HOSTNAME = "www.screener.in";
const SCREENER_PATH_PATTERN = /^\/company\/[A-Za-z0-9_-]+\/(?:consolidated\/)?$/;

export class ScreenerAdapter {
  id = "SCREENER";

  /** @param {string} urlString @returns {boolean} */
  isSupportedUrl(urlString) {
    let url;
    try {
      url = new URL(urlString);
    } catch {
      return false;
    }
    if (url.protocol !== "https:") return false;
    if (url.hostname !== SCREENER_HOSTNAME) return false;
    if (url.username || url.password) return false;
    if (url.port !== "" && url.port !== "443") return false;
    if (url.search) return false;
    if (url.hash) return false;
    return SCREENER_PATH_PATTERN.test(url.pathname);
  }

  /** @param {string} urlString @returns {{portal: "SCREENER", source_url: string} | null} */
  getSourceContext(urlString) {
    if (!this.isSupportedUrl(urlString)) return null;
    return { portal: this.id, source_url: urlString };
  }
}
