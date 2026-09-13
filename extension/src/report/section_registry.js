/**
 * ONE frontend mapping from the canonical backend ReportSectionId (Part 6A,
 * domain/report.py) to a display group, human label, and ordering. PRESENTATION ONLY -
 * no business/coverage/research semantics are encoded here. An unknown future section
 * identifier must never crash the viewer - it falls back to a generic "Other" group
 * with its own canonical (unmodified) identifier as the label.
 */

export const GROUPS = Object.freeze([
  "OVERVIEW",
  "BUSINESS",
  "FINANCIALS",
  "DOCUMENTS",
  "QUALITY",
  "MARKET",
  "VALUATION",
  "THESIS",
  "CHALLENGER",
  "LIMITATIONS",
  "APPENDICES",
  "OTHER",
]);

export const GROUP_LABELS = Object.freeze({
  OVERVIEW: "Overview",
  BUSINESS: "Business",
  FINANCIALS: "Financials",
  DOCUMENTS: "Documents",
  QUALITY: "Quality",
  MARKET: "Market",
  VALUATION: "Valuation",
  THESIS: "Thesis",
  CHALLENGER: "Independent Challenger",
  LIMITATIONS: "Limitations",
  APPENDICES: "Appendices",
  OTHER: "Other",
});

// [sectionId, group, label] - order below is the intra-group display order.
const REGISTRY_ROWS = [
  ["EXECUTIVE_RESEARCH_SNAPSHOT", "OVERVIEW", "Executive Snapshot"],
  ["COMPANY_IDENTITY_AND_EVIDENCE_FILE", "OVERVIEW", "Company Identity & Evidence File"],
  ["RESEARCH_SYNTHESIS", "OVERVIEW", "Research Synthesis"],

  ["BUSINESS_MODEL", "BUSINESS", "Business Model"],
  ["INDUSTRY_AND_COMPETITIVE_STRUCTURE", "BUSINESS", "Industry & Competitive Structure"],

  ["ANNUAL_FINANCIAL_PERFORMANCE", "FINANCIALS", "Annual Financial Performance"],
  ["QUARTERLY_PERFORMANCE_AND_INFLECTION", "FINANCIALS", "Quarterly Performance & Inflection"],
  ["BALANCE_SHEET_ANALYSIS", "FINANCIALS", "Balance Sheet Analysis"],
  ["CASH_FLOW_AND_EARNINGS_QUALITY", "FINANCIALS", "Cash Flow & Earnings Quality"],
  ["SECTOR_SPECIFIC_KPI_ANALYSIS", "FINANCIALS", "Sector-Specific KPIs"],

  ["DOCUMENT_CORPUS_AND_EVIDENCE_COVERAGE", "DOCUMENTS", "Document Corpus & Coverage"],
  ["DEEP_MANAGEMENT_AND_DOCUMENT_INTELLIGENCE", "DOCUMENTS", "Management & Document Intelligence"],
  ["CONCALL_INTELLIGENCE", "DOCUMENTS", "Concall Intelligence"],
  ["MANAGEMENT_GUIDANCE_AND_COMMITMENTS", "DOCUMENTS", "Management Guidance & Commitments"],
  ["MANAGEMENT_GUIDANCE_VS_ACTUAL", "DOCUMENTS", "Guidance vs. Actual"],
  ["MANAGEMENT_NARRATIVE_CHANGE_DETECTION", "DOCUMENTS", "Management Narrative Change"],
  ["AGM_INTELLIGENCE", "DOCUMENTS", "AGM Intelligence"],
  ["ANNUAL_REPORT_INTELLIGENCE", "DOCUMENTS", "Annual Report Intelligence"],
  ["INVESTOR_PRESENTATION_INTELLIGENCE", "DOCUMENTS", "Investor Presentation Intelligence"],
  ["MANAGEMENT_CREDIBILITY_CONSISTENCY", "DOCUMENTS", "Management Credibility & Consistency"],

  ["OWNERSHIP_AND_SHAREHOLDING", "QUALITY", "Ownership & Shareholding"],
  ["GOVERNANCE_AND_CAPITAL_ALLOCATION", "QUALITY", "Governance & Capital Allocation"],
  ["FORENSIC_AND_QUALITY_REVIEW", "QUALITY", "Forensic & Quality Review"],

  ["MARKET_AND_PRICE_ANALYSIS", "MARKET", "Market & Price Analysis"],
  ["TECHNICAL_ANALYSIS", "MARKET", "Technical Analysis"],

  ["VALUATION", "VALUATION", "Valuation"],
  ["MARKET_IMPLIED_EXPECTATIONS", "VALUATION", "Market-Implied Expectations"],

  ["MACRO_POLICY_REGULATION", "THESIS", "Macro / Policy / Regulation"],
  ["CATALYSTS", "THESIS", "Catalysts"],
  ["RISK_REGISTER", "THESIS", "Risk Register"],
  ["BULL_BASE_BEAR_SCENARIOS", "THESIS", "Bull / Base / Bear Scenarios"],
  ["VARIANT_PERCEPTION", "THESIS", "Variant Perception"],

  ["INDEPENDENT_CHALLENGER_REVIEW", "CHALLENGER", "Independent Challenger Review"],

  ["MONITORING_DASHBOARD", "LIMITATIONS", "Monitoring Items"],
  ["DATA_GAPS_AND_LIMITATIONS", "LIMITATIONS", "Data Gaps & Limitations"],

  ["REPORT_METADATA", "APPENDICES", "Report Metadata"],
  ["EVIDENCE_APPENDIX", "APPENDICES", "Evidence Appendix"],
  ["CALCULATION_APPENDIX", "APPENDICES", "Calculation Appendix"],
  ["MANAGEMENT_STATEMENT_APPENDIX", "APPENDICES", "Management Statement Appendix"],
  ["GUIDANCE_VS_ACTUAL_APPENDIX", "APPENDICES", "Guidance vs. Actual Appendix"],
  ["CHALLENGER_APPENDIX", "APPENDICES", "Challenger Appendix"],
  ["REPRODUCIBILITY_APPENDIX", "APPENDICES", "Reproducibility Appendix"],
];

const REGISTRY = new Map(
  REGISTRY_ROWS.map(([id, group, label], index) => [id, { group, label, order: index }]),
);

/**
 * @param {string} sectionId
 * @returns {{group: string, label: string, order: number}}
 */
export function describeSection(sectionId) {
  const known = REGISTRY.get(sectionId);
  if (known) return known;
  // Safe, deterministic fallback for an unknown future section identifier - never a
  // crash, never an invented business meaning.
  return { group: "OTHER", label: sectionId, order: Number.MAX_SAFE_INTEGER };
}

/** @returns {string[]} every canonical section ID this registry knows about. */
export function knownSectionIds() {
  return REGISTRY_ROWS.map((row) => row[0]);
}
