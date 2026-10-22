"""DETAILED_RESEARCH_V1 Box 14: Boxes 3-13 -> FinalResearchReportDataset (structured only).

Pipeline:
  integrity (every input re-validates; one company; the Box 4 / Box 5 hashes match; every other
  supplied dataset is the exact one Box 13 assessed (hash) - a dataset Box 13 never saw is
  refused; identity binds; nothing is dated after as_of) ->
  claim book + source register -> risk register (Box 13 signals, never re-graded) ->
  substantive sections 2-22 -> section status / completeness -> section 0 (due-diligence
  status) -> executive summary LAST (only existing claims) -> appendices -> reference check
  (every upstream / evidence / calculation / risk ID exists in the supplied datasets) ->
  the dataset contract (claim discipline, basis, L1, advice language, bounds).

No AI, no network, no clock: `generated_at` is injected and kept out of every ID. Inputs are
never mutated. Box 14 renders nothing and recommends nothing.
"""

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from pydantic import BaseModel

from stock_research_ai.application.business_intelligence import canonical_business_json
from stock_research_ai.application.document_intelligence import canonical_bundle_json
from stock_research_ai.application.due_diligence import (
    canonical_due_diligence_json,
    canonical_market_json,
)
from stock_research_ai.application.financial_analytics import canonical_analytics_json
from stock_research_ai.application.financial_charts import canonical_chart_json
from stock_research_ai.application.financial_reconciliation import canonical_dataset_json
from stock_research_ai.application.forecasting import canonical_forecast_json
from stock_research_ai.application.fundamental_valuation import canonical_valuation_json
from stock_research_ai.application.industry_intelligence import canonical_industry_json
from stock_research_ai.application.management_intelligence import canonical_management_json
from stock_research_ai.application.report_claims import (
    NS,
    Book,
    ReportCompositionError,
    Resolver,
    id_index,
    up,
    verify_references,
)
from stock_research_ai.application.report_sections import (
    Ctx,
    build_signals,
    business,
    cash_balance,
    concalls,
    credit,
    disagreements,
    events,
    forecasts,
    forensics,
    historical,
    industry,
    management,
    monitoring,
    order_book,
    overview,
    periods,
    reconciliation,
    risks,
    scenarios,
    shareholding,
    valuation,
    what_changes,
)
from stock_research_ai.application.research_completeness import (
    build_completeness,
    peer_block,
    risk_block,
)
from stock_research_ai.domain.business_intelligence import BusinessIntelligenceDataset
from stock_research_ai.domain.document_intelligence import EvidenceBundle
from stock_research_ai.domain.due_diligence import DueDiligenceDataset
from stock_research_ai.domain.final_research_report import (
    FINAL_REPORT_ENGINE_VERSION,
    PRIMARY_KINDS,
    SECTION_TITLES,
    SELECTION_RULES_VERSION,
    Appendix,
    AppendixId,
    ChartReference,
    ClaimBasis,
    ClaimType,
    DueDiligenceStatus,
    ExecutiveSummary,
    FamilyCheck,
    FamilyStatus,
    FinalReportRequest,
    FinalResearchReportDataset,
    InputFamily,
    InputReference,
    Layer,
    Materiality,
    OrderBookView,
    ReportLimits,
    ReportMetadata,
    ReportSection,
    RiskPresentation,
    Scenario,
    SectionCompleteness,
    SectionId,
    SectionStatus,
    SummaryKind,
    SummaryPoint,
    ValuationView,
)
from stock_research_ai.domain.financial_analytics import FinancialAnalyticsDataset
from stock_research_ai.domain.financial_canonical import (
    BusinessClass,
    CanonicalFinancialDataset,
    Currency,
    Industry,
)
from stock_research_ai.domain.financial_charts import FinancialChartBundle
from stock_research_ai.domain.forecasting import ForecastDataset
from stock_research_ai.domain.fundamental_valuation import ValuationDataset
from stock_research_ai.domain.industry_intelligence import IndustryIntelligenceDataset
from stock_research_ai.domain.management_intelligence import ManagementIntelligenceDataset
from stock_research_ai.domain.market_intelligence import MarketIntelligenceDataset

S = SectionId
F = InputFamily

#: the input families each section needs (deterministic completeness, no percentage)
REQUIRED: dict[SectionId, tuple[InputFamily, ...]] = {
    S.DUE_DILIGENCE_STATUS: (F.BOX13_DILIGENCE,),
    S.EXECUTIVE_SUMMARY: (F.BOX4_FINANCIALS, F.BOX5_ANALYTICS, F.BOX13_DILIGENCE),
    S.COMPANY_OVERVIEW: (F.BOX4_FINANCIALS, F.BOX7_BUSINESS, F.BOX12_MARKET),
    S.BUSINESS_MODEL: (F.BOX5_ANALYTICS, F.BOX7_BUSINESS, F.SEGMENT_DATA),
    S.INDUSTRY_COMPETITION: (F.BOX9_INDUSTRY,),
    S.HISTORICAL_FINANCIALS: (F.BOX4_FINANCIALS, F.BOX5_ANALYTICS, F.QUARTERLY_FINANCIALS),
    S.CASH_FLOW_BALANCE_SHEET: (F.BOX4_FINANCIALS, F.BOX5_ANALYTICS, F.BOX13_DILIGENCE),
    S.ACCOUNTING_FORENSICS: (F.BOX5_ANALYTICS, F.BOX13_DILIGENCE),
    S.MANAGEMENT_GOVERNANCE: (F.BOX8_MANAGEMENT, F.BOX13_DILIGENCE),
    S.CONCALL_GUIDANCE: (F.BOX8_MANAGEMENT, F.CONCALLS, F.GUIDANCE),
    S.CORPORATE_EVENTS: (F.BOX12_MARKET,),
    S.ORDER_BOOK: (F.BOX12_MARKET, F.BOX13_DILIGENCE, F.ORDER_LEDGER),
    S.SHAREHOLDING: (F.BOX13_DILIGENCE, F.SHAREHOLDING_DATA),
    S.CREDIT_LIQUIDITY: (F.BOX13_DILIGENCE, F.RATINGS),
    S.FORECASTS: (F.BOX10_FORECAST,),
    S.SCENARIO_ANALYSIS: (F.BOX10_FORECAST, F.BOX11_VALUATION),
    S.VALUATION: (F.BOX11_VALUATION, F.MARKET_DATA),
    S.RISKS: (F.BOX13_DILIGENCE,),
    S.WHAT_CHANGES_THE_VIEW: (F.BOX13_DILIGENCE,),
    S.MONITORING: (F.BOX13_DILIGENCE,),
    S.DATA_QUALITY_RECONCILIATION: (F.BOX13_DILIGENCE,),
    S.SOURCE_REGISTER: (F.BOX4_FINANCIALS,),
    S.APPENDICES: (F.BOX4_FINANCIALS,),
}
APPENDIX_TABLES: dict[str, AppendixId] = {
    "ANNUAL_FINANCIALS_FULL": AppendixId.FULL_FINANCIALS,
    "STANDALONE_FINANCIALS": AppendixId.STANDALONE_FINANCIALS,
    "QUARTERLY_FINANCIALS_FULL": AppendixId.QUARTERLY_TABLES,
    "EVENT_LEDGER": AppendixId.EVENT_LEDGER,
    "ORDER_LEDGER": AppendixId.ORDER_LEDGER,
    "SHAREHOLDING_HISTORY": AppendixId.SHAREHOLDING_HISTORY,
    "MANAGEMENT_STATEMENTS": AppendixId.MANAGEMENT_STATEMENTS,
    "GOVERNANCE_CATALOGUE": AppendixId.GOVERNANCE_CATALOGUE,
    "VALUATION_ASSUMPTIONS": AppendixId.VALUATION_ASSUMPTIONS,
    "RISK_REGISTER": AppendixId.RISK_REGISTER,
    "OPEN_DILIGENCE": AppendixId.OPEN_DILIGENCE,
    "SOURCE_DISAGREEMENTS": AppendixId.SOURCE_DISAGREEMENTS,
}
CHART_SECTIONS = {
    "GROWTH": S.HISTORICAL_FINANCIALS, "MARGINS": S.HISTORICAL_FINANCIALS,
    "QUARTERLY": S.HISTORICAL_FINANCIALS, "RETURNS": S.HISTORICAL_FINANCIALS,
    "BANKING": S.HISTORICAL_FINANCIALS, "CASH_FLOW": S.CASH_FLOW_BALANCE_SHEET,
    "BALANCE_SHEET": S.CASH_FLOW_BALANCE_SHEET, "WORKING_CAPITAL": S.CASH_FLOW_BALANCE_SHEET,
    "EARNINGS_QUALITY": S.ACCOUNTING_FORENSICS, "FORENSIC_MODELS": S.ACCOUNTING_FORENSICS,
    "SIGNALS": S.ACCOUNTING_FORENSICS, "SEGMENTS": S.BUSINESS_MODEL,
    "GEOGRAPHY": S.BUSINESS_MODEL, "OPERATING_KPIS": S.BUSINESS_MODEL,
}
SECTION_ZERO_KINDS = ("BLOCKED_SOURCE", "MISSING_REQUIRED_DOCUMENT",
                      "VALUATION_INPUTS_UNAVAILABLE", "FORECAST_INPUTS_UNAVAILABLE",
                      "STALE_DATA", "IDENTITY_PARTIAL", "INCOMPLETE_ORDER_LINKAGE",
                      "INPUT_CONFLICT", "DOCUMENT_SKIPPED")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_report_json(report: FinalResearchReportDataset) -> str:
    """Deterministic serialization; generated_at is excluded so equal inputs compare equal."""
    dump = report.model_dump(mode="json")
    dump["metadata"].pop("generated_at", None)
    return json.dumps(dump, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _copy[T_: BaseModel](value: T_ | None) -> T_ | None:
    """A defensive, re-validated copy: an edited / model_construct()-ed input must still meet
    its own contract, and Box 14 never touches the caller's object."""
    if value is None:
        return None
    return type(value).model_validate(value.model_dump())


def _fail(code: str, detail: str) -> ReportCompositionError:
    return ReportCompositionError(code, detail)


def _business_class(box4: CanonicalFinancialDataset) -> BusinessClass:
    return box4.classification


_GATE = {BusinessClass.FINANCIAL: "FINANCIAL", BusinessClass.NON_FINANCIAL: "NON_FINANCIAL",
         BusinessClass.UNKNOWN: "UNKNOWN"}


def _classification(box4: CanonicalFinancialDataset, box13: DueDiligenceDataset,
                    supplied: dict[Layer, Any]) -> None:
    """ONE business classification per report: every layer must state Box 4's (its
    industry, or the family / gate that Box 4's class implies); a contradiction is refused."""
    cls = _business_class(box4)
    family = {BusinessClass.FINANCIAL: {"FINANCIAL"}, BusinessClass.UNKNOWN: {"UNKNOWN"}}.get(
        cls)  # NON_FINANCIAL: any non-financial, resolved family
    wrong: list[str] = []
    if box13.industry != box4.industry or box13.sector_gate.value != _GATE[cls]:
        wrong.append(f"Box 13 {box13.industry.value}/{box13.sector_gate.value}")
    for layer in (Layer.BOX10, Layer.BOX11):
        ds = supplied.get(layer)
        if ds is not None and getattr(ds, "industry", box4.industry) != box4.industry:
            wrong.append(f"{layer.value} {ds.industry.value}")
    families = []
    box7 = supplied.get(Layer.BOX7)
    if isinstance(box7, BusinessIntelligenceDataset):
        families.append(("Box 7", box7.sector.value))
        if box7.business_structure.industry != box4.industry:
            wrong.append(f"Box 7 industry {box7.business_structure.industry.value}")
    box9 = supplied.get(Layer.BOX9)
    if isinstance(box9, IndustryIntelligenceDataset):
        families.append(("Box 9", box9.sector_family))
    for name, value in families:
        ok = value in family if family is not None else value not in ("FINANCIAL", "UNKNOWN")
        if not ok:
            wrong.append(f"{name} family {value}")
    if wrong:
        raise _fail("CLASSIFICATION_MISMATCH", f"Box 4 is {box4.industry.value} / {cls.value}; "
                    + "; ".join(wrong)[:400])


def _integrity(request: FinalReportRequest, box4: CanonicalFinancialDataset,
               box5: FinancialAnalyticsDataset, box13: DueDiligenceDataset,
               shas: dict[Layer, str], supplied: dict[Layer, Any]) -> list[str]:
    key = request.company_key
    for layer, ds in supplied.items():
        own = getattr(ds, "company_key", key)
        if own != key:
            raise _fail("COMPANY_MISMATCH", f"the {layer.value} dataset belongs to {own!r}, "
                        f"not {key!r}")
    refs = {r.layer.value: r for r in box13.sources}
    for layer in (Layer.BOX4, Layer.BOX5):
        ref = refs.get(layer.value)
        if ref is None or ref.sha256 != shas[layer]:
            raise _fail(f"{layer.value}_MISMATCH", f"Box 13 was not built from this "
                        f"{layer.value} dataset")
    notes: list[str] = []
    for layer in (Layer.BOX3, Layer.BOX7, Layer.BOX8, Layer.BOX9, Layer.BOX10, Layer.BOX11,
                  Layer.BOX12):
        ref = refs.get(layer.value)
        if layer in supplied:
            if ref is None:
                raise _fail("UNASSESSED_INPUT", f"Box 13 never assessed the supplied "
                            f"{layer.value} dataset; rebuild Box 13 with it")
            if ref.sha256 != shas[layer]:
                raise _fail(f"{layer.value}_MISMATCH", f"the supplied {layer.value} dataset is "
                            "not the one Box 13 assessed")
        elif ref is not None:
            notes.append(layer.value)
    box6 = supplied.get(Layer.BOX6)
    if isinstance(box6, FinancialChartBundle) and (
            box6.source_box4.sha256 != shas[Layer.BOX4]
            or box6.source_box5.sha256 != shas[Layer.BOX5]):
        raise _fail("BOX6_MISMATCH", "Box 6 charts were not built from these Box 4 / Box 5")
    # identity
    asked = {"nse": request.nse_symbol, "isin": request.isin}
    stated = {"nse": box13.request.nse_symbol, "isin": box13.request.isin}
    box12 = supplied.get(Layer.BOX12)
    for name in ("nse", "isin"):
        if asked[name] is not None and stated[name] is not None and asked[name] != stated[name]:
            raise _fail("IDENTITY_MISMATCH", f"the request {name} differs from Box 13's")
        if asked[name] is not None and isinstance(box12, MarketIntelligenceDataset):
            ident = box12.identity
            verified = ident.nse_symbol if name == "nse" else ident.isin
            if verified is not None and verified != asked[name]:
                raise _fail("IDENTITY_MISMATCH", f"Box 12 verified {name} {verified} differs")
    # basis: one analytical basis (Box 13's) for every comparable company-level layer
    analytical = box13.analytical_basis.value
    bases: dict[str, str] = {}
    box9 = supplied.get(Layer.BOX9)
    if isinstance(box9, IndustryIntelligenceDataset):
        bases.update({f"Box 9 {b.metric_code}": b.reporting_basis.value
                      for b in box9.financial_benchmarks})
    box10_ = supplied.get(Layer.BOX10)
    if isinstance(box10_, ForecastDataset):
        bases["Box 10 forecast"] = box10_.base.reporting_basis.value
    box11_ = supplied.get(Layer.BOX11)
    if isinstance(box11_, ValuationDataset):
        bases["Box 11 valuation"] = box11_.reporting_basis.value
    wrong = sorted(f"{k} is {v}" for k, v in bases.items() if v != analytical)
    if wrong:
        raise _fail("BASIS_MISMATCH", f"the analytical basis is {analytical}; "
                    + "; ".join(wrong)[:400])
    _classification(box4, box13, supplied)
    # time
    as_of = request.as_of
    if box13.as_of > as_of:
        raise _fail("INCOMPATIBLE_PERIOD", "Box 13 is dated after the report as_of")
    late = [v for v in box4.canonical_values if v.period.period_end is not None
            and v.period.period_end > as_of]
    if late:
        raise _fail("INCOMPATIBLE_PERIOD", f"{len(late)} Box 4 values end after as_of {as_of}")
    if isinstance(box12, MarketIntelligenceDataset):
        if box12.as_of.date() > as_of:
            raise _fail("INCOMPATIBLE_PERIOD", "Box 12 data is dated after as_of")
        future = [e for e in box12.events if e.event_date > as_of]
        if future:
            raise _fail("FUTURE_DATED_EVENT", f"{len(future)} Box 12 events are dated after "
                        f"as_of {as_of}")
    box8 = supplied.get(Layer.BOX8)
    if isinstance(box8, ManagementIntelligenceDataset) and any(
            e.call_date is not None and e.call_date > as_of for e in box8.events):
        raise _fail("FUTURE_DATED_EVENT", "a Box 8 management event is dated after as_of")
    box7 = supplied.get(Layer.BOX7)
    if isinstance(box7, BusinessIntelligenceDataset) and any(
            v.period.period_end is not None and v.period.period_end > as_of
            for v in box7.canonical_segment_values):
        raise _fail("INCOMPATIBLE_PERIOD", "Box 7 segment values end after as_of")
    box10 = supplied.get(Layer.BOX10)
    if isinstance(box10, ForecastDataset) and box10.base.latest_actual is not None and \
            box10.base.latest_actual.period_end is not None and \
            box10.base.latest_actual.period_end > as_of:
        raise _fail("INCOMPATIBLE_PERIOD", "the Box 10 historical anchor ends after as_of")
    box11 = supplied.get(Layer.BOX11)
    if isinstance(box11, ValuationDataset) and box11.valuation_date is not None and \
            box11.valuation_date.period_end is not None and \
            box11.valuation_date.period_end > as_of:
        raise _fail("INCOMPATIBLE_PERIOD", "the Box 11 valuation date is after as_of")
    return notes


def build_final_report(
    *,
    request: FinalReportRequest,
    generated_at: datetime,
    box4: CanonicalFinancialDataset,
    box5: FinancialAnalyticsDataset,
    box13: DueDiligenceDataset,
    bundle: EvidenceBundle | None = None,
    box6: FinancialChartBundle | None = None,
    box7: BusinessIntelligenceDataset | None = None,
    box8: ManagementIntelligenceDataset | None = None,
    box9: IndustryIntelligenceDataset | None = None,
    box10: ForecastDataset | None = None,
    box11: ValuationDataset | None = None,
    box12: MarketIntelligenceDataset | None = None,
    limits: ReportLimits | None = None,
) -> FinalResearchReportDataset:
    """Deterministic Box 14. Raises ReportCompositionError when the inputs cannot be composed."""
    request = FinalReportRequest.model_validate(request.model_dump())
    bounds = limits or ReportLimits()
    b4 = _copy(box4)
    b5 = _copy(box5)
    b13 = _copy(box13)
    assert b4 is not None and b5 is not None and b13 is not None
    if b4.company_key != request.company_key:
        raise _fail("COMPANY_MISMATCH", "the request names another company than Box 4")
    optional: dict[Layer, BaseModel | None] = {
        Layer.BOX3: _copy(bundle), Layer.BOX6: _copy(box6), Layer.BOX7: _copy(box7),
        Layer.BOX8: _copy(box8), Layer.BOX9: _copy(box9), Layer.BOX10: _copy(box10),
        Layer.BOX11: _copy(box11), Layer.BOX12: _copy(box12)}
    supplied: dict[Layer, Any] = {k: v for k, v in optional.items() if v is not None}
    supplied.update({Layer.BOX5: b5, Layer.BOX13: b13})
    canon: dict[Layer, Callable[[Any], str]] = {
        Layer.BOX3: canonical_bundle_json, Layer.BOX6: canonical_chart_json,
        Layer.BOX7: canonical_business_json, Layer.BOX8: canonical_management_json,
        Layer.BOX9: canonical_industry_json, Layer.BOX10: canonical_forecast_json,
        Layer.BOX11: canonical_valuation_json, Layer.BOX12: canonical_market_json,
    }
    shas: dict[Layer, str] = {Layer.BOX4: _sha(canonical_dataset_json(b4)),
                              Layer.BOX5: _sha(canonical_analytics_json(b5)),
                              Layer.BOX13: _sha(canonical_due_diligence_json(b13))}
    for layer, fn in canon.items():
        if layer in supplied:
            shas[layer] = _sha(fn(supplied[layer]))
    not_supplied = _integrity(request, b4, b5, b13, shas, supplied)
    analytical = ClaimBasis(b13.analytical_basis.value)
    control = ClaimBasis(b13.control_basis.value) if b13.control_basis else None
    book = Book(b4.company_key, request.as_of, bounds)
    inputs = _inputs(b4, b5, b13, supplied, shas)
    for ref in inputs:
        book.dataset_source(ref.layer, ref.dataset_id, ref.sha256,
                            f"{ref.layer.value} dataset ({ref.schema_version})", ref.status)
    res = Resolver(book, b4, b5, bundle=supplied.get(Layer.BOX3), box7=supplied.get(Layer.BOX7),
                   box8=supplied.get(Layer.BOX8), box10=supplied.get(Layer.BOX10),
                   box11=supplied.get(Layer.BOX11), box12=supplied.get(Layer.BOX12))
    ctx = Ctx(book=book, res=res, as_of=request.as_of, limits=bounds, box4=b4, box5=b5,
              box13=b13, bundle=supplied.get(Layer.BOX3), box6=supplied.get(Layer.BOX6),
              box7=supplied.get(Layer.BOX7), box8=supplied.get(Layer.BOX8),
              box9=supplied.get(Layer.BOX9), box10=supplied.get(Layer.BOX10),
              box11=supplied.get(Layer.BOX11), box12=supplied.get(Layer.BOX12),
              analytical=analytical, control=control, currency=b13.currency,
              financial=b13.sector_gate.value != "NON_FINANCIAL")  # restricted
    # --- substantive sections first (the summary is built last, from these)
    build_signals(ctx)
    overview(ctx)
    business(ctx)
    industry(ctx)
    historical(ctx)
    cash_balance(ctx)
    forensics(ctx)
    management(ctx)
    tracker = concalls(ctx)
    events(ctx)
    orders = order_book(ctx)
    shareholding(ctx)
    credit(ctx)
    forecast = forecasts(ctx)
    value_view = valuation(ctx)
    case = scenarios(ctx, forecast, value_view)
    risk_view = risks(ctx)
    recon = reconciliation(ctx)
    disagreement = disagreements(ctx)
    view = what_changes(ctx, tracker)
    monitor = monitoring(ctx, tracker, orders)
    charts = _charts(ctx)
    for layer in (Layer.BOX3, Layer.BOX6, Layer.BOX7, Layer.BOX8, Layer.BOX9, Layer.BOX10,
                  Layer.BOX11, Layer.BOX12):
        if layer not in supplied:
            ctx.limit(S.DATA_QUALITY_RECONCILIATION, f"LAYER_{layer.value}", "LAYER_NOT_SUPPLIED",
                      f"{layer.value} dataset" + (" (Box 13 assessed one)"
                                                  if layer.value in not_supplied else ""))
    families = _families(ctx, supplied, orders, value_view)
    # analytical completeness: a section whose material requirement is not met by what the
    # upstream layer produced (no eligible peer benchmark, unevaluated risk rules) is never
    # COMPLETE merely because the layer was supplied
    basis_value = b13.analytical_basis.value
    peer_req = peer_block(b4, ctx.box9, basis_value)
    risk_req = risk_block(b13)
    for sec, req in ((S.INDUSTRY_COMPETITION, peer_req), (S.RISKS, risk_req)):
        draft = ctx.drafts[sec]
        if req.status != SectionStatus.COMPLETE and draft.status in (None, SectionStatus.COMPLETE):
            draft.status = SectionStatus.PARTIAL
            draft.reason = "; ".join(x for x in (
                draft.reason, f"{req.block} {req.status.value}: {', '.join(req.reasons)}") if x)
    statuses = {s: _status(ctx, s, families) for s in SectionId}
    status_zero = _section_zero(ctx, families, statuses, risk_view)
    statuses[S.DUE_DILIGENCE_STATUS] = _status(ctx, S.DUE_DILIGENCE_STATUS, families)
    summary = _summary(ctx, risk_view, monitor, orders, value_view, status_zero)
    statuses[S.EXECUTIVE_SUMMARY] = _status(ctx, S.EXECUTIVE_SUMMARY, families)
    status_zero = status_zero.model_copy(update={"sections_by_status": tuple(
        (st, sum(1 for x in statuses.values() if x[0] == st)) for st in SectionStatus)})
    appendices = _appendices(ctx)
    completeness = build_completeness(
        box4=b4, box5=b5, box7=ctx.box7, box9=ctx.box9, box10=ctx.box10, box11=ctx.box11,
        box13=b13, basis=basis_value, statuses=statuses, peers=peer_req, risks=risk_req)
    if completeness.overall != SectionStatus.COMPLETE and (
            statuses[S.EXECUTIVE_SUMMARY][0] == SectionStatus.COMPLETE):
        # the executive summary summarises the research: never more complete than it
        statuses[S.EXECUTIVE_SUMMARY] = (
            SectionStatus.PARTIAL, f"research completeness {completeness.overall.value}",
            statuses[S.EXECUTIVE_SUMMARY][2])
        status_zero = status_zero.model_copy(update={"sections_by_status": tuple(
            (st, sum(1 for x in statuses.values() if x[0] == st)) for st in SectionStatus)})
    # --- assembly
    core = {i for s in SectionId if s != S.APPENDICES for i in (*ctx.drafts[s].claims,
                                                                 *ctx.drafts[s].limits)}
    high = {*ctx.drafts[S.EXECUTIVE_SUMMARY].claims, *ctx.drafts[S.EXECUTIVE_SUMMARY].limits,
            *(ctx.signal_claims[i] for i in risk_view.red)}
    materiality = {i: Materiality.HIGH if i in high else Materiality.MEDIUM
                   for i in core | high}
    claims, sources, tables = book.finish(materiality)
    index = _index(b4, b5, b13, supplied)
    index[Layer.BOX13] |= set(ctx.explicit)  # cited inputs: uuid5 over Box 13's request echo
    verify_references(claims, index, {r.risk_id for r in risk_view.risk_register})
    sections = tuple(ReportSection(
        section_id=s, order=n, title=SECTION_TITLES[s], status=statuses[s][0],
        status_reason=statuses[s][1], completeness=statuses[s][2],
        claim_ids=tuple(ctx.drafts[s].claims), limitation_claim_ids=tuple(ctx.drafts[s].limits),
        table_ids=tuple(ctx.drafts[s].tables), chart_ids=tuple(ctx.drafts[s].charts),
        notes=tuple(dict.fromkeys(ctx.drafts[s].notes))) for n, s in enumerate(SectionId))
    blocked = tuple(s.source_id for s in sources if s.status != "AVAILABLE")
    status_zero = status_zero.model_copy(update={"blocked_sources": blocked})
    ident = b12.identity if (b12 := supplied.get(Layer.BOX12)) is not None else None
    report_key = (
        f"{'|'.join(f'{r.layer.value}:{r.sha256}' for r in inputs)}|"
        f"{request.model_dump_json()}|{bounds.model_dump_json()}|{FINAL_REPORT_ENGINE_VERSION}|"
        f"{SELECTION_RULES_VERSION}")
    metadata = ReportMetadata(
        company_key=b4.company_key, company_name=b4.company_name,
        nse_symbol=ident.nse_symbol if ident else b13.request.nse_symbol,
        isin=ident.isin if ident else b13.request.isin,
        bse_code=ident.bse_code if ident else None,
        identity_status=ident.status.value if ident else "BOX12_NOT_SUPPLIED",
        industry=Industry(b4.industry.value), sector_gate=b13.sector_gate.value,
        business_model=b13.business_model.value, analytical_basis=analytical,
        control_basis=control, currency=Currency(b13.currency.value),
        unit_note="Money values carry their upstream unit (Box 4 scale; crore for Box 12 / "
                  "Box 13 inputs). Box 14 never rescales.",
        as_of=request.as_of, latest_fiscal_year_end=b13.latest_fiscal_year_end,
        generated_at=generated_at, inputs=tuple(inputs), limits=bounds,
        business_class=_business_class(b4).value,
        classification_reason=b4.classification_reason)
    return FinalResearchReportDataset(
        report_id=uuid5(NS, f"report|{report_key}"), metadata=metadata, sections=sections,
        due_diligence_status=status_zero, executive_summary=summary, investment_case=case,
        guidance_tracker=tuple(tracker), order_book=orders, valuation=value_view,
        risks=risk_view, what_changes_the_view=tuple(view), monitoring=tuple(monitor),
        reconciliation=tuple(recon), source_disagreements=tuple(disagreement),
        appendices=tuple(appendices), claims=claims, tables=tables, charts=tuple(charts),
        sources=sources, issues=tuple(ctx.issues), research_completeness=completeness)


# --- helpers ---------------------------------------------------------------------------------


def _inputs(b4: CanonicalFinancialDataset, b5: FinancialAnalyticsDataset,
            b13: DueDiligenceDataset, supplied: dict[Layer, Any],
            shas: dict[Layer, str]) -> list[InputReference]:
    refs = {r.layer.value: r for r in b13.sources}
    out = [InputReference(layer=Layer.BOX4, dataset_id=b4.dataset_id, sha256=shas[Layer.BOX4],
                          schema_version=b4.schema_version, status=b4.status,
                          assessed_by_box13=True)]
    ids = {Layer.BOX3: "bundle_id", Layer.BOX5: "analytics_id", Layer.BOX6: "bundle_id",
           Layer.BOX7: "dataset_id", Layer.BOX8: "dataset_id", Layer.BOX9: "dataset_id",
           Layer.BOX10: "forecast_id", Layer.BOX11: "valuation_id", Layer.BOX12: "dataset_id",
           Layer.BOX13: "dataset_id"}
    for layer in Layer:
        if layer == Layer.BOX4 or layer not in supplied:
            continue
        ds = supplied[layer]
        status = getattr(ds, "status", "SUPPLIED")
        if layer == Layer.BOX12:
            status = ds.identity.status
        if layer == Layer.BOX13:
            status = "SUPPLIED"
        ref = refs.get(layer.value)
        out.append(InputReference(
            layer=layer, dataset_id=getattr(ds, ids[layer]), sha256=shas[layer],
            schema_version=ds.schema_version, status=getattr(status, "value", status),
            assessed_by_box13=layer == Layer.BOX13 or (ref is not None
                                                       and ref.sha256 == shas[layer])))
    return out


def _index(b4: CanonicalFinancialDataset, b5: FinancialAnalyticsDataset,
           b13: DueDiligenceDataset, supplied: dict[Layer, Any]) -> dict[Layer, set[UUID]]:
    index = {Layer.BOX4: id_index(b4), Layer.BOX5: id_index(b5), Layer.BOX13: id_index(b13)}
    for layer, ds in supplied.items():
        if layer not in index:
            index[layer] = id_index(ds)
    return index


def _charts(ctx: Ctx) -> list[ChartReference]:
    b6 = ctx.box6
    out: list[ChartReference] = []
    if b6 is None:
        return out
    for chart in b6.charts:
        if chart.status.value not in ("READY", "PARTIAL"):
            continue
        basis = ClaimBasis(chart.reporting_basis.value) if chart.reporting_basis.value in (
            "CONSOLIDATED", "STANDALONE") else ClaimBasis.NOT_APPLICABLE
        section = CHART_SECTIONS.get(chart.category.value, S.APPENDICES)
        if basis not in (ctx.analytical, ClaimBasis.NOT_APPLICABLE):
            section = S.APPENDICES
        ref = ChartReference(
            chart_id=uuid5(NS, f"chart|{chart.chart_id}"), upstream_chart_id=chart.chart_id,
            chart_code=chart.chart_code, title=chart.title,
            purpose=f"{chart.category.value.lower().replace('_', ' ')} chart (Box 6 definition; "
                    "rendered by Box 15)", section_id=section, basis=basis,
            upstream_status=chart.status.value, source_dataset_ids=chart.source_dataset_ids,
            metric_codes=tuple(dict.fromkeys(s.metric_code for s in chart.series))[:50])
        out.append(ref)
        ctx.drafts[section].charts.append(ref.chart_id)
    if len(out) > ctx.limits.max_charts:
        raise _fail("RESOURCE_LIMIT_EXCEEDED", f"more than {ctx.limits.max_charts} charts")
    return out


def _families(ctx: Ctx, supplied: dict[Layer, Any], orders: OrderBookView,
              value_view: ValuationView) -> dict[InputFamily, FamilyCheck]:
    def check(family: InputFamily, ok: bool, detail: str | None = None,
              not_applicable: bool = False) -> FamilyCheck:
        status = FamilyStatus.NOT_APPLICABLE if not_applicable else (
            FamilyStatus.AVAILABLE if ok else FamilyStatus.MISSING)
        return FamilyCheck(family=family, status=status, detail=detail)

    b7, b8, b12 = ctx.box7, ctx.box8, ctx.box12
    layer_family = {F.BOX3_EVIDENCE: Layer.BOX3, F.BOX6_CHARTS: Layer.BOX6,
                    F.BOX7_BUSINESS: Layer.BOX7, F.BOX8_MANAGEMENT: Layer.BOX8,
                    F.BOX9_INDUSTRY: Layer.BOX9, F.BOX10_FORECAST: Layer.BOX10,
                    F.BOX11_VALUATION: Layer.BOX11, F.BOX12_MARKET: Layer.BOX12}
    out = {f: check(f, layer in supplied, None if layer in supplied else "not supplied")
           for f, layer in layer_family.items()}
    for f in (F.BOX4_FINANCIALS, F.BOX5_ANALYTICS, F.BOX13_DILIGENCE):
        out[f] = check(f, True)
    out[F.QUARTERLY_FINANCIALS] = check(F.QUARTERLY_FINANCIALS,
                                        bool(periods(ctx, ctx.analytical, "QUARTER")))
    out[F.SEGMENT_DATA] = check(F.SEGMENT_DATA, b7 is not None and any(
        ClaimBasis(v.reporting_basis.value) == ctx.analytical
        for v in b7.canonical_segment_values if v.reporting_basis.value != "UNKNOWN"))
    out[F.CONCALLS] = check(F.CONCALLS, b8 is not None and any(
        e.event_type.value == "EARNINGS_CALL" for e in b8.events))
    out[F.GUIDANCE] = check(F.GUIDANCE, b8 is not None and bool(b8.guidance))
    out[F.ORDER_LEDGER] = check(F.ORDER_LEDGER, b12 is not None and bool(b12.order_chains),
                                not_applicable=not orders.applicable)
    out[F.SHAREHOLDING_DATA] = check(F.SHAREHOLDING_DATA,
                                     bool(ctx.box13.ownership_risks.series))
    out[F.RATINGS] = check(F.RATINGS, bool(ctx.box13.liquidity_credit_risks.ratings))
    out[F.MARKET_DATA] = check(F.MARKET_DATA, bool(value_view.market_context_claim_ids))
    return out


def _status(ctx: Ctx, section: SectionId, families: dict[InputFamily, FamilyCheck]) -> tuple[
        SectionStatus, str, SectionCompleteness]:
    draft = ctx.drafts[section]
    required = REQUIRED[section]
    if draft.status is None and draft.inapplicable and not draft.has_content:
        # every upstream item is typed not applicable and no applicable content remains
        draft.status = SectionStatus.NOT_APPLICABLE
        draft.reason = (f"NOT_APPLICABLE: all {len(draft.inapplicable)} upstream items are "
                        "not applicable to this company")
    if draft.status == SectionStatus.NOT_APPLICABLE:  # nothing required, nothing missing
        return SectionStatus.NOT_APPLICABLE, draft.reason or "NOT_APPLICABLE", \
            SectionCompleteness(required=required, available=(), missing=(),
                                not_applicable=required)
    na = tuple(f for f in required if families[f].status == FamilyStatus.NOT_APPLICABLE
               or f in draft.not_applicable)
    missing = tuple(f for f in required if f not in na and (
        families[f].status == FamilyStatus.MISSING or f in draft.missing))
    available = tuple(f for f in required if f not in na and f not in missing)
    completeness = SectionCompleteness(required=required, available=available, missing=missing,
                                       not_applicable=na)
    if not available:
        return SectionStatus.UNAVAILABLE, "required inputs missing: " + ", ".join(
            f.value for f in missing), completeness
    if draft.status == SectionStatus.UNAVAILABLE:
        return SectionStatus.UNAVAILABLE, draft.reason or "UNAVAILABLE", completeness
    if missing or draft.status == SectionStatus.PARTIAL:
        reason = "; ".join(x for x in (
            ("missing: " + ", ".join(f.value for f in missing)) if missing else "",
            draft.reason or "") if x)
        return SectionStatus.PARTIAL, reason, completeness
    return SectionStatus.COMPLETE, "every required input family available", completeness


def _section_zero(ctx: Ctx, families: dict[InputFamily, FamilyCheck],
                  statuses: dict[SectionId, tuple[SectionStatus, str, SectionCompleteness]],
                  risk_view: RiskPresentation) -> DueDiligenceStatus:
    sec = S.DUE_DILIGENCE_STATUS
    d13 = ctx.box13
    conflicts = [d.claim_id for d in risk_view.data_limitations
                 if d.kind in ("INPUT_CONFLICT",)]
    conflicts += [ctx.book.claim(None, ClaimType.DATA_LIMITATION, "OPEN_SOURCE_CONFLICTS",
                                 f"Source disagreement remains open: {e.subject[:200]}.",
                                 basis=ClaimBasis.NOT_APPLICABLE,
                                 upstream=(up(Layer.BOX13, "DISAGREEMENT", e.entry_id),),
                                 limitations=("SOURCE_CONFLICT_UNRESOLVED",))
                  for e in d13.source_disagreements.entries if e.status == "OPEN"][:50]
    unavailable = [d.claim_id for d in risk_view.data_limitations if d.kind in
                   SECTION_ZERO_KINDS]
    unavailable += [i for i in ctx.drafts[S.DATA_QUALITY_RECONCILIATION].limits
                    if ctx.book.get(i).topic.startswith("LAYER_")]
    ctx.add(sec, *conflicts, *unavailable)
    open_items = tuple(i.item_id for i in d13.open_diligence_items
                       if i.importance.value in ("HIGH", "MEDIUM"))
    b10, b11 = ctx.box10, ctx.box11
    checked = [families[f] for f in InputFamily]
    return DueDiligenceStatus(
        input_families=tuple(checked),
        families_available=sum(1 for f in checked if f.status == FamilyStatus.AVAILABLE),
        families_checked=len(checked),
        sections_by_status=tuple((st, sum(1 for x in statuses.values() if x[0] == st))
                                 for st in SectionStatus),
        sections_total=len(SectionId), open_source_conflicts=d13.source_disagreements.open_count,
        conflict_claim_ids=tuple(dict.fromkeys(conflicts)), blocked_sources=(),
        unavailable_information_claim_ids=tuple(dict.fromkeys(unavailable)),
        open_diligence_item_ids=open_items,
        valuation_inputs_complete=b11 is not None and b11.status.value == "COMPLETE",
        valuation_status=f"Box 11 {b11.status.value}" if b11 else "Box 11 not supplied",
        forecast_inputs_complete=b10 is not None and b10.status.value == "COMPLETE",
        forecast_status=f"Box 10 {b10.status.value}" if b10 else "Box 10 not supplied")


def _pick(ctx: Ctx, section: SectionId, topics: tuple[str, ...], *, limit: int,
          latest_only: bool = False) -> list[UUID]:
    out: list[UUID] = []
    latest = ctx.box13.latest_fiscal_year_end
    for topic in topics:
        for i in ctx.drafts[section].claims:
            c = ctx.book.get(i)
            if c.topic != topic or c.basis not in (ctx.analytical, ClaimBasis.NOT_APPLICABLE):
                continue
            if latest_only and c.period_end != latest:
                continue
            out.append(i)
            break
        if len(out) >= limit:
            break
    return out


def _summary(ctx: Ctx, risk_view: RiskPresentation, monitor: list[Any], orders: OrderBookView,
             value_view: ValuationView, zero: DueDiligenceStatus) -> ExecutiveSummary:
    """Built LAST, from the claims of the completed sections only; writes no claim itself."""
    points: list[SummaryPoint] = []
    caps = {SummaryKind.WHAT_THE_COMPANY_DOES: 1, SummaryKind.OPERATING_DRIVERS: 1,
            SummaryKind.FINANCIAL_TRAJECTORY: 2, SummaryKind.STRENGTHS: 2,
            SummaryKind.BALANCE_SHEET_LIQUIDITY: 2, SummaryKind.MANAGEMENT_GUIDANCE: 1,
            SummaryKind.ORDER_BOOK: 2, SummaryKind.FORECAST_RANGE: 2,
            SummaryKind.VALUATION_RANGE: 2}

    def point(kind: SummaryKind, ids: list[UUID], reason: str,
              not_applicable: str | None = None) -> None:
        ids = list(dict.fromkeys(ids))
        if kind in caps:
            ids = ids[:caps[kind]]
        if not ids and not_applicable is not None:  # does not apply: never a data gap
            points.append(SummaryPoint(kind=kind, claim_ids=(), status="NOT_APPLICABLE",
                                       reason=f"NOT_APPLICABLE: {not_applicable}"))
            return
        points.append(SummaryPoint(kind=kind, claim_ids=tuple(ids),
                                   status="AVAILABLE" if ids else "UNAVAILABLE",
                                   reason=None if ids else reason))

    def inapplicable(section: SectionId, topics: tuple[str, ...]) -> str | None:
        """Every candidate topic is typed not applicable upstream for this company."""
        if all(t in ctx.drafts[section].inapplicable for t in topics):
            return "every candidate metric is not applicable to this company"
        return None

    point(SummaryKind.WHAT_THE_COMPANY_DOES, _pick(
        ctx, S.COMPANY_OVERVIEW, ("BUSINESS_BUSINESS_DESCRIPTION", "CLASSIFICATION",
                                  "BUSINESS_MODEL_ORDER_BOOK_DRIVEN",
                                  "BUSINESS_PRODUCT_SERVICE"), limit=1),
          "no business description upstream")
    point(SummaryKind.OPERATING_DRIVERS, _pick(
        ctx, S.BUSINESS_MODEL, ("SEGMENT_REVENUE", "BUSINESS_END_MARKET",
                                "BUSINESS_SEGMENT_DESCRIPTION", "BUSINESS_ORDER_BOOK",
                                "CAPEX_TO_REVENUE"), limit=1),
          "no operating-driver evidence upstream")
    point(SummaryKind.FINANCIAL_TRAJECTORY,
          _pick(ctx, S.HISTORICAL_FINANCIALS, ("REVENUE_CAGR_5Y", "REVENUE_CAGR_3Y"), limit=1) +
          _pick(ctx, S.HISTORICAL_FINANCIALS, ("PAT", "REVENUE_FROM_OPERATIONS", "REVENUE"),
                limit=1, latest_only=True), "no annual history")
    point(SummaryKind.STRENGTHS, [ctx.signal_claims[i] for i in risk_view.positive_controls],
          "no evidence-backed positive control upstream")
    credit = ("CREDIT_GROSS_DEBT", "CREDIT_NET_DEBT", "CREDIT_CFO", "CREDIT_DEBT_TO_EQUITY",
              "CREDIT_INTEREST_COVERAGE")
    point(SummaryKind.BALANCE_SHEET_LIQUIDITY, _pick(ctx, S.CREDIT_LIQUIDITY, credit, limit=2),
          "no balance-sheet metric available", inapplicable(S.CREDIT_LIQUIDITY, credit))
    management = _pick(ctx, S.MANAGEMENT_GOVERNANCE, ("GUIDANCE_ACCURACY",), limit=1)
    if not management:
        management = [i for i in ctx.drafts[S.MANAGEMENT_GOVERNANCE].limits
                      if ctx.book.get(i).topic == "MANAGEMENT_RELIABILITY"][:1]
    point(SummaryKind.MANAGEMENT_GUIDANCE, management, "no Box 8 / Box 13 guidance record")
    order_ids = list(orders.reported_order_book_claim_ids[:1]) + [
        b.count_claim_id for b in orders.buckets if b.count_claim_id is not None][:2]
    point(SummaryKind.ORDER_BOOK, order_ids, "order book unavailable",
          None if orders.applicable else orders.applicability)
    first = sorted({ctx.book.get(i).period_end for i in ctx.drafts[S.FORECASTS].claims
                    if ctx.book.get(i).topic == "FORECAST_REVENUE"
                    and ctx.book.get(i).period_end is not None}, key=str)
    fc = [i for sc in (Scenario.BEAR, Scenario.BULL, Scenario.BASE)
          for i in ctx.drafts[S.FORECASTS].claims
          if ctx.book.get(i).topic == "FORECAST_REVENUE" and ctx.book.get(i).scenario == sc
          and first and ctx.book.get(i).period_end == first[0]]
    point(SummaryKind.FORECAST_RANGE, fc, "no Box 10 forecast")
    vals: list[UUID] = []
    for method in dict.fromkeys(m.method for m in value_view.methods):
        found = [i for sc in (Scenario.BEAR, Scenario.BULL, Scenario.BASE)
                 for m in value_view.methods if m.method == method and m.scenario == sc
                 for i in m.result_claim_ids
                 if ctx.book.get(i).topic == "VALUATION_FAIR_VALUE_PER_SHARE"
                 and i in ctx.drafts[S.VALUATION].claims]
        if found:
            vals = found
            break
    point(SummaryKind.VALUATION_RANGE, vals, "no Box 11 method produced a per-share value")
    red = [ctx.signal_claims[i] for i in risk_view.red]
    yellow = [ctx.signal_claims[i] for i in risk_view.yellow]
    point(SummaryKind.MAJOR_RISKS, red + yellow[:5], "no YELLOW / RED company risk")
    points.append(SummaryPoint(
        kind=SummaryKind.MONITORING, claim_ids=(), status="AVAILABLE" if monitor else
        "UNAVAILABLE", reason=None if monitor else "no monitoring item",
        monitoring_item_ids=tuple(m.item_id for m in monitor[:6])))
    by_topic: dict[str, UUID] = {}
    for i in (*zero.conflict_claim_ids, *zero.unavailable_information_claim_ids):
        by_topic.setdefault(ctx.book.get(i).topic, i)
    limitation_ids = list(by_topic.values())[:6]
    point(SummaryKind.DATA_LIMITATIONS, limitation_ids, "no material data limitation")
    gaps = [p.kind.value for p in points if p.status == "UNAVAILABLE"
            and p.kind != SummaryKind.MAJOR_RISKS and p.kind != SummaryKind.DATA_LIMITATIONS]
    if gaps:
        ctx.drafts[S.EXECUTIVE_SUMMARY].status = SectionStatus.PARTIAL
        ctx.drafts[S.EXECUTIVE_SUMMARY].reason = "summary points without upstream claims: " + \
            ", ".join(gaps)
    primary = [i for p in points if p.kind in PRIMARY_KINDS for i in p.claim_ids]
    ids = [i for p in points for i in p.claim_ids]
    ctx.add(S.EXECUTIVE_SUMMARY, *ids)
    return ExecutiveSummary(
        built_after=tuple(s for s in SectionId if s != S.EXECUTIVE_SUMMARY),
        points=tuple(points), primary_claim_ids=tuple(dict.fromkeys(primary)),
        risk_claim_ids=tuple(dict.fromkeys(red + yellow[:5])),
        limitation_claim_ids=tuple(dict.fromkeys(limitation_ids)),
        yellow_risks_not_listed=max(0, len(yellow) - 5))


def _appendices(ctx: Ctx) -> list[Appendix]:
    tables = {t.code: t for t in ctx.book.tables}
    out: list[Appendix] = []
    for code, appendix_id in APPENDIX_TABLES.items():
        t = tables.get(code)
        info = ctx.appendix.get(code)
        if t is None:
            out.append(Appendix(appendix_id=appendix_id, title=appendix_id.value.replace(
                "_", " ").title(), status=SectionStatus.UNAVAILABLE, table_ids=(), claim_ids=(),
                entries=0, upstream_entries=0))
            continue
        upstream = info[2] if info else len(t.rows)
        out.append(Appendix(
            appendix_id=appendix_id, title=t.title, status=SectionStatus.COMPLETE,
            table_ids=(t.table_id,), claim_ids=t.claim_ids, entries=len(t.rows),
            upstream_entries=upstream, truncated=t.truncated or upstream > len(t.rows),
            limitation_claim_id=info[3] if info else None))
    forecast_tables = [t for t in ctx.book.tables if t.code.startswith("ASSUMPTIONS_")]
    assumption_claims = tuple(dict.fromkeys(i for t in forecast_tables for i in t.claim_ids))
    held = len(ctx.box10.assumptions) if ctx.box10 else 0
    limitation = None
    if held > len(assumption_claims):
        limitation = ctx.limit(S.DATA_QUALITY_RECONCILIATION, "APPENDIX_FORECAST_ASSUMPTIONS",
                               "APPENDIX_LIMIT_REACHED", f"{len(assumption_claims)} of {held} "
                               "Box 10 assumptions fall in the tabulated forecast years")
    out.append(Appendix(
        appendix_id=AppendixId.FORECAST_ASSUMPTIONS, title="Forecast assumptions (Box 10)",
        status=SectionStatus.COMPLETE if forecast_tables else SectionStatus.UNAVAILABLE,
        table_ids=tuple(t.table_id for t in forecast_tables), claim_ids=assumption_claims,
        entries=len(assumption_claims), upstream_entries=held,
        truncated=held > len(assumption_claims), limitation_claim_id=limitation))
    out.append(Appendix(appendix_id=AppendixId.SOURCE_REGISTER, title="Source register",
                        status=SectionStatus.COMPLETE, table_ids=(), claim_ids=(),
                        entries=ctx.book.source_count, upstream_entries=ctx.book.source_count))
    for a in out:
        if a.truncated and a.limitation_claim_id is None:
            raise _fail("RESOURCE_LIMIT_EXCEEDED", f"appendix {a.appendix_id.value} truncated "
                        "without a surfaced limitation")
    order = {a: i for i, a in enumerate(AppendixId)}
    return sorted(out, key=lambda a: order[a.appendix_id])


async def build_final_report_async(**kwargs: Any) -> FinalResearchReportDataset:
    """For async callers: the CPU work (and every hash) runs off the event loop."""
    return await asyncio.to_thread(lambda: build_final_report(**kwargs))


__all__ = [
    "ReportCompositionError",
    "build_final_report",
    "build_final_report_async",
    "canonical_report_json",
]
