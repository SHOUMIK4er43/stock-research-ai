/**
 * DOM rendering for the report viewer (Part 6B-C). Built entirely on ./dom.js's `el()` -
 * every piece of report/document text is assigned via textContent, never innerHTML.
 * This module renders; it never recalculates, reinterprets, or invents anything not
 * already present in the canonical ResearchReportSnapshot payload.
 */
import { el, safeLink } from "./dom.js";
import { groupSections, groupDocumentCorpus, materialChallengedStatements } from "./view_model.js";
import {
  coverageLabel,
  humanizeStatus,
  epistemicLabel,
  challengeOutcomeLabel,
  quoteAttributionLabel,
  documentTypeLabel,
  documentCoverageLabel,
  completenessLabel,
  COMPLETENESS_EXPLANATION,
  valuationEvidenceLabel,
  monitoringDirectionLabel,
  MATERIAL_CHALLENGE_OUTCOMES,
} from "./labels.js";

function coverageBadge(doc, coverage) {
  const isAvailable = coverage.status === "AVAILABLE" || coverage.status === "PARTIAL";
  return el(doc, "span", {
    className: `badge badge-coverage ${isAvailable ? "badge-ok" : "badge-muted"}`,
    text: coverageLabel(coverage.status),
  });
}

function evidenceDrawerBody(doc, statement) {
  const rows = [];
  rows.push(el(doc, "p", { className: "muted", text: `Epistemic class: ${epistemicLabel(statement.epistemic_class)}` }));
  rows.push(el(doc, "p", { className: "muted", text: `Origin: ${statement.origin === "NARRATOR" ? "Narrator-assisted" : "Deterministic"}` }));
  if (statement.quote_attribution) {
    rows.push(el(doc, "p", { className: "muted", text: `Attribution: ${quoteAttributionLabel(statement.quote_attribution)}` }));
  }
  if (statement.challenge_outcome) {
    rows.push(el(doc, "p", { className: "muted", text: `Challenge outcome: ${challengeOutcomeLabel(statement.challenge_outcome)}` }));
  }
  if (statement.evidence_refs && statement.evidence_refs.length) {
    rows.push(el(doc, "p", { className: "muted", text: "Evidence references:" }));
    const list = el(doc, "ul", { className: "evidence-ref-list" });
    for (const ref of statement.evidence_refs) {
      const text = ref.item_key ? `${ref.kind} · ${ref.item_key}` : ref.kind;
      list.appendChild(el(doc, "li", { text }));
    }
    rows.push(list);
  }
  const idCounts = [
    ["Source claims", statement.source_claim_ids],
    ["Challenge findings", statement.challenge_finding_ids],
    ["Alternative interpretations", statement.alternative_interpretation_ids],
  ];
  for (const [label, ids] of idCounts) {
    if (ids && ids.length) {
      rows.push(el(doc, "p", { className: "muted", text: `${label}: ${ids.length} referenced` }));
    }
  }
  if (statement.limitations && statement.limitations.length) {
    rows.push(el(doc, "p", { className: "muted", text: "Limitations:" }));
    const list = el(doc, "ul", { className: "limitations-list" });
    for (const limitation of statement.limitations) list.appendChild(el(doc, "li", { text: limitation }));
    rows.push(list);
  }
  return el(doc, "div", { className: "evidence-drawer-body", children: rows });
}

export function renderStatement(doc, statement) {
  const badgeRow = el(doc, "div", { className: "badge-row" });
  badgeRow.appendChild(el(doc, "span", { className: "badge badge-epistemic", text: epistemicLabel(statement.epistemic_class) }));
  if (statement.challenge_outcome) {
    const material = MATERIAL_CHALLENGE_OUTCOMES.includes(statement.challenge_outcome);
    badgeRow.appendChild(
      el(doc, "span", {
        className: `badge badge-challenge${material ? " badge-material" : ""}`,
        text: (material ? "⚠ " : "") + challengeOutcomeLabel(statement.challenge_outcome),
      }),
    );
  }
  if (statement.quote_attribution) {
    badgeRow.appendChild(
      el(doc, "span", { className: "badge badge-attribution", text: quoteAttributionLabel(statement.quote_attribution) }),
    );
  }

  const details = el(doc, "details", { className: "evidence-drawer" });
  details.appendChild(el(doc, "summary", { text: "Why does the report say this?" }));
  details.appendChild(evidenceDrawerBody(doc, statement));

  return el(doc, "div", {
    className: `statement-card${
      statement.challenge_outcome && MATERIAL_CHALLENGE_OUTCOMES.includes(statement.challenge_outcome)
        ? " statement-card-material"
        : ""
    }`,
    children: [el(doc, "p", { className: "statement-text", text: statement.text }), badgeRow, details],
  });
}

function cellText(metric) {
  if (metric.display_value !== null && metric.display_value !== undefined && metric.display_value !== "") {
    return metric.unit ? `${metric.display_value} ${metric.unit}` : metric.display_value;
  }
  return metric.label;
}

export function renderTable(doc, table) {
  const head = el(doc, "tr", {
    children: table.columns.map((column) => el(doc, "th", { text: column, attrs: { scope: "col" } })),
  });
  const body = table.rows.map((row) =>
    el(doc, "tr", {
      children: row.map((metric) => {
        const nonAvailable = metric.status && metric.status !== "AVAILABLE";
        return el(doc, "td", {
          className: nonAvailable ? "cell-non-available" : undefined,
          text: cellText(metric),
          attrs: nonAvailable ? { title: humanizeStatus(metric.status) } : undefined,
        });
      }),
    }),
  );
  return el(doc, "div", {
    className: "table-scroll",
    children: [
      el(doc, "table", {
        className: "report-table",
        children: [
          el(doc, "caption", { text: table.title }),
          el(doc, "thead", { children: [head] }),
          el(doc, "tbody", { children: body }),
        ],
      }),
    ],
  });
}

export function renderSection(doc, entry) {
  const { section, label } = entry;
  const header = el(doc, "div", {
    className: "section-header",
    children: [el(doc, "h3", { text: label }), coverageBadge(doc, section.coverage)],
  });
  const reason = el(doc, "p", { className: "muted section-reason", text: section.coverage.reason });

  const children = [header, reason];
  const hasContent =
    (section.tables && section.tables.length) || (section.statements && section.statements.length);

  if (hasContent) {
    for (const table of section.tables || []) children.push(renderTable(doc, table));
    for (const statement of section.statements || []) children.push(renderStatement(doc, statement));
  }

  return el(doc, "section", { className: "report-section", attrs: { id: `section-${section.section}` }, children });
}

export function renderExecutiveSnapshot(doc, snapshot) {
  const children = [];
  children.push(
    el(doc, "div", {
      className: "badge-row",
      children: [
        el(doc, "span", { className: "badge badge-completeness", text: `Completeness: ${completenessLabel(snapshot.research_completeness)}` }),
        el(doc, "span", { className: "badge badge-valuation-state", text: `Valuation evidence: ${valuationEvidenceLabel(snapshot.valuation_evidence_state)}` }),
      ],
    }),
  );
  children.push(el(doc, "p", { className: "muted small", text: COMPLETENESS_EXPLANATION }));

  const blocks = [
    ["Company overview", snapshot.company_overview],
    ["Key financial trend", snapshot.key_financial_trend],
    ["Key sector trend", snapshot.key_sector_trend],
    ["Supporting evidence", snapshot.supporting_evidence],
    ["Document highlights", snapshot.document_highlights],
    ["Valuation evidence", snapshot.valuation_evidence],
    ["Market-implied expectations", snapshot.market_implied_expectations],
    ["Catalysts", snapshot.catalysts],
    ["Risks", snapshot.risks],
  ];
  for (const [heading, statements] of blocks) {
    if (!statements || statements.length === 0) continue;
    children.push(el(doc, "h4", { text: heading }));
    for (const statement of statements) children.push(renderStatement(doc, statement));
  }

  if (snapshot.disconfirming_evidence && snapshot.disconfirming_evidence.length) {
    children.push(el(doc, "h4", { text: "Disconfirming evidence" }));
    const list = el(doc, "ul", {
      children: snapshot.disconfirming_evidence.map((ref) =>
        el(doc, "li", { text: ref.item_key ? `${ref.kind} · ${ref.item_key}` : ref.kind }),
      ),
    });
    children.push(list);
  }

  if (snapshot.material_challenger_finding_ids && snapshot.material_challenger_finding_ids.length) {
    children.push(
      el(doc, "p", {
        className: "muted",
        text: `Material challenger findings: ${snapshot.material_challenger_finding_ids.length} referenced`,
      }),
    );
  }

  if (snapshot.unresolved_questions && snapshot.unresolved_questions.length) {
    children.push(el(doc, "h4", { text: "Unresolved questions" }));
    children.push(
      el(doc, "ul", { children: snapshot.unresolved_questions.map((q) => el(doc, "li", { text: q })) }),
    );
  }

  if (snapshot.major_data_gaps && snapshot.major_data_gaps.length) {
    children.push(el(doc, "h4", { text: "Major data gaps" }));
    for (const gap of snapshot.major_data_gaps) {
      children.push(el(doc, "p", { className: "muted", text: gap.message }));
    }
  }

  if (snapshot.narrative) {
    children.push(el(doc, "h4", { text: "Narrative" }));
    children.push(el(doc, "p", { text: snapshot.narrative }));
  }

  return el(doc, "section", { className: "report-section executive-snapshot", children });
}

export function renderSynthesis(doc, synthesis) {
  const children = [
    el(doc, "div", {
      className: "badge-row",
      children: [
        el(doc, "span", { className: "badge badge-completeness", text: `Completeness: ${completenessLabel(synthesis.research_completeness)}` }),
        el(doc, "span", { className: "badge badge-valuation-state", text: `Valuation evidence: ${valuationEvidenceLabel(synthesis.valuation_evidence_state)}` }),
      ],
    }),
  ];
  const counters = [
    ["Supported claims", synthesis.supporting_claim_ids],
    ["Challenged claims", synthesis.challenged_claim_ids],
    ["Invalid claims", synthesis.invalid_claim_ids],
    ["Unverifiable claims", synthesis.unverifiable_claim_ids],
    ["Material disconfirming evidence", synthesis.material_disconfirming_evidence],
    ["Material challenger findings", synthesis.material_challenger_finding_ids],
  ];
  const countRow = el(doc, "div", { className: "badge-row" });
  for (const [label, ids] of counters) {
    if (ids && ids.length) {
      countRow.appendChild(el(doc, "span", { className: "badge badge-muted", text: `${label}: ${ids.length}` }));
    }
  }
  children.push(countRow);
  if (synthesis.evidence_limitations && synthesis.evidence_limitations.length) {
    children.push(el(doc, "h4", { text: "Evidence limitations" }));
    children.push(renderDataGaps(doc, synthesis.evidence_limitations));
  }
  if (synthesis.unresolved_questions && synthesis.unresolved_questions.length) {
    children.push(el(doc, "h4", { text: "Unresolved questions" }));
    children.push(
      el(doc, "ul", { children: synthesis.unresolved_questions.map((q) => el(doc, "li", { text: q })) }),
    );
  }
  if (synthesis.narrative) {
    children.push(el(doc, "p", { text: synthesis.narrative }));
  }
  return el(doc, "section", { className: "report-section", children });
}

export function renderDocumentCorpus(doc, report) {
  const corpus = report.document_corpus;
  const children = [
    el(doc, "p", {
      className: "muted",
      text: "Documents in this research run (not a claim of complete filing history).",
    }),
  ];
  const groups = groupDocumentCorpus(report);
  if (groups.length === 0) {
    children.push(el(doc, "p", { className: "muted", text: "No documents were pinned into this research run." }));
  }
  for (const group of groups) {
    children.push(el(doc, "h4", { text: documentTypeLabel(group.documentType) }));
    const list = el(doc, "ul", { className: "document-list" });
    for (const entry of group.entries) {
      const parts = [documentCoverageLabel(entry.coverage_status)];
      if (entry.fiscal_label) parts.push(entry.fiscal_label);
      if (entry.publication_date) parts.push(entry.publication_date);
      list.appendChild(el(doc, "li", { text: parts.join(" · ") }));
    }
    children.push(list);
  }
  return el(doc, "section", { className: "report-section", children });
}

export function renderMonitoring(doc, entries) {
  if (!entries || entries.length === 0) {
    return el(doc, "p", { className: "muted", text: "No monitoring items were retained for this research run." });
  }
  const list = el(doc, "ul", { className: "monitoring-list" });
  for (const entry of entries) {
    const text = `${entry.metric}: ${entry.current_value ?? "—"} (${monitoringDirectionLabel(entry.direction)}${
      entry.prior_value ? `, was ${entry.prior_value}` : ""
    })`;
    list.appendChild(el(doc, "li", { text }));
  }
  return el(doc, "div", {
    children: [el(doc, "p", { className: "muted", text: "Monitoring items (static, not live alerts)." }), list],
  });
}

export function renderDataGaps(doc, limitations) {
  if (!limitations || limitations.length === 0) {
    return el(doc, "p", { className: "muted", text: "No data gaps were retained for this research run." });
  }
  const list = el(doc, "ul", { className: "limitations-list" });
  for (const limitation of limitations) {
    list.appendChild(el(doc, "li", { text: limitation.message }));
  }
  return list;
}

/** Grouped, ordered navigation - never a flat 42-item list. */
export function renderNav(doc, groups, activeSectionId, onSelect) {
  const nav = el(doc, "nav", { className: "report-nav", attrs: { "aria-label": "Report sections" } });
  for (const group of groups) {
    const groupEl = el(doc, "div", { className: "report-nav-group" });
    groupEl.appendChild(el(doc, "p", { className: "report-nav-group-label", text: group.groupLabel }));
    for (const item of group.sections) {
      const button = el(doc, "button", {
        className: `report-nav-item${item.id === activeSectionId ? " active" : ""}`,
        text: item.label,
        attrs: { type: "button" },
      });
      button.addEventListener("click", () => onSelect(item.id));
      groupEl.appendChild(button);
    }
    nav.appendChild(groupEl);
  }
  return nav;
}

export { groupSections, materialChallengedStatements, safeLink };
