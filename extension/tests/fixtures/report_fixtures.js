/**
 * Realistic ResearchReportSnapshot-shaped fixtures for Part 6B-C viewer tests, built
 * directly against the REAL frozen backend schema (src/stock_research_ai/domain/report.py,
 * analysis.py, challenge.py) - never an invented alternate shape. Field names, coverage
 * statuses, epistemic classes, and challenge outcomes are copied verbatim from those
 * Literal definitions.
 */

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);

/** The exact, closed EvidenceKind Literal (domain/analysis.py) - every `kind` value used
 * below must be one of these 13. A prior draft of this fixture invented plausible-looking
 * but nonexistent kinds (e.g. "FINANCIAL_STATEMENT_LINE_ITEM", "CONCALL_TRANSCRIPT_CHUNK")
 * that the real backend never emits; this list exists so a test can catch that class of
 * schema-fidelity drift going forward. */
export const REAL_EVIDENCE_KINDS = Object.freeze([
  "COMPANY",
  "FINANCIAL",
  "SHAREHOLDING",
  "MARKET",
  "FINANCIAL_CALCULATION",
  "SECTOR_KPI",
  "PATTERN",
  "MARKET_CALCULATION",
  "TECHNICAL",
  "VALUATION",
  "DOCUMENT_CHUNK",
  "PUBLICATION",
  "MACRO",
]);

function statement(overrides = {}) {
  return {
    statement_id: "11111111-1111-1111-1111-111111111111",
    section: "BUSINESS_MODEL",
    text: "The company generates revenue primarily from retail lending.",
    epistemic_class: "FACT",
    origin: "DETERMINISTIC",
    source_claim_ids: ["22222222-2222-2222-2222-222222222222"],
    challenge_finding_ids: [],
    alternative_interpretation_ids: [],
    evidence_refs: [
      {
        kind: "FINANCIAL",
        owning_snapshot_id: "33333333-3333-3333-3333-333333333333",
        item_key: "revenue_fy24",
        snapshot_payload_sha256: SHA_A,
        raw_artifact_sha256: SHA_B,
      },
    ],
    challenge_outcome: null,
    limitations: [],
    quote_attribution: null,
    ...overrides,
  };
}

function metric(overrides = {}) {
  return {
    label: "Revenue",
    display_value: "12,340",
    unit: "INR cr",
    period: "FY24",
    reporting_scope: "CONSOLIDATED",
    status: "AVAILABLE",
    evidence_ref: null,
    issues: [],
    ...overrides,
  };
}

function coverage(overrides = {}) {
  return {
    status: "AVAILABLE",
    reason: "Sufficient evidence was available for this section.",
    reason_code: null,
    contributing_evidence_kinds: [],
    ...overrides,
  };
}

/** A fully-populated, realistic report exercising the widest variety of statuses. */
export function buildFullReport() {
  const financialsSection = {
    section: "ANNUAL_FINANCIAL_PERFORMANCE",
    coverage: coverage(),
    tables: [
      {
        table_id: "annual-financials",
        title: "Annual Financial Performance",
        section: "ANNUAL_FINANCIAL_PERFORMANCE",
        columns: ["Metric", "FY24", "FY23"],
        rows: [
          [
            metric({ label: "Revenue", display_value: "12,340", unit: "INR cr", period: "FY24" }),
            metric({ label: "Revenue", display_value: "10,980", unit: "INR cr", period: "FY23" }),
            metric({ label: "Revenue", display_value: null, status: "INSUFFICIENT_INPUT", period: "FY22" }),
          ],
        ],
      },
    ],
    statements: [
      statement({
        statement_id: "44444444-4444-4444-4444-444444444444",
        section: "ANNUAL_FINANCIAL_PERFORMANCE",
        text: "Revenue grew year-over-year, driven by loan book expansion.",
        epistemic_class: "CALCULATION",
      }),
    ],
  };

  const businessSection = {
    section: "BUSINESS_MODEL",
    coverage: coverage(),
    tables: [],
    statements: [
      statement({ text: "<script>alert(1)</script> is retained verbatim from a source filing." }),
      statement({
        statement_id: "55555555-5555-5555-5555-555555555555",
        text: "Management stated the branch network will double within three years.",
        epistemic_class: "INTERPRETATION",
        origin: "NARRATOR",
        evidence_refs: [
          {
            kind: "DOCUMENT_CHUNK",
            owning_snapshot_id: "33333333-3333-3333-3333-333333333333",
            item_key: "concall_q4fy24_chunk_12",
            snapshot_payload_sha256: SHA_A,
            raw_artifact_sha256: SHA_B,
          },
        ],
        quote_attribution: "TRANSCRIPT_STATEMENT",
      }),
      statement({
        statement_id: "66666666-6666-6666-6666-666666666666",
        text: "Loan growth is assumed to continue at the trailing five-year average rate.",
        epistemic_class: "ASSUMPTION",
      }),
      statement({
        statement_id: "77777777-7777-7777-7777-777777777777",
        text: "In a bear scenario, credit costs could rise materially above guidance.",
        epistemic_class: "SCENARIO",
      }),
      statement({
        statement_id: "88888888-8888-8888-8888-888888888888",
        text: "The company claims zero related-party transactions in FY24.",
        challenge_outcome: "CHALLENGED",
        challenge_finding_ids: ["99999999-9999-9999-9999-999999999999"],
      }),
      statement({
        statement_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        text: "The company reported a debt-free balance sheet as of FY24.",
        challenge_outcome: "INVALID_AGAINST_BUNDLE",
        challenge_finding_ids: ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"],
      }),
    ],
  };

  const valuationSection = {
    section: "VALUATION",
    coverage: coverage(),
    tables: [
      {
        table_id: "valuation-multiples",
        title: "Valuation",
        section: "VALUATION",
        columns: ["Metric", "Value"],
        rows: [
          [
            metric({ label: "Price / Book", display_value: null, status: "UNVERIFIED_METRIC_SEMANTICS" }),
          ],
        ],
      },
    ],
    statements: [],
  };

  const sectorKpiSection = {
    section: "SECTOR_SPECIFIC_KPI_ANALYSIS",
    coverage: coverage({ status: "NOT_APPLICABLE", reason: "This company's sector defines no bank-specific KPIs." }),
    tables: [],
    statements: [],
  };

  const guidanceSection = {
    section: "MANAGEMENT_GUIDANCE_AND_COMMITMENTS",
    coverage: coverage({
      status: "UNAVAILABLE",
      reason: "No structured guidance was retained for this research run.",
      reason_code: "REQUIRED_STRUCTURED_GUIDANCE_NOT_RETAINED",
    }),
    tables: [],
    statements: [],
  };

  const unknownFutureSection = {
    section: "SOME_FUTURE_SECTION_NOT_YET_KNOWN",
    coverage: coverage(),
    tables: [],
    statements: [
      statement({ section: "SOME_FUTURE_SECTION_NOT_YET_KNOWN", text: "A statement in an unknown future section." }),
    ],
  };

  return {
    schema_version: "6A.1",
    id: "c0000000-0000-0000-0000-000000000001",
    company_id: "c0000000-0000-0000-0000-000000000002",
    analysis_snapshot_id: "c0000000-0000-0000-0000-000000000003",
    analysis_snapshot_sha256: SHA_A,
    challenge_snapshot_id: "c0000000-0000-0000-0000-000000000004",
    challenge_snapshot_sha256: SHA_B,
    sections: [financialsSection, businessSection, valuationSection, sectorKpiSection, guidanceSection, unknownFutureSection],
    executive_snapshot: {
      company_overview: [statement({ text: "A mid-sized retail-focused lender." })],
      key_financial_trend: [statement({ text: "Revenue has grown for four consecutive years." })],
      key_sector_trend: [],
      supporting_evidence: [statement({ text: "Loan book growth is corroborated across two independent filings." })],
      disconfirming_evidence: [
        {
          kind: "FINANCIAL",
          owning_snapshot_id: "33333333-3333-3333-3333-333333333333",
          item_key: "npa_ratio_fy24",
          snapshot_payload_sha256: SHA_A,
          raw_artifact_sha256: SHA_B,
        },
      ],
      document_highlights: [],
      valuation_evidence: [],
      market_implied_expectations: [],
      catalysts: [],
      risks: [],
      material_challenger_finding_ids: ["99999999-9999-9999-9999-999999999999"],
      unresolved_questions: ["Why did related-party disclosures change between FY23 and FY24?"],
      major_data_gaps: [
        { code: "NO_SEGMENT_DATA", message: "Segment-level profitability was not disclosed.", section: null, evidence_refs: [] },
      ],
      research_completeness: "MODERATE",
      valuation_evidence_state: "SCENARIO_DEPENDENT",
      narrative: null,
    },
    synthesis: {
      research_completeness: "MODERATE",
      valuation_evidence_state: "SCENARIO_DEPENDENT",
      supporting_claim_ids: ["22222222-2222-2222-2222-222222222222"],
      challenged_claim_ids: ["88888888-8888-8888-8888-888888888888"],
      invalid_claim_ids: ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
      unverifiable_claim_ids: [],
      material_disconfirming_evidence: [],
      material_challenger_finding_ids: ["99999999-9999-9999-9999-999999999999"],
      evidence_limitations: [
        { code: "NO_SEGMENT_DATA", message: "Segment-level profitability was not disclosed.", section: null, evidence_refs: [] },
      ],
      unresolved_questions: ["Why did related-party disclosures change between FY23 and FY24?"],
      narrative: null,
    },
    document_corpus: {
      corpus_scope: "PINNED_REPORT_INPUT_ONLY",
      entries: [
        {
          document_type: "annual_report",
          document_id: "d0000000-0000-0000-0000-000000000001",
          version_id: "d0000000-0000-0000-0000-000000000002",
          fiscal_label: "FY24",
          publication_date: "2024-05-15",
          coverage_status: "COMPLETE",
          evidence_ref: {
            kind: "PUBLICATION",
            owning_snapshot_id: "33333333-3333-3333-3333-333333333333",
            item_key: "annual_report_fy24",
            snapshot_payload_sha256: SHA_A,
            raw_artifact_sha256: SHA_B,
          },
        },
        {
          document_type: "concall_transcript",
          document_id: "d0000000-0000-0000-0000-000000000003",
          version_id: "d0000000-0000-0000-0000-000000000004",
          fiscal_label: "Q4FY24",
          publication_date: "2024-05-20",
          coverage_status: "PARTIAL",
          evidence_ref: {
            kind: "DOCUMENT_CHUNK",
            owning_snapshot_id: "33333333-3333-3333-3333-333333333333",
            item_key: "concall_q4fy24",
            snapshot_payload_sha256: SHA_A,
            raw_artifact_sha256: SHA_B,
          },
        },
      ],
    },
    monitoring: [
      {
        metric: "Gross NPA ratio",
        current_value: "1.8%",
        prior_value: "2.1%",
        direction: "DOWN",
        source: {
          kind: "FINANCIAL",
          owning_snapshot_id: "33333333-3333-3333-3333-333333333333",
          item_key: "gross_npa_fy24",
          snapshot_payload_sha256: SHA_A,
          raw_artifact_sha256: SHA_B,
        },
        period: "FY24",
        coverage: "AVAILABLE",
        monitoring_rationale: "Asset quality trend is a key underwriting signal for this lender.",
      },
    ],
    limitations: [
      { code: "NO_SEGMENT_DATA", message: "Segment-level profitability was not disclosed.", section: null, evidence_refs: [] },
    ],
    narrator_metadata: null,
    rejected_narrative_blocks: [],
    artifact_manifest: { entries: [] },
    reproducibility: {
      analysis_snapshot_id: "c0000000-0000-0000-0000-000000000003",
      analysis_snapshot_sha256: SHA_A,
      challenge_snapshot_id: "c0000000-0000-0000-0000-000000000004",
      challenge_snapshot_sha256: SHA_B,
      relevance_registry_version: "1.0.0",
      narrator_metadata: null,
    },
    validation_issues: [],
    evaluated_at: "2024-06-01T00:00:00Z",
  };
}

/** A minimal report with no documents and no monitoring - exercises empty-state rendering. */
export function buildEmptyCorpusReport() {
  const report = buildFullReport();
  return {
    ...report,
    document_corpus: { corpus_scope: "PINNED_REPORT_INPUT_ONLY", entries: [] },
    monitoring: [],
    limitations: [],
  };
}
