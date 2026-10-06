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
 * from the DOM. The only trusted outputs are {portal, source_url} (getSourceContext)
 * and the generic page identity below (getPageIdentity, SUMMARY_V1 Box 1) - a path
 * identifier and page mode read directly from the URL shape, never the authoritative
 * company identity. `page_identifier` is only ever the Screener path segment: it is
 * NOT an NSE ticker, NOT an exchange symbol, and no company name is ever derived from
 * it here - that identity can only come from the backend's own extraction later.
 */

const SCREENER_HOSTNAME = "www.screener.in";
const SCREENER_PATH_PATTERN = /^\/company\/[A-Za-z0-9_-]+\/(?:consolidated\/)?$/;
const SCREENER_PAGE_IDENTITY_PATTERN = /^\/company\/([A-Za-z0-9_-]+)\/(consolidated\/)?$/;

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

  /**
   * Generic page identity for a supported Screener company page (SUMMARY_V1 Box 1).
   * Answers only "which page is this", never "which company is this": `page_identifier`
   * is the exact Screener path segment (which may be alphabetic, numeric, or contain
   * underscore/hyphen) and is never labeled as an NSE ticker or exchange symbol, and no
   * company name is inferred from it or from page content. Returns null for any
   * unsupported URL - identical rejection rules to isSupportedUrl/getSourceContext.
   *
   * @param {string} urlString
   * @returns {{portal: "SCREENER", source_url: string, page_identifier: string, page_mode: "standalone" | "consolidated"} | null}
   */
  getPageIdentity(urlString) {
    if (!this.isSupportedUrl(urlString)) return null;
    const url = new URL(urlString);
    const match = SCREENER_PAGE_IDENTITY_PATTERN.exec(url.pathname);
    return {
      portal: this.id,
      source_url: urlString,
      page_identifier: match[1],
      page_mode: match[2] ? "consolidated" : "standalone",
    };
  }
}
