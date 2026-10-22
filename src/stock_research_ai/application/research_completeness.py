"""DETAILED_RESEARCH_V1 research completeness: the material-requirement ledger.

Execution success is not analytical completeness. Every requirement below belongs to the
company's Box 4 business model (the one classification; peer_selection.business_model) and
is judged from what the upstream layer actually produced, never from the layer having run:

  block                  material  judged from
  HISTORICAL_FINANCIALS  yes       Box 14 sections HISTORICAL_FINANCIALS + CASH_FLOW_BALANCE_SHEET
  FINANCIAL_ANALYTICS    yes       Box 5 core metrics of the model (latest FY, analytical basis)
  BUSINESS_ANALYSIS      yes       Box 14 section BUSINESS_MODEL
  PEER_ANALYSIS          yes       Box 9 core benchmarks of the model (NO_ELIGIBLE_PEERS = gap)
  FORECAST               yes       Box 10 core lines of its forecast model (first BASE year)
  VALUATION              yes       Box 11 BASE results of the methods applicable to the model
  RISK_ANALYSIS          yes       Box 13 applicable company-risk rules: evaluated vs not
  GOVERNANCE             yes       Box 13 governance / management / auditor rules: evaluated?
  SOURCE_INTEGRITY       yes       Box 14 sections DATA_QUALITY_RECONCILIATION + SOURCE_REGISTER
  OPERATING_KPIS         optional  Box 7 canonical KPIs
  MANAGEMENT_COMMENTARY  optional  Box 14 section CONCALL_GUIDANCE
  MARKET_AND_EVENTS      optional  Box 14 sections CORPORATE_EVENTS + SHAREHOLDING
  CREDIT_RATINGS         optional  Box 14 section CREDIT_LIQUIDITY
  ORDER_BOOK             optional  Box 14 section ORDER_BOOK (NOT_APPLICABLE to financials)

A financial subtype Box 4 left unresolved imposes general-financial requirements only (no
lender- or bank-specific metric is required of it). An UNKNOWN business model has no
model-specific requirement it could satisfy: those blocks are UNAVAILABLE
(CLASSIFICATION_UNRESOLVED), so the research is never COMPLETE.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from stock_research_ai.application.peer_registry import (
    BANK_MODEL,
    NON_BANK_FINANCIAL_MODEL,
    NON_FINANCIAL_MODEL,
    UNKNOWN_MODEL,
)
from stock_research_ai.application.peer_selection import business_model
from stock_research_ai.domain.final_research_report import (
    CompletenessBlock,
    ResearchCompleteness,
    SectionCompleteness,
    SectionId,
    SectionStatus,
    overall_completeness,
)

S = SectionId
ST = SectionStatus
COMPLETENESS_VERSION = "research-completeness/1"
NBFC = "NBFC"

#: Box 5 core analytics per business model (latest FY on the analytical basis)
ANALYTICS: dict[str, tuple[str, ...]] = {
    NON_FINANCIAL_MODEL: ("REVENUE_GROWTH_YOY", "EBITDA_MARGIN", "PAT_MARGIN", "ROE", "ROCE",
                          "DEBT_TO_EQUITY", "CFO_TO_PAT"),
    BANK_MODEL: ("PAT_GROWTH_YOY", "ROE", "ROA", "FINANCING_PROFIT_MARGIN", "GROSS_NPA_RATIO",
                 "NET_NPA_RATIO"),
    # general financial: required of every non-bank financial company, subtype or not
    NON_BANK_FINANCIAL_MODEL: ("PAT_GROWTH_YOY", "ROE", "ROA"),
}
#: lender analytics, required only once Box 4 has resolved the subtype as a lender (NBFC)
LENDER_ANALYTICS: tuple[str, ...] = ("BORROWINGS_TO_EQUITY", "COST_OF_BORROWINGS")
#: Box 9 core benchmarks per business model; every other benchmark is optional
PEERS: dict[str, tuple[str, ...]] = {
    NON_FINANCIAL_MODEL: ("REVENUE_GROWTH_YOY", "EBITDA_MARGIN", "ROE"),
    BANK_MODEL: ("ROE", "ROA"),
    NON_BANK_FINANCIAL_MODEL: ("ROE", "PAT_GROWTH_YOY"),  # business-model neutral
}
#: Box 10 core lines per forecast model (the first BASE forecast year)
FORECAST_LINES: dict[str, tuple[str, ...]] = {
    "NON_FINANCIAL": ("REVENUE", "EBITDA", "PAT"),
    "BANK": ("REVENUE", "PAT"),
    "NON_BANK_FINANCIAL": ("TOTAL_ASSETS", "PAT", "COMMON_EQUITY", "TOTAL_BORROWINGS"),
}
#: Box 11 results whose reason says the method does not belong to this company's model
VALUATION_NOT_APPLICABLE = frozenset({"UNSUPPORTED_SECTOR", "NOT_APPLICABLE"})
#: Box 13 rule statuses: evaluated, and not applicable to the company
EVALUATED = frozenset({"EVALUATED", "INFORMATIONAL"})
RULE_NOT_APPLICABLE = frozenset({"NOT_APPLICABLE", "UNSUPPORTED_SECTOR_RULE"})
GOVERNANCE_CATEGORIES = frozenset({"GOVERNANCE", "MANAGEMENT", "AUDITOR"})


def _block(block: str, title: str, material: bool, status: SectionStatus,
           reasons: Iterable[str] = (), detail: str | None = None,
           sections: tuple[SectionId, ...] = ()) -> CompletenessBlock:
    codes = tuple(sorted(set(reasons))) if status in (ST.PARTIAL, ST.UNAVAILABLE) else ()
    return CompletenessBlock(block=block, title=title, material=material, status=status,
                             reasons=codes or (("UNSPECIFIED",) if status in (
                                 ST.PARTIAL, ST.UNAVAILABLE) else ()),
                             detail=detail[:1000] if detail else None, section_ids=sections)


def _coverage(done: int, total: int) -> SectionStatus:
    if total == 0 or done == 0:
        return ST.UNAVAILABLE
    return ST.COMPLETE if done == total else ST.PARTIAL


def _sections(statuses: Mapping[SectionId, tuple[SectionStatus, str, SectionCompleteness]],
              ids: tuple[SectionId, ...]) -> tuple[SectionStatus, list[str]]:
    applicable = [(i, statuses[i]) for i in ids if statuses[i][0] != ST.NOT_APPLICABLE]
    if not applicable:
        return ST.NOT_APPLICABLE, []
    reasons = [f"{f.value}_MISSING" for _i, (st, _r, c) in applicable if st != ST.COMPLETE
               for f in c.missing]
    reasons += [f"{i.value}_{st.value}" for i, (st, _r, c) in applicable
                if st != ST.COMPLETE and not c.missing]
    states = {st for _i, (st, _r, _c) in applicable}
    if states == {ST.COMPLETE}:
        return ST.COMPLETE, []
    if states == {ST.UNAVAILABLE}:
        return ST.UNAVAILABLE, reasons
    return ST.PARTIAL, reasons


def _unresolved(block: str, title: str) -> CompletenessBlock:
    return _block(block, title, True, ST.UNAVAILABLE, ("CLASSIFICATION_UNRESOLVED",),
                  "Box 4 business model unresolved: no model-specific requirement applies")


def analytics_block(box4: Any, box5: Any, basis: str | None) -> CompletenessBlock:
    model, subtype = business_model(box4)
    title = "Financial analytics (Box 5 core metrics)"
    if model == UNKNOWN_MODEL:
        return _unresolved("FINANCIAL_ANALYTICS", title)
    required = ANALYTICS[model] + (LENDER_ANALYTICS if subtype == NBFC else ())
    metrics = [m for m in box5.all_metrics() if m.reporting_basis.value == basis
               and m.period.period_type.value == "FY"]
    latest = max((m.period.period_end for m in metrics if m.status.value == "CALCULATED"),
                 default=None)
    status = {m.code: m.status.value for m in metrics if m.period.period_end == latest}
    missing = [c for c in required if status.get(c) != "CALCULATED"]
    return _block("FINANCIAL_ANALYTICS", title, True,
                  _coverage(len(required) - len(missing), len(required)),
                  (status.get(c, "NOT_CALCULATED") for c in missing),
                  f"{len(required) - len(missing)} of {len(required)} core metrics calculated"
                  + (f" ({latest})" if latest else "")
                  + (f"; missing: {', '.join(missing)}" if missing else ""))


def peer_block(box4: Any, box9: Any, basis: str | None) -> CompletenessBlock:
    model, _subtype = business_model(box4)
    title = "Peer analysis (Box 9 core benchmarks)"
    sections = (S.INDUSTRY_COMPETITION,)
    if model == UNKNOWN_MODEL:
        return _unresolved("PEER_ANALYSIS", title)
    required = PEERS[model]
    if box9 is None:
        return _block("PEER_ANALYSIS", title, True, ST.UNAVAILABLE, ("LAYER_NOT_SUPPLIED",),
                      "Box 9 not supplied")
    by_code = {b.metric_code: b for b in box9.financial_benchmarks
               if b.reporting_basis.value == basis}
    reasons, done = [], 0
    for code in required:
        b = by_code.get(code)
        if b is None:
            reasons.append("TARGET_METRIC_UNAVAILABLE")
        elif b.availability == "UNAVAILABLE":
            reasons.append("NO_ELIGIBLE_PEERS")
        else:
            done += 1
    counts = ", ".join(
        f"{c} {by_code[c].statistics.peer_count} eligible" if c in by_code else f"{c} absent"
        for c in required)
    return _block("PEER_ANALYSIS", title, True, _coverage(done, len(required)), reasons,
                  f"{done} of {len(required)} core benchmarks available ({counts})",
                  sections if done == len(required) else ())


def forecast_block(box10: Any) -> CompletenessBlock:
    title = "Forecast (Box 10 core lines)"
    if box10 is None:
        return _block("FORECAST", title, True, ST.UNAVAILABLE, ("LAYER_NOT_SUPPLIED",),
                      "Box 10 not supplied")
    model = box10.base.forecast_model.value
    required = FORECAST_LINES.get(model)
    if required is None:
        return _unresolved("FORECAST", title)
    years = [v.period.fiscal_year for v in box10.values
             if v.scenario is not None and v.scenario.value == "BASE"]
    first = min(years, default=None)
    available = {v.line.value for v in box10.values if v.scenario is not None
                 and v.scenario.value == "BASE" and v.period.fiscal_year == first
                 and v.status == "AVAILABLE"}
    missing = [line for line in required if line not in available]
    reasons = [f"{line}_NOT_FORECAST" for line in missing]
    if box10.status.value == "UNAVAILABLE":
        reasons += sorted({i.code for i in box10.issues})[:8]
    return _block("FORECAST", title, True,
                  ST.UNAVAILABLE if box10.status.value == "UNAVAILABLE"
                  else _coverage(len(required) - len(missing), len(required)), reasons,
                  f"{model}: {len(required) - len(missing)} of {len(required)} core lines "
                  f"forecast for FY{first}" + (f"; missing: {', '.join(missing)}"
                                                if missing else ""))


def valuation_block(box11: Any) -> CompletenessBlock:
    title = "Valuation (Box 11 applicable methods)"
    if box11 is None:
        return _block("VALUATION", title, True, ST.UNAVAILABLE, ("LAYER_NOT_SUPPLIED",),
                      "Box 11 not supplied")
    base = [r for r in box11.results if r.scenario.value == "BASE"]
    applicable = [r for r in base if (r.reason or "") not in VALUATION_NOT_APPLICABLE]
    na = sorted({r.method.value for r in base} - {r.method.value for r in applicable})
    if not applicable:
        return _block("VALUATION", title, True, ST.UNAVAILABLE, ("NO_APPLICABLE_METHOD",),
                      "no valuation method applies to this business model")
    usable = [r for r in applicable if r.status.value == "COMPLETE"]
    some = [r for r in applicable if r.status.value in ("COMPLETE", "PARTIAL")]
    status = (ST.COMPLETE if len(usable) == len(applicable) else ST.PARTIAL if some
              else ST.UNAVAILABLE)
    return _block("VALUATION", title, True, status,
                  ((r.reason or r.status.value) for r in applicable
                   if r.status.value != "COMPLETE"),
                  f"{len(usable)} of {len(applicable)} applicable BASE methods valued ("
                  + ", ".join(f"{r.method.value} {r.reason or r.status.value}"
                              for r in applicable) + ")"
                  + (f"; not applicable: {', '.join(na)}" if na else ""))


def _rules(box13: Any, governance: bool) -> tuple[list[Any], list[Any]]:
    applicable = [s for s in box13.signals if s.signal_class.value == "COMPANY_RISK"
                  and s.status.value not in RULE_NOT_APPLICABLE
                  and (s.category.value in GOVERNANCE_CATEGORIES) == governance]
    return applicable, [s for s in applicable if s.status.value in EVALUATED]


def risk_block(box13: Any) -> CompletenessBlock:
    applicable, evaluated = _rules(box13, governance=False)
    not_evaluated = Counter(s.status.value for s in applicable if s.status.value not in EVALUATED)
    gaps = ", ".join(f"{k} {n}" for k, n in sorted(not_evaluated.items()))
    return _block("RISK_ANALYSIS", "Risk analysis (Box 13 applicable rules)", True,
                  _coverage(len(evaluated), len(applicable)),
                  (f"RULE_{k}" for k in not_evaluated) if applicable else ("NO_APPLICABLE_RULE",),
                  f"{len(evaluated)} of {len(applicable)} applicable rules evaluated (an "
                  "unevaluated rule is not a 'no risk' finding)"
                  + (f"; not evaluated: {gaps}" if gaps else ""),
                  (S.RISKS,) if evaluated and len(evaluated) == len(applicable) else ())


def governance_block(box13: Any) -> CompletenessBlock:
    """From Box 13's governance evidence ledger: a MATERIAL topic counts as evaluated only
    when its registered rules ran on cited evidence (FLAGGED / EVALUATED_NO_FLAG) - a located
    disclosure without a rule, or missing evidence, never completes it."""
    topics = [t for t in getattr(box13.governance_risks, "topics", ())
              if t.material and t.risk_status.value != "NOT_APPLICABLE"]
    if topics:
        done = [t for t in topics if t.risk_status.value in ("FLAGGED", "EVALUATED_NO_FLAG")]
        status = _coverage(len(done), len(topics))
        missing = [t for t in topics if t not in done]
        return _block("GOVERNANCE", "Governance & management evidence (Box 13 ledger)", True,
                      status, (t.reason or "MISSING_SOURCE_EVIDENCE" for t in missing),
                      f"{len(done)} of {len(topics)} material governance topics evaluated"
                      + (f" ({', '.join(t.topic for t in done)})" if done else "")
                      + (f"; not evaluated: {', '.join(t.topic for t in missing)}"
                         if missing else ""))
    applicable, evaluated = _rules(box13, governance=True)
    status = _coverage(len(evaluated), len(applicable))
    return _block("GOVERNANCE", "Governance & management evidence (Box 13 rules)", True, status,
                  ("MISSING_SOURCE_EVIDENCE",) if status != ST.COMPLETE else (),
                  f"{len(evaluated)} of {len(applicable)} governance / management / auditor "
                  "rules evaluated")


def section_block(block: str, title: str, material: bool,
                  statuses: Mapping[SectionId, tuple[SectionStatus, str, SectionCompleteness]],
                  ids: tuple[SectionId, ...]) -> CompletenessBlock:
    status, reasons = _sections(statuses, ids)
    detail = "; ".join(f"{i.value} {statuses[i][0].value}" for i in ids)
    return _block(block, title, material, status, reasons, detail, ids)


def kpi_block(box7: Any) -> CompletenessBlock:
    n = len(box7.canonical_kpi_values) if box7 is not None else 0
    return _block("OPERATING_KPIS", "Operating KPIs (Box 7)", False,
                  ST.COMPLETE if n else ST.UNAVAILABLE,
                  ("LAYER_NOT_SUPPLIED",) if box7 is None else ("MISSING_SOURCE_EVIDENCE",),
                  f"{n} canonical KPI values")


def build_completeness(
    *, box4: Any, box5: Any, box7: Any, box9: Any, box10: Any, box11: Any, box13: Any,
    basis: str | None,
    statuses: Mapping[SectionId, tuple[SectionStatus, str, SectionCompleteness]],
    peers: CompletenessBlock, risks: CompletenessBlock,
) -> ResearchCompleteness:
    model, subtype = business_model(box4)
    blocks = [
        section_block("HISTORICAL_FINANCIALS", "Historical financials", True, statuses,
                      (S.HISTORICAL_FINANCIALS, S.CASH_FLOW_BALANCE_SHEET)),
        analytics_block(box4, box5, basis),
        section_block("BUSINESS_ANALYSIS", "Business analysis", True, statuses,
                      (S.BUSINESS_MODEL,)),
        peers,
        forecast_block(box10),
        valuation_block(box11),
        risks,
        governance_block(box13),
        section_block("SOURCE_INTEGRITY", "Sources & reconciliation", True, statuses,
                      (S.DATA_QUALITY_RECONCILIATION, S.SOURCE_REGISTER)),
        kpi_block(box7),
        section_block("MANAGEMENT_COMMENTARY", "Concalls & guidance", False, statuses,
                      (S.CONCALL_GUIDANCE,)),
        section_block("MARKET_AND_EVENTS", "Market data, events & shareholding", False,
                      statuses, (S.CORPORATE_EVENTS, S.SHAREHOLDING)),
        section_block("CREDIT_RATINGS", "Credit ratings", False, statuses,
                      (S.CREDIT_LIQUIDITY,)),
        section_block("ORDER_BOOK", "Order book", False, statuses, (S.ORDER_BOOK,)),
    ]
    return ResearchCompleteness(business_model=model, subtype=subtype,
                                overall=overall_completeness(blocks), blocks=tuple(blocks))


def summary_lines(rc: ResearchCompleteness) -> list[tuple[str, str]]:
    """The bounded, reader-facing summary: overall, then the MATERIAL requirements by status
    (with their reason codes) and the optional ones apart - never every low-level metric."""
    def names(status: SectionStatus, material: bool) -> str:
        found = [b.title + (f" ({', '.join(b.reasons)})" if b.reasons else "")
                 for b in rc.blocks if b.status == status and b.material == material]
        return "; ".join(found) or "None"
    model = rc.business_model + (f" / {rc.subtype}" if rc.subtype else "")
    return [
        ("Overall", f"{rc.overall.value} (business model {model}; COMPLETE only when every "
                    "material applicable requirement is complete)"),
        ("Complete", names(ST.COMPLETE, True)),
        ("Partial", names(ST.PARTIAL, True)),
        ("Unavailable", names(ST.UNAVAILABLE, True)),
        ("Not applicable", names(ST.NOT_APPLICABLE, True)),
        ("Optional (not counted)", "; ".join(
            f"{b.title}: {b.status.value}" for b in rc.blocks if not b.material) or "None"),
    ]


__all__ = ["COMPLETENESS_VERSION", "build_completeness", "peer_block", "risk_block",
           "summary_lines"]
