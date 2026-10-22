"""DETAILED_RESEARCH_V1 Box 13: source-backed governance evidence (no score, no opinion).

1. auditor_reports_from_bundle: the statutory auditor's report printed in an issuer-VERIFIED
   annual report (Box 3) becomes the AuditorReport input the registered AUDITOR_SIGNAL rule
   already grades. Only the report's own structure is read: the opinion heading (Qualified /
   Adverse / Disclaimer of Opinion, else the unmodified 'true and fair view' opinion), an
   'Emphasis of Matter' heading and a 'Material Uncertainty Related to Going Concern' heading.
   Key audit matters never become a flag. Every report cites the Box 3 evidence items on its
   pages (Box 13 refuses an evidence ID the bundle does not hold); no evidence, no report.

2. governance_topics: the governance evidence ledger. Each topic reports what the REVIEWED
   sources show (EVIDENCE_FOUND / NO_ADVERSE_EVIDENCE_IN_REVIEWED_SOURCE / SOURCE_NOT_AVAILABLE /
   NOT_EVALUABLE / NOT_APPLICABLE) and the registered rules' outcome (FLAGGED / EVALUATED_NO_FLAG
   / NOT_EVALUABLE / NOT_APPLICABLE). Absence of a red flag is never strong governance: 'no
   adverse evidence' requires every applicable rule of the topic to have run on cited inputs.
   A located disclosure (related parties, remuneration, the corporate-governance report) is
   recorded as a fact without a risk conclusion when no registered rule can grade it.
"""

import re
from collections.abc import Iterable, Sequence
from uuid import UUID

from stock_research_ai.domain.document_intelligence import (
    EvidenceBundle,
    IssuerBindingStatus,
    SectionKind,
)
from stock_research_ai.domain.due_diligence import (
    AuditOpinion,
    AuditorReport,
    DiligenceSource,
    DueDiligenceDataset,
    GovernanceEvidenceStatus,
    GovernanceRiskStatus,
    GovernanceTopic,
    RiskSignal,
    Severity,
    SignalStatus,
)
from stock_research_ai.domain.due_diligence import SourceAuthority as InputAuthority
from stock_research_ai.domain.evidence import EvidenceDocumentType, SourceAuthority

E, R = GovernanceEvidenceStatus, GovernanceRiskStatus

# --- 1. the statutory auditor's report ---------------------------------------------------------

_SPACE = re.compile(r"[ \t ]+")
_START = re.compile(r"report on the audit of the (standalone|consolidated) financial statements",
                    re.I)
_TITLE = re.compile(r"independent auditor.?s report", re.I)


def _heading(pattern: str) -> re.Pattern[str]:
    """A report heading: at the start of a line, optionally numbered ('3. Emphasis of Matter')."""
    return re.compile(rf"(?im)^\s*(?:[0-9]+[.)]\s*|[A-Z][.)]\s*)?(?:{pattern})\s*:?\s*$")


QUALIFIED = _heading(r"(?:basis for )?qualified opinion")
ADVERSE = _heading(r"(?:basis for )?adverse opinion")
DISCLAIMER = _heading(r"(?:basis for )?disclaimer of opinion")
EMPHASIS = _heading(r"emphasis of matters?")
GOING_CONCERN = _heading(r"material uncertainty related to going concern")
UNMODIFIED = re.compile(r"in our opinion.{0,600}?give[s]? a true and fair view", re.I | re.S)
OPINION = _heading(r"opinion")
_YEAR = re.compile(r"31\s*(?:st)?\s*march,?\s*(20\d{2})|march\s*31,?\s*(20\d{2})", re.I)
_FIRM = re.compile(r"\bfor\s+(?:m/s\.?\s+)?([A-Z][A-Za-z0-9&.,'()\- ]{2,80}?)\s*,?\s*\n?\s*"
                   r"chartered accountants\b", re.I)
#: only an exchange filing or the company's own verified document states its audit report
AUTHORITY = {SourceAuthority.PRIMARY_REGULATORY: InputAuthority.EXCHANGE_FILING,
             SourceAuthority.PRIMARY_COMPANY: InputAuthority.COMPANY_DOCUMENT}
WINDOW = 3  # opinion, basis, emphasis and going-concern headings sit on the first pages
SIGN_OFF = 14  # the firm signs at the end of the report


def _text(raw: str) -> str:
    return "\n".join(_SPACE.sub(" ", line).strip() for line in raw.splitlines())


def auditor_reports_from_bundle(bundle: EvidenceBundle | None) -> tuple[AuditorReport, ...]:
    """One AuditorReport per (fiscal year, basis) printed in a verified annual report."""
    out: dict[tuple[int, str], AuditorReport] = {}
    if bundle is None:
        return ()
    for x in bundle.extractions:
        authority = AUTHORITY.get(x.authority)
        if x.document_type != EvidenceDocumentType.ANNUAL_REPORT or authority is None or (
                x.issuer_binding_status != IssuerBindingStatus.VERIFIED):
            continue  # an unverified issuer's / unofficial copy is never this company's audit
        pages = {p.page_number: _text(p.raw_text) for p in x.pages}
        for number in sorted(pages):
            head = pages[number]
            start = _START.search(head)
            if start is None or not _TITLE.search(head) or not any(
                    h.search(head) for h in (OPINION, QUALIFIED, ADVERSE, DISCLAIMER)):
                continue  # a contents page names the report but carries no opinion
            basis = start.group(1).upper()
            body = "\n".join(pages.get(n, "") for n in range(number, number + WINDOW))
            year = _YEAR.search(body)
            if year is None:
                continue
            fiscal_year = int(year.group(1) or year.group(2))
            if QUALIFIED.search(body):
                opinion = AuditOpinion.QUALIFIED
            elif ADVERSE.search(body):
                opinion = AuditOpinion.ADVERSE
            elif DISCLAIMER.search(body):
                opinion = AuditOpinion.DISCLAIMER
            elif UNMODIFIED.search(body):
                opinion = AuditOpinion.UNMODIFIED
            else:
                continue  # no opinion read: no report, never an assumed clean one
            evidence = tuple(
                i.evidence_id for i in x.evidence
                if number <= i.page_start < number + WINDOW and (
                    i.section_kind == SectionKind.AUDITOR_REPORT
                    or i.kind.value == "AUDITOR_DISCLOSURE"))[:50]
            if not evidence:
                continue  # no provenance, no conclusion
            tail = "\n".join(pages.get(n, "") for n in range(number, number + SIGN_OFF))
            firm = _FIRM.search(tail)
            report = AuditorReport(
                fiscal_year=fiscal_year, basis=basis,  # type: ignore[arg-type]
                auditor=(firm.group(1).strip(" ,") if firm else "not identified in the "
                         "extracted report text")[:200],
                opinion=opinion,
                emphasis_of_matter=bool(EMPHASIS.search(body)),
                going_concern_uncertainty=bool(GOING_CONCERN.search(body)),
                source=DiligenceSource(
                    authority=authority,
                    reference=(f"Annual report: independent auditor's report on the "
                               f"{basis.lower()} financial statements, p.{number} (read by Box "
                               "13 from Box 3 evidence)"),
                    document_id=x.document_id, page=number, evidence_ids=evidence))
            out.setdefault((fiscal_year, basis), report)
    return tuple(out[k] for k in sorted(out))


def merge_auditor_reports(explicit: Sequence[AuditorReport],
                          derived: Iterable[AuditorReport]) -> tuple[AuditorReport, ...]:
    """Cited inputs keep precedence: a derived report fills only a (year, basis) not cited."""
    cited = {(r.fiscal_year, r.basis) for r in explicit}
    return (*explicit, *(r for r in derived if (r.fiscal_year, r.basis) not in cited))


# --- 2. the governance evidence ledger ---------------------------------------------------------

#: topic -> (title, material, registered rules, Box 3 sections that are factual disclosures)
TOPICS: tuple[tuple[str, str, bool, tuple[str, ...], tuple[SectionKind, ...]], ...] = (
    ("AUDITOR_OPINION", "Statutory audit opinion", True, ("AUDITOR_SIGNAL",), ()),
    ("RELATED_PARTY", "Related-party transactions", True,
     ("RELATED_PARTY_DEPENDENCE", "RELATED_PARTY_LOANS"), (SectionKind.RELATED_PARTY,)),
    ("PROMOTER_PLEDGE", "Promoter pledge / encumbrance", True, ("PROMOTER_PLEDGE",), ()),
    ("MANAGEMENT_CHANGE", "CEO / CFO / auditor turnover", True, ("MANAGEMENT_CHURN",), ()),
    ("BOARD_INDEPENDENCE", "Board independence", True, ("BOARD_INDEPENDENCE",),
     (SectionKind.CORPORATE_GOVERNANCE,)),
    ("REGULATORY_ACTION", "Regulatory / exchange action", True,
     ("EXCHANGE_PENALTIES", "MATERIAL_REGULATORY_ACTION"), ()),
    ("PROMOTER_OWNERSHIP", "Promoter ownership changes", False, ("PROMOTER_SELLING",), ()),
    ("LITIGATION", "Material adverse litigation", False, ("MATERIAL_LITIGATION",), ()),
    ("CAPITAL_ISSUANCE", "Capital issuance / dilution", False,
     ("EQUITY_DILUTION", "CHEAP_INSIDER_ALLOTMENT"), ()),
    ("EXECUTIVE_PAY", "Executive remuneration", False, ("EXECUTIVE_PAY_VS_PROFIT",),
     (SectionKind.REMUNERATION,)),
)
EVALUATED = (SignalStatus.EVALUATED, SignalStatus.INFORMATIONAL)
NOT_APPLICABLE = (SignalStatus.NOT_APPLICABLE, SignalStatus.UNSUPPORTED_SECTOR_RULE)
_ABSENT = re.compile(r"NOT_SUPPLIED|^NO_")


def _flagged(s: RiskSignal) -> bool:
    return s.status == SignalStatus.EVALUATED and s.severity in (Severity.YELLOW, Severity.RED)


def governance_topics(
    signals: Sequence[RiskSignal], bundle: EvidenceBundle | None,
    auditors: Sequence[AuditorReport] = (), capital_facts: Sequence[tuple[str, UUID]] = (),
) -> tuple[GovernanceTopic, ...]:
    """The ledger, from the registered rules' signals and the located Box 3 disclosures.
    `capital_facts`: (statement, Box 4 record ID) factual capital events (never a flag)."""
    by_code: dict[str, list[RiskSignal]] = {}
    for s in signals:
        by_code.setdefault(s.code, []).append(s)
    disclosures: dict[SectionKind, list[tuple[UUID, int, str]]] = {}
    for x in bundle.extractions if bundle is not None else ():
        if x.issuer_binding_status != IssuerBindingStatus.VERIFIED:
            continue
        for i in x.evidence:
            if i.section_kind is not None:
                disclosures.setdefault(i.section_kind, []).append(
                    (i.evidence_id, i.page_start, f"{x.artifact_reference}#{i.locator}"))
    out: list[GovernanceTopic] = []
    for topic, title, material, codes, sections in TOPICS:
        rules = [s for c in codes for s in by_code.get(c, [])]
        applicable = [s for s in rules if s.status not in NOT_APPLICABLE]
        flagged = [s for s in applicable if _flagged(s)]
        evaluated = [s for s in applicable if s.status in EVALUATED]
        located = [d for k in sections for d in disclosures.get(k, [])]
        facts = list(capital_facts) if topic == "CAPITAL_ISSUANCE" else []
        evidence = tuple(dict.fromkeys(e for s in (*evaluated, *flagged) for e in s.evidence_ids))
        records = tuple(dict.fromkeys([*(r for s in (*evaluated, *flagged) for r in s.record_ids),
                                       *(rid for _t, rid in facts)]))
        sources = tuple(dict.fromkeys(src for s in (*evaluated, *flagged) for src in s.source_ids))
        notes = [f"{s.code}: {s.explanation}" for s in (flagged or evaluated)]
        if topic == "AUDITOR_OPINION" and auditors:
            latest = max(r.fiscal_year for r in auditors)
            notes.insert(0, "; ".join(
                f"FY{r.fiscal_year} {r.basis.lower()}: {r.opinion.value.lower()} opinion, "
                f"auditor {r.auditor}" for r in auditors if r.fiscal_year == latest))
        notes += [statement for statement, _rid in facts]
        missing = [s for s in applicable if s.status not in EVALUATED]
        reason = missing[0].reason if missing else None
        if rules and not applicable and not located:
            out.append(GovernanceTopic(
                topic=topic, title=title, material=material,
                evidence_status=E.NOT_APPLICABLE, risk_status=R.NOT_APPLICABLE,
                finding="the registered rules do not apply to this company",
                signal_ids=tuple(s.risk_id for s in rules)))
            continue
        if flagged:
            evidence_status, risk_status = E.EVIDENCE_FOUND, R.FLAGGED
        elif applicable and not missing:
            evidence_status, risk_status = (E.NO_ADVERSE_EVIDENCE_IN_REVIEWED_SOURCE,
                                            R.EVALUATED_NO_FLAG)
        elif located or facts:
            evidence_status, risk_status = E.EVIDENCE_FOUND, R.NOT_EVALUABLE
            pages = sorted({p for _e, p, _s in located})
            if located:
                notes.append(f"{len(located)} disclosure items located (pages "
                             f"{', '.join(map(str, pages[:8]))}{' ...' if len(pages) > 8 else ''}"
                             "); recorded as fact - no registered rule could grade them")
            evidence = tuple(dict.fromkeys([*evidence, *(e for e, _p, _s in located)]))[:50]
            sources = tuple(dict.fromkeys([*sources, *(src for _e, _p, src in located)]))[:20]
        elif evaluated:
            evidence_status, risk_status = E.NOT_EVALUABLE, R.NOT_EVALUABLE
        else:
            absent = all(_ABSENT.search(s.reason or "") for s in missing) if missing else True
            evidence_status = E.SOURCE_NOT_AVAILABLE if absent else E.NOT_EVALUABLE
            risk_status = R.NOT_EVALUABLE
        if risk_status == R.NOT_EVALUABLE:
            reason = reason or ("NO_REGISTERED_RULE" if located or facts else "RULE_NOT_RUN")
            notes += [f"{s.code} not evaluated ({s.reason})" for s in missing]
        finding = "; ".join(n for n in notes if n) or (
            "no source for this topic was reviewed" if evidence_status == E.SOURCE_NOT_AVAILABLE
            else "not evaluated")
        out.append(GovernanceTopic(
            topic=topic, title=title, material=material, evidence_status=evidence_status,
            risk_status=risk_status, finding=finding[:1000],
            reason=reason if risk_status == R.NOT_EVALUABLE else None,
            signal_ids=tuple(s.risk_id for s in rules), evidence_ids=evidence[:50],
            record_ids=records[:50], source_ids=tuple(x[:1000] for x in sources[:20])))
    return tuple(out)


def verify_governance_provenance(dataset: DueDiligenceDataset,
                                 bundle: EvidenceBundle | None = None) -> list[str]:
    """Every governance topic resolves: its signals exist in Box 13, its evidence IDs are Box
    3 evidence (when the bundle is supplied) or evidence its signals cite, and a finding or a
    'no adverse evidence' reading cites at least one record."""
    problems: list[str] = []
    signals = {s.risk_id: s for s in dataset.signals}
    bundle_ids = ({i.evidence_id for x in bundle.extractions for i in x.evidence}
                  if bundle is not None else set())
    for t in dataset.governance_risks.topics:
        what = f"governance topic {t.topic}"
        if not set(t.signal_ids) <= set(signals):
            problems.append(f"{what}: unknown signal")
        cited = {e for i in t.signal_ids if i in signals for e in signals[i].evidence_ids}
        for e in t.evidence_ids:
            if e not in cited and (bundle is None or e not in bundle_ids):
                problems.append(f"{what}: evidence {e} not in Box 3 nor cited by its rules")
        if t.risk_status == R.FLAGGED and not any(
                _flagged(signals[i]) for i in t.signal_ids if i in signals):
            problems.append(f"{what}: FLAGGED without a flagged registered rule")
        if t.risk_status == R.EVALUATED_NO_FLAG and any(
                signals[i].status not in (*EVALUATED, *NOT_APPLICABLE)
                for i in t.signal_ids if i in signals):
            problems.append(f"{what}: 'no adverse evidence' over an unevaluated rule")
    return problems


__all__ = ["auditor_reports_from_bundle", "governance_topics", "merge_auditor_reports",
           "verify_governance_provenance"]
