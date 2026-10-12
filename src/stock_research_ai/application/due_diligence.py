"""DETAILED_RESEARCH_V1 Box 13: Boxes 3-12 + explicit cited inputs -> DueDiligenceDataset.

Pipeline:
  input policy (diligence_inputs: exact duplicates once, conflicting cited inputs resolved by
  authority or withheld, every family in canonical order; upstream record IDs unique) ->
  integrity (every input re-validates; the Box 4 / Box 5 / Box 3 / Box 10 hashes each later box
  recorded must match the datasets supplied; one company; no period after as_of; one currency;
  every cited Box 3 evidence ID exists) ->
  basis selection (consolidated = analytical, standalone = forensic / control layer) ->
  sections (forensics, earnings quality, working capital, leverage / credit, contingencies,
  ownership, governance, related parties, auditor, management reliability, exchange events,
  order book) -> standalone / consolidated reconciliation -> source disagreements -> data
  quality (DATA_LIMITATION) -> open diligence items -> mitigants, red flags, risk map.

No AI, no network, no clock: `generated_at` is injected and kept out of every ID; IDs are uuid5
over content. Inputs are never mutated. Box 13 describes risk and never recommends.
"""

import asyncio
import hashlib
import json
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from pydantic import BaseModel

from stock_research_ai.application.business_intelligence import canonical_business_json
from stock_research_ai.application.credit_risk import (
    contingent_liabilities,
    credit_profile,
    stalled_cwip,
)
from stock_research_ai.application.data_quality import (
    attach_mitigants,
    data_quality,
    open_items,
    risk_map,
    source_disagreements,
)
from stock_research_ai.application.diligence_inputs import (
    conflicted,
    normalize,
    unique_by_id,
)
from stock_research_ai.application.document_intelligence import canonical_bundle_json
from stock_research_ai.application.earnings_quality import build_forensics, positive_controls
from stock_research_ai.application.financial_analytics import (
    canonical_analytics_json,
    frame_currencies,
)
from stock_research_ai.application.financial_metrics import (
    FINANCIAL_INDUSTRIES,
    REVENUE,
    Fact,
    Frame,
    Got,
    Periods,
    shift,
)
from stock_research_ai.application.financial_reconciliation import canonical_dataset_json
from stock_research_ai.application.forecasting import canonical_forecast_json
from stock_research_ai.application.fundamental_valuation import canonical_valuation_json
from stock_research_ai.application.governance_risk import (
    MATERIAL_EVENT_PCT,
    build_auditor,
    build_governance,
    build_ownership,
    exchange_events,
    management_reliability,
    related_party,
)
from stock_research_ai.application.industry_intelligence import canonical_industry_json
from stock_research_ai.application.management_intelligence import canonical_management_json
from stock_research_ai.application.order_book_risk import build_order_book
from stock_research_ai.application.red_flags import (
    NS,
    RULES,
    Box5View,
    Ctx,
    DueDiligenceInputError,
    Ev,
    Trace,
    manifest,
    q,
)
from stock_research_ai.domain.business_intelligence import BusinessIntelligenceDataset
from stock_research_ai.domain.document_intelligence import EvidenceBundle
from stock_research_ai.domain.due_diligence import (
    DUE_DILIGENCE_ENGINE_VERSION,
    FORMULA_REGISTRY_VERSION,
    RISK_MAP_RULES_VERSION,
    RULE_REGISTRY_VERSION,
    BasisComparison,
    DiligenceSource,
    DueDiligenceDataset,
    DueDiligenceIssue,
    DueDiligenceRequest,
    ExchangeEventRisk,
    Layer,
    PeerContext,
    ReconciliationExplanation,
    ReconciliationStatus,
    SectorSpecificRisks,
    Severity,
    SignalBasis,
    SignalClass,
    SignalStatus,
    UpstreamRef,
)
from stock_research_ai.domain.financial_analytics import FinancialAnalyticsDataset, PeriodRole
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalMetric,
    Currency,
    FinancialPeriod,
    PeriodType,
    ReportingBasis,
)
from stock_research_ai.domain.forecasting import ForecastDataset
from stock_research_ai.domain.fundamental_valuation import ValuationDataset
from stock_research_ai.domain.industry_intelligence import IndustryIntelligenceDataset
from stock_research_ai.domain.management_intelligence import ManagementIntelligenceDataset
from stock_research_ai.domain.market_intelligence import Freshness, MarketIntelligenceDataset

M = CanonicalMetric
S = SignalStatus
D = Decimal
HUNDRED = D(100)
MATERIAL_BASIS_DIFFERENCE_PCT = D(10)
RECONCILIATION_YEARS = 5
SHARE_COUNT_WINDOW_DAYS = 120
PEER_CONTEXT_CODES = ("RECEIVABLE_DAYS", "CASH_CONVERSION_CYCLE", "CFO_TO_PAT",
                      "OTHER_INCOME_TO_PBT", "NET_DEBT_TO_EBITDA", "DEBT_TO_EQUITY",
                      "INTEREST_COVERAGE")

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_market_json(dataset: MarketIntelligenceDataset) -> str:
    return json.dumps(dataset.model_dump(mode="json", exclude={"generated_at"}), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)


def canonical_due_diligence_json(dataset: DueDiligenceDataset) -> str:
    """Deterministic serialization; generated_at is excluded so equal inputs compare equal."""
    return json.dumps(dataset.model_dump(mode="json", exclude={"generated_at"}), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)


def _revalidate[T_: BaseModel](value: T_ | None) -> T_ | None:
    """A dataset built with model_construct (or edited) must still satisfy its own contract."""
    if value is None:
        return None
    return type(value).model_validate(value.model_dump())


def _sources(request: DueDiligenceRequest) -> Iterable[DiligenceSource]:
    for name, field_value in request:
        if isinstance(field_value, tuple):
            for item in field_value:
                source = getattr(item, "source", None)
                if isinstance(source, DiligenceSource):
                    yield source
        else:
            source = getattr(field_value, "source", None)
            if isinstance(source, DiligenceSource):
                yield source
        if name == "business_model_source" and isinstance(field_value, DiligenceSource):
            yield field_value


def _money_currencies(request: DueDiligenceRequest) -> set[Currency]:
    out: set[Currency] = set()
    for _, field_value in request:
        items = field_value if isinstance(field_value, tuple) else (field_value,)
        for item in items:
            currency = getattr(item, "currency", None)
            if isinstance(currency, Currency):
                out.add(currency)
    return out


def _check_lineage(
    request: DueDiligenceRequest,
    box4: CanonicalFinancialDataset,
    box5: FinancialAnalyticsDataset,
    box4_sha: str,
    box5_sha: str,
    bundle: EvidenceBundle | None,
    bundle_sha: str | None,
    box7: BusinessIntelligenceDataset | None,
    box8: ManagementIntelligenceDataset | None,
    box9: IndustryIntelligenceDataset | None,
    box10: ForecastDataset | None,
    box10_sha: str | None,
    box11: ValuationDataset | None,
    box12: MarketIntelligenceDataset | None,
) -> None:
    def fail(code: str, detail: str) -> None:
        raise DueDiligenceInputError(code, detail)

    key = box4.company_key
    if request.company_key != key:
        fail("COMPANY_MISMATCH", "the request names another company than Box 4")
    for name, other in (("Box 5", box5), ("Box 3", bundle), ("Box 7", box7), ("Box 8", box8),
                        ("Box 9", box9), ("Box 10", box10), ("Box 11", box11)):
        if other is not None and getattr(other, "company_key", key) != key:
            fail("COMPANY_MISMATCH", f"the {name} dataset belongs to another company")
    if box5.source_box4.canonical_sha256 != box4_sha or box5.source_box4.dataset_id != \
            box4.dataset_id:
        fail("BOX4_MISMATCH", "Box 5 was not built from this Box 4 dataset")
    if box7 is not None and box7.source_box4.sha256 != box4_sha:
        fail("BOX4_MISMATCH", "Box 7 was not built from this Box 4 dataset")
    if box8 is not None:
        if box8.source_box4.sha256 != box4_sha:
            fail("BOX4_MISMATCH", "Box 8 was not built from this Box 4 dataset")
        if box8.source_box5 is not None and box8.source_box5.sha256 != box5_sha:
            fail("BOX5_MISMATCH", "Box 8 was not built from this Box 5 dataset")
        if bundle_sha is not None and box8.source_box3.sha256 != bundle_sha:
            fail("BOX3_MISMATCH", "Box 8 was not built from this Box 3 bundle")
        canonical = {v.canonical_value_id for v in box4.canonical_values}
        metrics = {m.metric_id for m in box5.all_metrics()}
        for o in box8.outcomes:
            actual = o.actual
            if actual is None:
                continue
            if (actual.layer.value == "BOX4_METRIC" and actual.record_id not in canonical) or (
                actual.layer.value == "BOX5_METRIC" and box8.source_box5 is not None
                and actual.record_id not in metrics
            ):
                fail("BROKEN_REFERENCE", f"Box 8 outcome {o.outcome_id} cites a record that is "
                     "not in the supplied Box 4 / Box 5 datasets")
    if box9 is not None:
        own = [s for s in box9.sources if s.company_key == key]
        for s in own:
            if s.layer == "BOX4" and s.sha256 != box4_sha:
                fail("BOX4_MISMATCH", "Box 9 was not built from this Box 4 dataset")
            if s.layer == "BOX5" and s.sha256 != box5_sha:
                fail("BOX5_MISMATCH", "Box 9 was not built from this Box 5 dataset")
    if box10 is not None and box10.source_box4.canonical_sha256 != box4_sha:
        fail("BOX4_MISMATCH", "Box 10 was not built from this Box 4 dataset")
    if box11 is not None:
        if box11.source_forecast.box4_canonical_sha256 != box4_sha:
            fail("BOX4_MISMATCH", "Box 11 was not built from this Box 4 dataset")
        if box10_sha is not None and box11.source_forecast.canonical_sha256 != box10_sha:
            fail("BOX10_MISMATCH", "Box 11 was not built from this Box 10 forecast")
    if box12 is not None:
        identity = box12.identity
        if not identity.usable:
            fail("IDENTITY_UNUSABLE", f"Box 12 identity is {identity.status.value}")
        if request.nse_symbol is None and request.isin is None:
            fail("IDENTITY_UNBOUND", "Box 12 needs the request's NSE symbol or ISIN")
        declared = {
            "nse": (request.nse_symbol, identity.nse_symbol, identity.declared_nse_symbol),
            "isin": (request.isin, identity.isin, identity.declared_isin),
        }
        bound = False
        for name, (asked, verified, stated) in declared.items():
            if asked is None:
                continue
            if verified is not None and verified != asked:
                fail("IDENTITY_MISMATCH", f"Box 12 verified {name} {verified} != {asked}")
            if verified == asked or stated == asked:
                bound = True
        if not bound:
            fail("IDENTITY_MISMATCH", "Box 12 is for another security")
        if box12.as_of.date() > request.as_of:
            fail("INCOMPATIBLE_PERIOD", "Box 12 data is dated after as_of")
    late = [v for v in box4.canonical_values if v.period.period_end is not None
            and v.period.period_end > request.as_of]
    if late:
        fail("INCOMPATIBLE_PERIOD", f"{len(late)} Box 4 values end after as_of {request.as_of}")


def _evidence(request: DueDiligenceRequest, bundle: EvidenceBundle | None) -> tuple[
        dict[UUID, tuple[UUID, str, int]], int]:
    cited = {e for s in _sources(request) for e in s.evidence_ids}
    if bundle is None:
        return {}, len(cited)
    index = {
        item.evidence_id: (item.document_id, f"{x.artifact_reference}#{item.locator}",
                           item.page_start)
        for x in bundle.extractions for item in x.evidence
    }
    broken = sorted(str(e) for e in cited - set(index))
    if broken:
        raise DueDiligenceInputError("BROKEN_EVIDENCE_REFERENCE",
                                     f"{len(broken)} cited evidence IDs are not in the Box 3 "
                                     f"bundle (first: {broken[0]})")
    return index, 0


def _latest(box5: FinancialAnalyticsDataset, basis: ReportingBasis) -> date | None:
    for p in box5.periods:
        if p.role == PeriodRole.LATEST_FY and p.reporting_basis == basis and p.period:
            return p.period.period_end
    return None


def _market_cap(ctx: Ctx, box12: MarketIntelligenceDataset | None) -> tuple[Decimal, UUID,
                                                                             str] | None:
    r = ctx.request
    if r.market_cap is not None and r.market_cap.currency == ctx.currency:
        return r.market_cap.value_crore, uuid5(NS, f"input|market_cap|"
                                               f"{r.market_cap.model_dump_json()}"), "CITED"
    snap = box12.snapshot if box12 is not None else None
    if snap is None or ctx.currency != Currency.INR or snap.market_date is None or \
            snap.freshness in (Freshness.STALE, Freshness.UNKNOWN):
        return None
    day = snap.market_date
    counts = [c for c in r.share_counts if abs((c.as_of - day).days) <= SHARE_COUNT_WINDOW_DAYS]
    if not counts or conflicted(ctx, "share_counts", lambda c: abs(
            (c.as_of - day).days) <= SHARE_COUNT_WINDOW_DAYS):
        return None
    count = min(counts, key=lambda c: (abs((c.as_of - day).days), c.as_of))
    return q(snap.last_price * count.shares / D(10_000_000)), snap.snapshot_id, \
        "BOX12_PRICE_X_CITED_SHARES"


# --- standalone / consolidated reconciliation ---------------------------------------------------

Item = tuple[str, str, str, tuple[UUID, ...]]
EXPECTED_DIFFERENCE = {"MINORITY_INTEREST", "SHARE_OF_ASSOCIATES_JV"}


def _compare(ctx: Ctx) -> tuple[list[BasisComparison], list[tuple[str, str, str,
                                                                   tuple[UUID, ...]]], UUID]:
    out: list[BasisComparison] = []
    extra: list[tuple[str, str, str, tuple[UUID, ...]]] = []
    trace = Trace()
    unexplained = 0
    latest = ctx.latest.get(ReportingBasis.CONSOLIDATED)
    standalone_latest = ctx.latest.get(ReportingBasis.STANDALONE)
    if ctx.control is None or latest is None or standalone_latest is None:
        rid = ctx.emit("UNEXPLAINED_BASIS_DIFFERENCE", Ev(
            S.UNAVAILABLE, SignalBasis.NOT_APPLICABLE, reason="ONE_BASIS_ONLY",
            explanation="standalone and consolidated statements are not both available"))
        return out, extra, rid
    explanations = {(e.metric, e.fiscal_year): e for e in ctx.request.reconciliation_explanations}
    reviewed = {r.fiscal_year for r in ctx.request.reconciliation_reviews}
    same_currency = ctx.frames[ReportingBasis.STANDALONE].currency == ctx.currency
    for k in range(RECONCILIATION_YEARS):
        end = shift(latest, -12 * k)
        p, b = ctx.periods.fy(end), ctx.periods.pit(end)
        rows: list[tuple[str, FinancialPeriod, Any]] = [
            ("REVENUE", p, REVENUE), ("PAT", p, (M.PAT,)), ("TOTAL_EQUITY", b, (M.TOTAL_EQUITY,)),
            ("NET_WORTH", b, "SHAREHOLDERS_EQUITY"), ("GROSS_DEBT", b, "GROSS_DEBT"),
            ("CASH", b, (M.CASH_AND_CASH_EQUIVALENTS,)), ("MINORITY_INTEREST", p,
                                                          (M.MINORITY_INTEREST,)),
            ("SHARE_OF_ASSOCIATES_JV", p, (M.SHARE_OF_ASSOCIATES_JV,)),
        ]
        for code, period, source in rows:
            cons, stand = (_basis_value(ctx, source, period, basis)
                           for basis in (ReportingBasis.CONSOLIDATED, ReportingBasis.STANDALONE))
            out.append(_comparison(ctx, code, period, cons, stand, same_currency, explanations,
                                   reviewed, extra))
            last = out[-1]
            if k == 0 and last.status == ReconciliationStatus.MATERIAL_UNEXPLAINED:
                unexplained += 1
                for basis, got in ((ReportingBasis.CONSOLIDATED, cons),
                                   (ReportingBasis.STANDALONE, stand)):
                    ctx.add(trace, f"{code} {basis.value.lower()}", got, "MONEY",
                            Layer.BOX4, basis)
        out.extend(_explicit_comparisons(ctx, end, explanations, reviewed, extra) if k == 0
                   else [])
    p0 = ctx.periods.fy(latest)
    if not ctx.request.reconciliation_reviews or p0.fiscal_year not in reviewed:
        ev = Ev(S.UNAVAILABLE, SignalBasis.NOT_APPLICABLE, reason="SOURCES_NOT_REVIEWED",
                period_label=p0.label, period_end=p0.period_end,
                explanation="no recorded review of the sources that explain standalone / "
                "consolidated differences for the latest year")
    else:
        review = next(r for r in ctx.request.reconciliation_reviews
                      if r.fiscal_year == p0.fiscal_year)
        ctx.explicit(trace, "source review", D(len(review.sources_checked)), "SOURCES",
                     uuid5(NS, f"input|review|{review.model_dump_json()}"), review.source)
        ev = Ev(S.EVALUATED, SignalBasis.NOT_APPLICABLE,
                Severity.YELLOW if unexplained else Severity.GREEN, D(unexplained),
                period_label=p0.label, period_end=p0.period_end, trace=trace,
                calculation=f"{unexplained} material unexplained differences",
                explanation=f"{unexplained} material standalone / consolidated differences "
                "remain unexplained after the recorded source review")
    rid = ctx.emit("UNEXPLAINED_BASIS_DIFFERENCE", ev)
    return out, extra, rid


def _basis_value(ctx: Ctx, source: Any, period: FinancialPeriod, basis: ReportingBasis) -> Got:
    if isinstance(source, str):
        return ctx.m5(source, period, basis)
    return ctx.canonical(source, period, basis)


def _comparison(ctx: Ctx, code: str, period: FinancialPeriod, cons: Got, stand: Got,
                same_currency: bool, explanations: dict[tuple[str, int], ReconciliationExplanation],
                reviewed: set[int], extra: list[Item]) -> BasisComparison:
    records = tuple(sorted({i.canonical_value_id for g in (cons, stand) for i in g.inputs}
                           | {d for g in (cons, stand) for d in g.derived_from}, key=str))
    cv = cons.value if isinstance(cons, Fact) else None
    sv = stand.value if isinstance(stand, Fact) else None
    common: dict[str, Any] = {
        "comparison_id": uuid5(NS, f"{ctx.dd_key}|basis|{code}|{period.key()}"),
        "metric": code, "period": period.label, "period_end": period.period_end,
        "standalone_value": q(sv) if sv is not None else None,
        "consolidated_value": q(cv) if cv is not None else None, "record_ids": records,
    }
    year = period.fiscal_year or (period.period_end.year if period.period_end else 0)
    explanation = explanations.get((code, year))
    if code in EXPECTED_DIFFERENCE and cv is not None:
        return BasisComparison(**common, absolute_difference=q(cv - (sv or D(0)))
                               if sv is not None else None, percentage_difference=None,
                               explanation="consolidation-only line (no standalone equivalent)",
                               explanation_record_id=None,
                               status=ReconciliationStatus.EXPECTED_GROUP_DIFFERENCE,
                               reason="EXPECTED_BY_CONSTRUCTION")
    if cv is None or sv is None:
        return BasisComparison(**common, absolute_difference=None, percentage_difference=None,
                               explanation=None, explanation_record_id=None,
                               status=ReconciliationStatus.UNAVAILABLE,
                               reason="ONE_OR_BOTH_BASES_UNAVAILABLE")
    if not same_currency:
        return BasisComparison(**common, absolute_difference=None, percentage_difference=None,
                               explanation=None, explanation_record_id=None,
                               status=ReconciliationStatus.NOT_COMPARABLE,
                               reason="CURRENCY_DIFFERS_BETWEEN_BASES")
    diff = q(cv - sv)
    if cv == 0:
        status = ReconciliationStatus.IMMATERIAL if diff == 0 else \
            ReconciliationStatus.NOT_COMPARABLE
        return BasisComparison(**common, absolute_difference=diff, percentage_difference=None,
                               explanation=None, explanation_record_id=None, status=status,
                               reason="ZERO_CONSOLIDATED_BASE")
    pct = q(abs(diff) * HUNDRED / abs(cv))
    if pct <= MATERIAL_BASIS_DIFFERENCE_PCT:
        status, reason = ReconciliationStatus.IMMATERIAL, "WITHIN_10_PERCENT"
    elif explanation is not None:
        status = (ReconciliationStatus.EXPECTED_GROUP_DIFFERENCE
                  if explanation.kind == "GROUP_STRUCTURE"
                  else ReconciliationStatus.MATERIAL_EXPLAINED)
        reason = "EXPLAINED_BY_CITED_SOURCE"
    elif year in reviewed:
        status, reason = ReconciliationStatus.MATERIAL_UNEXPLAINED, "REVIEWED_NOT_EXPLAINED"
    else:
        status, reason = ReconciliationStatus.MATERIAL_NOT_REVIEWED, "SOURCES_NOT_YET_CHECKED"
        extra.append((f"Why does {code} differ between standalone and consolidated in "
                      f"{period.label}?", f"{pct} % difference, explaining sources not yet "
                      "checked", "Consolidation notes: subsidiaries, JVs, eliminations", ()))
    return BasisComparison(
        **common, absolute_difference=diff, percentage_difference=pct,
        explanation=explanation.explanation if explanation else None,
        explanation_record_id=uuid5(NS, f"input|explanation|{explanation.model_dump_json()}")
        if explanation else None, status=status, reason=reason)


def _explicit_comparisons(
    ctx: Ctx, end: date, explanations: dict[tuple[str, int], ReconciliationExplanation],
    reviewed: set[int], extra: list[Item],
) -> list[BasisComparison]:
    """Contingent liabilities and related-party balances stated on both bases."""
    out: list[BasisComparison] = []
    period = ctx.periods.pit(end)
    def balance(r: Any) -> bool:
        return bool(r.fiscal_year == period.fiscal_year and r.kind.value in (
            "BALANCE_RECEIVABLE", "BALANCE_PAYABLE"))

    for code, rows, disputed in (
        ("ORDER_BOOK", [(r.basis, r.value_crore, r) for r in ctx.request.order_book_reports
                        if r.as_of == end],
         conflicted(ctx, "order_book_reports", lambda r: r.as_of == end)),
        ("CONTINGENT_LIABILITIES", [(c.basis, c.amount_crore, c) for c in
                                   ctx.request.contingent_liabilities
                                   if c.as_of == end and c.category.value == "TOTAL"],
         conflicted(ctx, "contingent_liabilities",
                    lambda c: c.as_of == end and c.category.value == "TOTAL")),
        ("RELATED_PARTY_BALANCES", [(r.basis, r.amount_crore, r) for r in
                                   ctx.request.related_party if balance(r)],
         conflicted(ctx, "related_party", balance)),
    ):
        if disputed:  # a disputed line never enters a comparison total
            continue
        totals: dict[str, Decimal] = {}
        records: list[UUID] = []
        for basis, amount, item in rows:
            totals[basis] = totals.get(basis, D(0)) + amount
            records.append(uuid5(NS, f"input|{code.lower()}|{item.model_dump_json()}"))
        if not totals:
            continue
        cons, stand = totals.get("CONSOLIDATED"), totals.get("STANDALONE")
        if cons is None or stand is None:
            continue
        diff = q(cons - stand)
        pct = q(abs(diff) * HUNDRED / cons) if cons else None
        explanation = explanations.get((code, period.fiscal_year or end.year))
        if pct is None or pct <= MATERIAL_BASIS_DIFFERENCE_PCT:
            status, reason = ReconciliationStatus.IMMATERIAL, "WITHIN_10_PERCENT"
        elif explanation is not None:
            status, reason = ReconciliationStatus.MATERIAL_EXPLAINED, "EXPLAINED_BY_CITED_SOURCE"
        elif (period.fiscal_year or end.year) in reviewed:
            status, reason = ReconciliationStatus.MATERIAL_UNEXPLAINED, "REVIEWED_NOT_EXPLAINED"
        else:
            status, reason = ReconciliationStatus.MATERIAL_NOT_REVIEWED, "SOURCES_NOT_YET_CHECKED"
        out.append(BasisComparison(
            comparison_id=uuid5(NS, f"{ctx.dd_key}|basis|{code}|{end}"), metric=code,
            period=str(end), period_end=end, standalone_value=q(stand), consolidated_value=q(cons),
            absolute_difference=diff, percentage_difference=pct,
            explanation=explanation.explanation if explanation else None,
            explanation_record_id=None, status=status,
            record_ids=tuple(sorted(records, key=str)), reason=reason))
    return out


# --- sector context ---------------------------------------------------------------------------


def _peer_context(ctx: Ctx, box9: IndustryIntelligenceDataset | None) -> list[PeerContext]:
    if box9 is None:
        return []
    out: list[PeerContext] = []
    for code in PEER_CONTEXT_CODES:
        rows = [b for b in box9.financial_benchmarks
                if b.metric_code == code and b.reporting_basis == ctx.analytical]
        if not rows:
            continue
        b = max(rows, key=lambda r: (r.period_end, str(r.benchmark_id)))
        out.append(PeerContext(
            metric_code=code, period_end=b.period_end, company_value=b.target.value,
            peer_count=b.statistics.peer_count, peer_median=b.statistics.median,
            peer_minimum=b.statistics.minimum, peer_maximum=b.statistics.maximum,
            benchmark_id=b.benchmark_id))
    return out


# --- builder ----------------------------------------------------------------------------------


def build_due_diligence(
    box4: CanonicalFinancialDataset,
    box5: FinancialAnalyticsDataset,
    *,
    request: DueDiligenceRequest,
    generated_at: datetime,
    bundle: EvidenceBundle | None = None,
    box7: BusinessIntelligenceDataset | None = None,
    box8: ManagementIntelligenceDataset | None = None,
    box9: IndustryIntelligenceDataset | None = None,
    box10: ForecastDataset | None = None,
    box11: ValuationDataset | None = None,
    box12: MarketIntelligenceDataset | None = None,
) -> DueDiligenceDataset:
    """Deterministic Box 13. Raises DueDiligenceInputError when the inputs cannot be combined."""
    request = DueDiligenceRequest.model_validate(request.model_dump())
    box4 = CanonicalFinancialDataset.model_validate(box4.model_dump())
    box5 = FinancialAnalyticsDataset.model_validate(box5.model_dump())
    bundle, box7, box8, box9 = (_revalidate(bundle), _revalidate(box7), _revalidate(box8),
                                _revalidate(box9))
    box10, box11, box12 = _revalidate(box10), _revalidate(box11), _revalidate(box12)
    box4_sha = _sha(canonical_dataset_json(box4))
    box5_sha = _sha(canonical_analytics_json(box5))
    bundle_sha = _sha(canonical_bundle_json(bundle)) if bundle else None
    box10_sha = _sha(canonical_forecast_json(box10)) if box10 else None
    _check_lineage(request, box4, box5, box4_sha, box5_sha, bundle, bundle_sha, box7, box8, box9,
                   box10, box10_sha, box11, box12)
    currencies = frame_currencies(box4)
    if ReportingBasis.CONSOLIDATED in currencies:
        analytical = ReportingBasis.CONSOLIDATED
        selection = "CONSOLIDATED: the primary analytical basis"
    elif ReportingBasis.STANDALONE in currencies:
        analytical = ReportingBasis.STANDALONE
        selection = "STANDALONE: Box 4 holds no consolidated statements"
    else:
        raise DueDiligenceInputError("NO_FINANCIAL_BASIS", "Box 4 has no stated basis")
    currency = currencies[analytical]
    if currency in (Currency.UNKNOWN, Currency.OTHER):
        raise DueDiligenceInputError("CURRENCY_UNKNOWN", "the analytical currency is not known")
    for frame in box5.frames:
        if currencies.get(frame.reporting_basis) != frame.currency:
            raise DueDiligenceInputError("BOX5_MISMATCH", "Box 5 frames disagree with Box 4")
    foreign = _money_currencies(request) - {currency}
    if foreign:
        raise DueDiligenceInputError(
            "CURRENCY_MISMATCH", f"explicit inputs in {sorted(c.value for c in foreign)}; the "
            f"analytical currency is {currency.value}")
    evidence_index, unverified = _evidence(request, bundle)
    if box12 is not None:
        box12 = box12.model_copy(update={
            "shareholding": unique_by_id(box12.shareholding, "filing_id",
                                         "Box 12 shareholding filing"),
            "insider_trades": unique_by_id(box12.insider_trades, "record_id",
                                           "Box 12 insider trade"),
            "corporate_actions": unique_by_id(box12.corporate_actions, "action_id",
                                              "Box 12 corporate action"),
            "filings": unique_by_id(box12.filings, "document_id", "Box 12 filing"),
        })
    known_events = {e.event_id for e in box12.events} if box12 is not None else set()
    if any(a.event_id not in known_events for a in request.event_assessments):
        raise DueDiligenceInputError("BROKEN_REFERENCE", "an event assessment cites an event "
                                     "that is not in the supplied Box 12 dataset")
    inputs = normalize(request)
    control = ReportingBasis.STANDALONE if (
        analytical == ReportingBasis.CONSOLIDATED and ReportingBasis.STANDALONE in currencies
    ) else None
    frames: dict[ReportingBasis, Frame] = {
        b: Frame(box4, b, c, box5.limits.max_canonical_values) for b, c in currencies.items()
        if b in (ReportingBasis.CONSOLIDATED, ReportingBasis.STANDALONE)
    }
    shas = {
        Layer.BOX3: bundle_sha,
        Layer.BOX7: _sha(canonical_business_json(box7)) if box7 else None,
        Layer.BOX8: _sha(canonical_management_json(box8)) if box8 else None,
        Layer.BOX9: _sha(canonical_industry_json(box9)) if box9 else None,
        Layer.BOX10: box10_sha,
        Layer.BOX11: _sha(canonical_valuation_json(box11)) if box11 else None,
        Layer.BOX12: _sha(canonical_market_json(box12)) if box12 else None,
    }
    dd_key = (
        f"{box4_sha}|{box5_sha}|{'|'.join(f'{k.value}:{v or "-"}' for k, v in shas.items())}|"
        f"{inputs.echo.model_dump_json()}|{DUE_DILIGENCE_ENGINE_VERSION}|{RULE_REGISTRY_VERSION}|"
        f"{FORMULA_REGISTRY_VERSION}|{RISK_MAP_RULES_VERSION}"
    )
    ctx = Ctx(
        box4=box4, box5=box5, request=inputs.request, frames=frames, currency=currency,
        analytical=analytical, control=control, periods=Periods(box4.fiscal_year_end_month),
        latest={b: _latest(box5, b) for b in frames}, financial=box4.industry in
        FINANCIAL_INDUSTRIES, b5=Box5View(box5),
        obs_source={o.observation_id: f"{o.source.source_url}#{o.source.locator}"[:1000]
                    for o in box4.observations},
        dd_key=dd_key, evidence_index=evidence_index, inputs=inputs,
    )
    latest = ctx.latest.get(analytical)
    market_cap = _market_cap(ctx, box12)
    forensics, earnings, working, accounting, balance, leverage = build_forensics(
        ctx, latest, market_cap)
    controls = positive_controls(ctx, latest)
    credit, _, _ = credit_profile(ctx, box12, bundle)
    contingent, contingent_ev = contingent_liabilities(ctx)
    contingent_id = ctx.emit("CONTINGENT_LIABILITY", contingent_ev)
    cwip_id = ctx.emit("STALLED_CWIP", stalled_cwip(ctx))
    ownership, _ = build_ownership(ctx, box12)
    governance, _ = build_governance(ctx, box12)
    related, _ = related_party(ctx)
    auditor, _ = build_auditor(ctx, bundle)
    reliability, _ = management_reliability(ctx, box8)
    events, _ = exchange_events(ctx, box12, market_cap[0] if market_cap else None)
    orders, _ = build_order_book(ctx, box12, box7)
    comparisons, extra, basis_id = _compare(ctx)
    disagreements = source_disagreements(ctx, box4, box12)
    peers = _peer_context(ctx, box9)
    # --- assembly ------------------------------------------------------------------------------
    signals = attach_mitigants(ctx.signals)
    ctx.signals = signals
    flags = sorted(
        (s for s in signals if s.signal_class == SignalClass.COMPANY_RISK
         and s.severity in (Severity.YELLOW, Severity.RED)),
        key=lambda s: (0 if s.severity == Severity.RED else 1, s.category.value, s.code,
                       str(s.risk_id)))
    quality = data_quality(ctx, box12=box12, box8=box8, box10=box10, box11=box11, bundle=bundle,
                           disagreements=disagreements, unverified_evidence=unverified,
                           order_book=orders)
    extra.extend(_extra_items(ctx, box12, contingent.total_method == "STATED_TOTAL"
                              and len(contingent.by_category) == 1, events.entries))
    items = open_items(ctx, box12=box12, extra=extra)
    present = ctx.present

    def group(*codes: str) -> tuple[UUID, ...]:
        return tuple(controls[c] for c in codes if c in controls)

    earnings = earnings.model_copy(update={"signal_ids": earnings.signal_ids + present(
        group("STRONG_CASH_CONVERSION", "POSITIVE_FCF_HISTORY"))})
    working = working.model_copy(update={"signal_ids": working.signal_ids + present(
        group("IMPROVING_DEBTOR_DAYS"))})
    credit = credit.model_copy(update={"signal_ids": credit.signal_ids + present(leverage) +
                                       present(group("NET_CASH", "STRONG_INTEREST_COVER",
                                                     "DECLINING_LEVERAGE"))})
    contingent = contingent.model_copy(update={"signal_ids": present([contingent_id])})
    sector = SectorSpecificRisks(
        sector_gate=ctx.gate, business_model=request.business_model,
        gated_rules=tuple(sorted(ctx.gated)), peer_context=tuple(peers), signal_ids=())
    issues = list(ctx.issues)
    if ctx.financial:
        issues.append(DueDiligenceIssue(
            code="FINANCIAL_SECTOR_RULES_UNSUPPORTED", severity="INFO",
            detail="bank / NBFC / insurer: industrial rules are UNSUPPORTED_SECTOR_RULE; no "
            "sector-specific financial-company rule set exists yet"))
    return DueDiligenceDataset(
        dataset_id=uuid5(NS, dd_key),
        company_key=box4.company_key,
        company_name=box4.company_name,
        industry=box4.industry,
        sector_gate=ctx.gate,
        business_model=request.business_model,
        analytical_basis=SignalBasis(analytical.value),
        basis_selection=selection,
        control_basis=SignalBasis(control.value) if control else None,
        currency=currency,
        as_of=request.as_of,
        latest_fiscal_year_end=latest,
        generated_at=generated_at,
        sources=tuple(_upstream(box4, box4_sha, box5, box5_sha, bundle, box7, box8, box9, box10,
                                box11, box12, shas)),
        request=inputs.echo,
        financial_forensics=forensics,
        earnings_quality=earnings,
        accounting_risks=tuple(accounting) + present([cwip_id, basis_id]),
        balance_sheet_risks=tuple(balance),
        liquidity_credit_risks=credit,
        working_capital_risks=working,
        governance_risks=governance,
        management_reliability=reliability,
        ownership_risks=ownership,
        exchange_event_risks=events,
        order_book_quality=orders,
        contingent_liabilities=contingent,
        related_party_risks=related,
        auditor_signals=auditor,
        standalone_consolidated_reconciliation=tuple(comparisons),
        source_disagreements=disagreements,
        red_flags=tuple(s.risk_id for s in flags),
        positive_controls=tuple(s.risk_id for s in signals
                                if s.signal_class == SignalClass.POSITIVE_CONTROL),
        risk_map=tuple(risk_map(ctx, flags)),
        open_diligence_items=tuple(items),
        data_quality_summary=quality,
        sector_specific_risks=sector,
        signals=tuple(signals),
        issues=tuple(issues),
        manifest=manifest(),
    )


def _extra_items(ctx: Ctx, box12: MarketIntelligenceDataset | None, total_only: bool,
                 events: Iterable[ExchangeEventRisk] = ()) -> list[tuple[
                     str, str, str, tuple[UUID, ...]]]:
    out: list[tuple[str, str, str, tuple[UUID, ...]]] = []
    for e in events:
        if e.direction in ("NOT_ESTABLISHED", "CONFLICTING") and e.materiality_pct is not None \
                and e.materiality_pct >= MATERIAL_EVENT_PCT:
            out.append((f"Is the {e.event_type.lower()} filed on {e.event_date} adverse to the "
                        f"company? ({e.title[:200]})", f"stated value = {e.materiality_pct} % of "
                        f"reference revenue; direction {e.direction.lower().replace('_', ' ')} "
                        "(an event category or its size never establishes it)",
                        "The order / award / notice itself (cite it as an event assessment)",
                        ()))
    latest = ctx.latest.get(ctx.analytical)
    frame = ctx.frames[ctx.analytical]
    quarters = [d for d in frame.periods(PeriodType.QUARTER) if latest and d > latest]
    if quarters:
        last = max(quarters)
        cfo = ctx.canonical([M.CFO], ctx.periods.quarter(last))
        if not isinstance(cfo, Fact):
            out.append(("What was operating cash flow in the latest quarter?",
                        f"no cash-flow statement for the quarter ended {last}",
                        "Half-yearly / quarterly cash-flow statement", ()))
    flagged = [s for s in ctx.signals if s.code in ("RISING_DEBTOR_DAYS",
                                                    "RECEIVABLES_OUTRUNNING_SALES")
               and s.severity in (Severity.YELLOW, Severity.RED)]
    if flagged and not any(w.item.value == "RECEIVABLES_OVER_6_MONTHS"
                           for w in ctx.request.working_capital):
        out.append(("How old are trade receivables?", "receivables flagged; ageing not "
                    "supplied", "Annual report trade-receivables ageing schedule",
                    tuple(s.risk_id for s in flagged)))
    if total_only:
        out.append(("What cases make up the contingent liabilities?", "only a total is "
                    "supplied", "Annual report contingent-liabilities note (case detail)", ()))
    if box12 is not None:
        unresolved = [c for c in box12.order_chains if c.l1_linkage is not None
                      and c.l1_linkage.resolution.value in ("UNRESOLVED_LINKAGE",
                                                            "SOURCE_INCOMPLETE")]
        if unresolved:
            out.append((f"Were {len(unresolved)} L1 bids later awarded, lost or cancelled?",
                        "award linkage unresolved from exchange evidence (never counted as "
                        "backlog)", "LOA filings with tender / letter references", ()))
        for c in box12.order_chains:
            if c.status.value == "UNLINKED_CANCELLATION":
                out.append((f"Which order does the cancellation of {c.first_announced} "
                            f"({(c.client or 'client not stated')[:120]}) cancel?",
                            "the cancellation could not be linked to an order in the ledger",
                            "The cancellation filing and the original LOA", ()))
        rating_events = [e for e in box12.events if e.event_type.value == "CREDIT_RATING"]
        if rating_events and not ctx.request.ratings:
            out.append(("What do the rating reports say (rating, outlook, sensitivities)?",
                        f"{len(rating_events)} rating announcements without parsed grades",
                        "Rating-agency rationale documents", ()))
    if any(v.reporting_basis == ReportingBasis.UNKNOWN for v in ctx.box4.canonical_values):
        out.append(("Which basis (standalone / consolidated) do the unlabelled values use?",
                    "Box 4 values without a stated basis are excluded",
                    "The source filing's statement headings", ()))
    return out


def _upstream(box4: CanonicalFinancialDataset, box4_sha: str, box5: FinancialAnalyticsDataset,
              box5_sha: str, bundle: EvidenceBundle | None,
              box7: BusinessIntelligenceDataset | None,
              box8: ManagementIntelligenceDataset | None,
              box9: IndustryIntelligenceDataset | None, box10: ForecastDataset | None,
              box11: ValuationDataset | None, box12: MarketIntelligenceDataset | None,
              shas: dict[Layer, str | None]) -> list[UpstreamRef]:
    out = [
        UpstreamRef(layer=Layer.BOX4, dataset_id=box4.dataset_id, sha256=box4_sha,
                    schema_version=box4.schema_version, status=box4.status),
        UpstreamRef(layer=Layer.BOX5, dataset_id=box5.analytics_id, sha256=box5_sha,
                    schema_version=box5.schema_version, status=box5.status),
    ]
    for layer, ds, ident, schema, status in (
        (Layer.BOX3, bundle, bundle.bundle_id if bundle else None,
         bundle.schema_version if bundle else "", "SUPPLIED"),
        (Layer.BOX7, box7, box7.dataset_id if box7 else None,
         box7.schema_version if box7 else "", box7.status if box7 else ""),
        (Layer.BOX8, box8, box8.dataset_id if box8 else None,
         box8.schema_version if box8 else "", box8.status if box8 else ""),
        (Layer.BOX9, box9, box9.dataset_id if box9 else None,
         box9.schema_version if box9 else "", box9.status if box9 else ""),
        (Layer.BOX10, box10, box10.forecast_id if box10 else None,
         box10.schema_version if box10 else "", box10.status.value if box10 else ""),
        (Layer.BOX11, box11, box11.valuation_id if box11 else None,
         box11.schema_version if box11 else "", box11.status.value if box11 else ""),
        (Layer.BOX12, box12, box12.dataset_id if box12 else None,
         box12.schema_version if box12 else "", box12.identity.status.value if box12 else ""),
    ):
        sha = shas.get(layer)
        if ds is not None and sha is not None:
            out.append(UpstreamRef(layer=layer, dataset_id=ident, sha256=sha,
                                   schema_version=schema, status=status))
    return out


async def build_due_diligence_async(
    box4: CanonicalFinancialDataset,
    box5: FinancialAnalyticsDataset,
    **kwargs: Any,
) -> DueDiligenceDataset:
    """For async callers: the CPU work (and every hash) runs off the event loop."""
    return await asyncio.to_thread(build_due_diligence, box4, box5, **kwargs)


__all__ = [
    "DueDiligenceInputError",
    "build_due_diligence",
    "build_due_diligence_async",
    "canonical_due_diligence_json",
    "RULES",
]
