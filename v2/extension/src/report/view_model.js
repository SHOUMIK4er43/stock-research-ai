/**
 * Pure transforms from a canonical ResearchReportSnapshot (Part 6A JSON, unmodified) to
 * render-friendly structures. No DOM here, no recalculation, no invented business
 * semantics - every function only reshapes/groups already-present fields.
 */
import { describeSection, GROUPS, GROUP_LABELS } from "./section_registry.js";
import { MATERIAL_CHALLENGE_OUTCOMES } from "./labels.js";

/**
 * @param {import('./types.js').ResearchReportSnapshot} report
 * @returns {{group: string, groupLabel: string, sections: {id: string, label: string, section: object}[]}[]}
 */
export function groupSections(report) {
  const sections = report.sections || [];
  const described = sections.map((section) => ({
    section,
    ...describeSection(section.section),
  }));
  const byGroup = new Map();
  for (const entry of described) {
    if (!byGroup.has(entry.group)) byGroup.set(entry.group, []);
    byGroup.get(entry.group).push(entry);
  }
  const result = [];
  for (const group of GROUPS) {
    const entries = byGroup.get(group);
    if (!entries || entries.length === 0) continue;
    entries.sort((a, b) => a.order - b.order);
    result.push({
      group,
      groupLabel: GROUP_LABELS[group] ?? group,
      sections: entries.map((entry) => ({
        id: entry.section.section,
        label: entry.label,
        section: entry.section,
      })),
    });
  }
  return result;
}

/** @param {object} section a ReportSection */
export function isSectionAvailable(section) {
  return section.coverage && section.coverage.status === "AVAILABLE";
}

/** @param {object} section a ReportSection */
export function hasSectionContent(section) {
  return (
    (section.statements && section.statements.length > 0) ||
    (section.tables && section.tables.length > 0)
  );
}

/**
 * @param {import('./types.js').ResearchReportSnapshot} report
 * @returns {{documentType: string, entries: object[]}[]}
 */
export function groupDocumentCorpus(report) {
  const entries = (report.document_corpus && report.document_corpus.entries) || [];
  const byType = new Map();
  for (const entry of entries) {
    const key = entry.document_type;
    if (!byType.has(key)) byType.set(key, []);
    byType.get(key).push(entry);
  }
  return Array.from(byType.entries()).map(([documentType, docEntries]) => ({
    documentType,
    entries: docEntries,
  }));
}

/**
 * Every statement across every section whose challenge_outcome is CHALLENGED or
 * INVALID_AGAINST_BUNDLE - collected once so the viewer can surface them prominently
 * without the browser reconciling or reinterpreting anything.
 * @param {import('./types.js').ResearchReportSnapshot} report
 */
export function materialChallengedStatements(report) {
  const sections = report.sections || [];
  const result = [];
  for (const section of sections) {
    for (const statement of section.statements || []) {
      if (MATERIAL_CHALLENGE_OUTCOMES.includes(statement.challenge_outcome)) {
        result.push({ sectionId: section.section, statement });
      }
    }
  }
  return result;
}

/** @param {import('./types.js').ResearchReportSnapshot} report @param {string} sectionId */
export function findSection(report, sectionId) {
  return (report.sections || []).find((section) => section.section === sectionId) || null;
}
