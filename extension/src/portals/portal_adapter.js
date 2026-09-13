/**
 * Portal adapter contract (documentation-only - JS has no interfaces).
 *
 * A portal adapter identifies whether a URL belongs to its portal and, if so,
 * builds the ONLY context the backend transport needs. It must never contain
 * financial semantics, sector logic, report sections, valuation logic, or any
 * other research rule - all of that is backend-owned.
 *
 * @typedef {Object} SourceContext
 * @property {string} portal - e.g. "SCREENER"
 * @property {string} source_url - the exact, current, supported page URL
 *
 * @typedef {Object} PortalAdapter
 * @property {string} id - stable portal identifier, e.g. "SCREENER"
 * @property {(url: string) => boolean} isSupportedUrl
 * @property {(url: string) => SourceContext | null} getSourceContext
 */

export {};
