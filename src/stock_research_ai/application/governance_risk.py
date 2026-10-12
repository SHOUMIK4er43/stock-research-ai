"""DETAILED_RESEARCH_V1 Box 13: ownership, governance, auditor, management and exchange events.

Inputs: the Box 12 shareholding / event / insider ledgers, the Box 8 guidance outcomes, Box 3
auditor-report evidence, and explicit cited inputs (pledges, auditor reports, related-party
transactions, management changes, board facts, penalties ...), already normalized by
diligence_inputs (duplicates once, conflicts resolved by authority or withheld). Severity is
never read from the wording of a filing: an event is graded only when it is adverse to the
company (by type, or by a cited direction) and numerically material; everything else from the
registered rules. No motive is inferred and no wrongdoing is implied.
"""

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from stock_research_ai.application.diligence_inputs import (
    Candidate,
    Resolution,
    conflict_ev,
    conflicted,
    fid,
    norm,
    resolve,
)
from stock_research_ai.application.financial_metrics import REVENUE, Fact, shift
from stock_research_ai.application.red_flags import (
    NS,
    RULES,
    Ctx,
    Ev,
    Trace,
    grade,
    q,
    source_label,
    unavailable,
    worse,
)
from stock_research_ai.domain.document_intelligence import (
    EvidenceBundle,
    EvidenceKind,
    SectionKind,
)
from stock_research_ai.domain.due_diligence import (
    AUTHORITY_RANK,
    AuditOpinion,
    AuditorSignals,
    AuditorTextMention,
    ExchangeEventAssessment,
    ExchangeEventRisk,
    ExchangeEvents,
    ExecutivePay,
    Governance,
    GovernanceEntry,
    HolderCategory,
    Layer,
    ManagementReliability,
    ManagementRole,
    Ownership,
    OwnershipPoint,
    OwnershipSeries,
    RelatedParty,
    RelatedPartyKind,
    RelatedPartyRelation,
    RiskCategory,
    ShareCount,
    SignalBasis,
    SignalStatus,
    SourceAuthority,
)
from stock_research_ai.domain.due_diligence import Severity as SV
from stock_research_ai.domain.financial_canonical import (
    CanonicalMetric,
    Confidence,
    ReportingBasis,
)
from stock_research_ai.domain.management_intelligence import (
    Lifecycle,
    ManagementIntelligenceDataset,
    Measure,
    OutcomeResult,
    TargetType,
)
from stock_research_ai.domain.market_intelligence import (
    CorporateActionKind,
    CoverageCategory,
    CoverageStatus,
    EventType,
    MarketIntelligenceDataset,
    MetricStatus,
)

S = SignalStatus
NA = SignalBasis.NOT_APPLICABLE
D = Decimal
HUNDRED = D(100)
M = CanonicalMetric
MIN_GUIDANCE_SAMPLE = 5
PLEDGE_STEP_PP = D(2)  # a 'material' quarterly rise in the pledged share


def years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February
        return day.replace(year=day.year - years, day=28)


def coverage_ok(box12: MarketIntelligenceDataset | None, category: CoverageCategory) -> bool:
    if box12 is None:
        return False
    item = next((i for i in box12.coverage.items if i.category == category), None)
    return item is not None and item.status in (CoverageStatus.AVAILABLE, CoverageStatus.STALE,
                                                CoverageStatus.NOT_FOUND)


def event_source(box12: MarketIntelligenceDataset, event_id: UUID) -> str | None:
    event = next((e for e in box12.events if e.event_id == event_id), None)
    if event is None:
        return None
    return event.source_url or next((s.source_url for s in event.sources if s.source_url), None)


# --- ownership ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Holding:
    period_end: date
    pct: Decimal
    layer: Layer
    record_id: UUID
    source_id: str
    confidence: Confidence
    authority: str


EXCHANGE_RANK = AUTHORITY_RANK[SourceAuthority.EXCHANGE_FILING]


def _pct_text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def holding_points(
    ctx: Ctx, box12: MarketIntelligenceDataset | None, category: HolderCategory
) -> tuple[dict[date, Holding | None], list[tuple[date, Resolution]]]:
    """One observation per quarter end from the Box 12 filings and the cited holdings, decided
    by the shared input policy: the latest revision of a filing (by its submission date), equal
    values merged, different values resolved by authority or left OPEN (the quarter is then
    None: never an arbitrary pick). Returns the points and every disagreement."""
    candidates: dict[date, list[Candidate]] = {}
    if box12 is not None:
        superseded = {f.supersedes_filing_id for f in box12.shareholding
                      if f.supersedes_filing_id}
        filings: dict[date, list[tuple[date | None, Candidate]]] = {}
        for f in box12.shareholding:
            pct = f.promoter_pct if category == HolderCategory.PROMOTER else f.public_pct
            if f.filing_id in superseded or pct is None:
                continue
            url = f.xbrl_url or next((s.source_url for s in f.sources if s.source_url), None)
            filings.setdefault(f.period_end, []).append((f.submitted_on, Candidate(
                EXCHANGE_RANK, _pct_text(pct), f"BOX12|{f.filing_id}",
                Holding(f.period_end, pct, Layer.BOX12, f.filing_id,
                        url or f"BOX12:shareholding:{f.filing_id}", Confidence.HIGH,
                        SourceAuthority.EXCHANGE_FILING.value))))
        for day, rows in filings.items():
            dates = [r[0] for r in rows]
            if all(d is not None for d in dates):  # a later explicit submission revises it
                newest = max(d for d in dates if d is not None)
                rows = [r for r in rows if r[0] == newest]
            candidates[day] = [c for _, c in rows]
    for h in ctx.request.holdings:
        if h.category != category:
            continue
        candidates.setdefault(h.period_end, []).append(Candidate(
            AUTHORITY_RANK[h.source.authority], _pct_text(h.pct), f"REQUEST|{fid('holding', h)}",
            Holding(h.period_end, h.pct, Layer.REQUEST, fid("holding", h),
                    source_label(h.source), Confidence.HIGH, h.source.authority.value)))
    points: dict[date, Holding | None] = {}
    disputes: list[tuple[date, Resolution]] = []
    explicit_open = {c.first.period_end for c in conflicted(
        ctx, "holdings", lambda h: h.category == category and h.holder is None)}
    for day in sorted(set(candidates) | explicit_open):
        if day in explicit_open:  # the cited inputs themselves disagree (see the input policy)
            points[day] = None
            continue
        resolution = resolve(candidates[day])
        points[day] = resolution.winner.item if resolution.winner else None
        if resolution.status in ("RESOLVED", "OPEN"):
            disputes.append((day, resolution))
    return points, disputes


def promoter_series(ctx: Ctx, box12: MarketIntelligenceDataset | None) -> list[Holding]:
    points, _ = holding_points(ctx, box12, HolderCategory.PROMOTER)
    return [h for _, h in sorted(points.items()) if h is not None]


def _change(points: Sequence[tuple[date, Decimal]], months: int) -> Decimal | None:
    if not points:
        return None
    latest_day, latest = points[-1]
    target = shift(latest_day, -months)
    prior = next((v for d, v in points if d == target), None)
    return None if prior is None else q(latest - prior)


def ownership_series(ctx: Ctx, box12: MarketIntelligenceDataset | None) -> list[OwnershipSeries]:
    out: list[OwnershipSeries] = []

    def series(category: object, rows: list[OwnershipPoint], holder: str | None = None) -> None:
        if not rows:
            return
        pairs = [(r.period_end, r.pct) for r in rows]
        out.append(OwnershipSeries(
            category=category, holder=holder, points=tuple(rows),  # type: ignore[arg-type]
            change_qoq_pp=_change(pairs, 3), change_4q_pp=_change(pairs, 12),
            change_12q_pp=_change(pairs, 36)))

    def filed(category: HolderCategory) -> list[OwnershipPoint]:
        points, _ = holding_points(ctx, box12, category)
        return [OwnershipPoint(period_end=h.period_end, pct=h.pct, layer=h.layer,
                               record_id=h.record_id, source_id=h.source_id[:1000])
                for _, h in sorted(points.items()) if h is not None]

    series(HolderCategory.PROMOTER, filed(HolderCategory.PROMOTER))
    series(HolderCategory.PUBLIC, filed(HolderCategory.PUBLIC))
    explicit: dict[tuple[HolderCategory, str | None], dict[date, OwnershipPoint]] = {}
    for h in ctx.request.holdings:  # one record per quarter, category and holder (the policy)
        if h.category in (HolderCategory.PROMOTER, HolderCategory.PUBLIC):
            continue
        explicit.setdefault((h.category, h.holder), {})[h.period_end] = OwnershipPoint(
            period_end=h.period_end, pct=h.pct, layer=Layer.REQUEST, record_id=fid("holding", h),
            source_id=source_label(h.source))
    for (category, holder), rows in sorted(explicit.items(), key=lambda i: (i[0][0].value,
                                                                             i[0][1] or "")):
        series(category, [rows[k] for k in sorted(rows)], holder)
    pledges = {p.period_end: p for p in ctx.request.pledges}
    series("PLEDGED_OF_PROMOTER", [
        OwnershipPoint(period_end=d, pct=p.pledged_pct_of_promoter_holding, layer=Layer.REQUEST,
                       record_id=fid("pledge", p), source_id=source_label(p.source))
        for d, p in sorted(pledges.items())])
    counts = {c.period_end: c for c in ctx.request.shareholder_counts}
    series("SHAREHOLDER_COUNT", [
        OwnershipPoint(period_end=d, pct=D(c.count), layer=Layer.REQUEST,
                       record_id=fid("shareholders", c), source_id=source_label(c.source))
        for d, c in sorted(counts.items())])
    return out


def promoter_pledge(ctx: Ctx) -> tuple[Ev, Ev | None]:
    pledges = sorted(ctx.request.pledges, key=lambda p: p.period_end)  # one per quarter
    disputed = {c.first.period_end: c for c in conflicted(ctx, "pledges")}
    window_days = sorted({p.period_end for p in pledges} | set(disputed))[-3:]
    if any(d in disputed for d in window_days):
        return conflict_ev(ctx, NA, [disputed[d] for d in window_days if d in disputed]), None
    if not pledges:
        return unavailable(NA, "PLEDGE_DATA_NOT_SUPPLIED", explanation="promoter pledge "
                           "data (shareholding pattern / pledge disclosures) not supplied"), None
    latest = pledges[-1]
    trace = Trace()
    window = pledges[-3:]
    for p in window:
        ctx.explicit(trace, f"pledged % {p.period_end}", p.pledged_pct_of_promoter_holding,
                     "PERCENT", fid("pledge", p), p.source, period_end=p.period_end)
    value = latest.pledged_pct_of_promoter_holding
    severity = grade(value, RULES["PROMOTER_PLEDGE"].thresholds)
    issues: list[str] = []
    if len(window) == 3:
        quarters = [p.period_end for p in window]
        consecutive = all(shift(quarters[i], 3) == quarters[i + 1] for i in range(2))
        steps = [window[i + 1].pledged_pct_of_promoter_holding
                 - window[i].pledged_pct_of_promoter_holding for i in range(2)]
        if consecutive and all(s >= PLEDGE_STEP_PP for s in steps):
            severity = SV.RED
            issues.append("PLEDGE_RISING_TWO_CONSECUTIVE_QUARTERS")
    ev = Ev(S.EVALUATED, NA, severity, value, period_label=str(latest.period_end),
            period_end=latest.period_end, trace=trace, issues=issues,
            calculation=f"pledged share of promoter holding {value} %",
            explanation=f"{value} % of promoter shares pledged at {latest.period_end}")
    positive = None
    if value == 0:
        positive = Ev(S.EVALUATED, NA, SV.GREEN, value, period_label=str(latest.period_end),
                      period_end=latest.period_end, trace=trace,
                      explanation="no promoter shares pledged in the latest quarter")
    return ev, positive


DILUTING_EVENTS = {EventType.FUND_RAISE, EventType.RIGHTS, EventType.BONUS,
                   EventType.ACQUISITION}
DILUTING_ACTIONS = {CorporateActionKind.BONUS, CorporateActionKind.RIGHTS,
                    CorporateActionKind.MERGER, CorporateActionKind.DEMERGER}


def dilution_ruled_out(ctx: Ctx, box12: MarketIntelligenceDataset | None, start: date,
                       end: date) -> bool:
    if box12 is None or not coverage_ok(box12, CoverageCategory.MATERIAL_ANNOUNCEMENTS):
        return False
    if any(start < e.event_date <= end and e.event_type in DILUTING_EVENTS for e in box12.events):
        return False
    if any(a.kind in DILUTING_ACTIONS and any(start < x <= end for x in (
            a.record_date, a.ex_date, a.effective_date, a.announcement_date) if x)
           for a in box12.corporate_actions):
        return False
    return not any(start < i.issue_date <= end for i in ctx.request.preferential_issues)


SPLITTING_EVENTS = {EventType.BONUS, EventType.SPLIT}
SPLITTING_ACTIONS = {CorporateActionKind.BONUS, CorporateActionKind.SPLIT}
#: promoter % is filed to 2 decimals: a derived promoter share count within 0.01 % of the
#: shares outstanding is rounding, not a sale
PROMOTER_SHARE_TOLERANCE_PP = D("0.01")


def bonus_split_ruled_out(box12: MarketIntelligenceDataset | None, start: date,
                          end: date) -> bool:
    """No bonus / split in (start, end], on a complete corporate-action ledger."""
    if box12 is None or not coverage_ok(box12, CoverageCategory.CORPORATE_ACTIONS):
        return False
    if any(start < e.event_date <= end and e.event_type in SPLITTING_EVENTS
           for e in box12.events):
        return False
    return not any(a.kind in SPLITTING_ACTIONS and any(start < x <= end for x in (
        a.record_date, a.ex_date, a.effective_date, a.announcement_date) if x)
        for a in box12.corporate_actions)


def comparable_counts(ctx: Ctx, box12: MarketIntelligenceDataset | None, start: date,
                      end: date) -> tuple[ShareCount, ShareCount] | str | None:
    """Two share counts on one share basis: both restated for bonus / split, or both as
    filed with no bonus / split between them. A string says why counts exist but do not
    compare (a bonus or split would otherwise read as promoter buying or selling)."""
    counts = {(c.as_of, c.bonus_split_adjusted): c for c in ctx.request.share_counts}
    adjusted = counts.get((start, True)), counts.get((end, True))
    if adjusted[0] is not None and adjusted[1] is not None:
        return adjusted[0], adjusted[1]
    filed = counts.get((start, False)), counts.get((end, False))
    if filed[0] is not None and filed[1] is not None:
        if bonus_split_ruled_out(box12, start, end):
            return filed[0], filed[1]
        return "UNADJUSTED_COUNTS_ACROSS_POSSIBLE_BONUS_SPLIT"
    if {d for d, _ in counts} >= {start, end}:
        return "SHARE_COUNT_BASES_DIFFER"
    return None


def promoter_selling(ctx: Ctx, box12: MarketIntelligenceDataset | None) -> tuple[Ev, Ev | None]:
    points, disputes = holding_points(ctx, box12, HolderCategory.PROMOTER)
    if not points:
        return unavailable(NA, "NO_SHAREHOLDING_SERIES"), None
    latest_day = max(points)
    target = shift(latest_day, -12)
    if target not in points:
        return unavailable(NA, "NO_HOLDING_FOUR_QUARTERS_EARLIER"), None
    trace = Trace()
    prior, latest = points[target], points[latest_day]
    if prior is None or latest is None:  # the quarter's sources disagree: nothing is picked
        for day, resolution in disputes:
            if day in (target, latest_day):
                for c in resolution.candidates:
                    h = c.item
                    ctx.record(trace, f"conflicting promoter % {h.period_end} ({h.authority})",
                               h.pct, "PERCENT", h.layer, h.record_id, h.source_id,
                               period_end=h.period_end,
                               evidence=[h.record_id] if h.layer == Layer.BOX12 else [])
        return conflict_ev(ctx, NA, conflicted(
            ctx, "holdings", lambda h: h.category == HolderCategory.PROMOTER
            and h.period_end in (target, latest_day)), trace,
            what=" - promoter holding"), None
    for h in (prior, latest):
        ctx.record(trace, f"promoter % {h.period_end}", h.pct, "PERCENT", h.layer, h.record_id,
                   h.source_id, period_end=h.period_end, confidence=h.confidence,
                   evidence=[h.record_id] if h.layer == Layer.BOX12 else [])
    disputed_counts = conflicted(ctx, "share_counts",
                                 lambda c: c.as_of in (prior.period_end, latest.period_end))
    if disputed_counts:
        return conflict_ev(ctx, NA, disputed_counts, trace, what=" - share counts"), None
    raw = q(prior.pct - latest.pct)
    thresholds = RULES["PROMOTER_SELLING"].thresholds
    pair = comparable_counts(ctx, box12, prior.period_end, latest.period_end)
    issues: list[str] = []
    calc = f"{prior.pct} - {latest.pct}"
    value: Decimal
    if isinstance(pair, tuple):
        c0, c1 = pair
        for count in (c0, c1):
            ctx.explicit(trace, f"shares {count.as_of}", count.shares, "SHARES",
                         fid("shares", count), count.source, period_end=count.as_of)
        held0, held1 = prior.pct / HUNDRED * c0.shares, latest.pct / HUNDRED * c1.shares
        value = q((held0 - held1) * HUNDRED / c1.shares)
        issues.append("DILUTION_SEPARATED_BY_SHARE_COUNTS")
        if c1.shares > c0.shares and value > PROMOTER_SHARE_TOLERANCE_PP:
            issues.append("PROMOTER_SHARES_FELL_DESPITE_DILUTION")
        elif raw > 0 and value <= PROMOTER_SHARE_TOLERANCE_PP:
            issues.append("DECLINE_EXPLAINED_BY_DILUTION")
        calc = (f"promoter shares {q(held0)} ({prior.pct}% x {c0.shares}) -> {q(held1)} "
                f"({latest.pct}% x {c1.shares}); ({q(held0)} - {q(held1)}) / {c1.shares} x 100")
    elif dilution_ruled_out(ctx, box12, prior.period_end, latest.period_end):
        value = raw
        issues.append("DILUTION_RULED_OUT_BY_EVENT_LEDGER")
    elif grade(raw, thresholds) == SV.GREEN:
        value = raw
        issues.append("UNADJUSTED_DECLINE_BELOW_THRESHOLD")
    else:
        return unavailable(NA, "DILUTION_NOT_SEPARABLE", trace=trace, value=raw,
                           explanation=f"promoter holding fell {raw} pp but an issue of shares "
                           "in the window cannot be excluded" + (
                               f" (share counts not comparable: {pair})" if pair else "")), None
    severity = grade(value, thresholds)
    ev = Ev(S.EVALUATED, NA, severity, value, period_label=f"{prior.period_end} to "
            f"{latest.period_end}", period_end=latest.period_end, trace=trace, issues=issues,
            calculation=calc, explanation=f"promoter holding change over 4 quarters: "
            f"{prior.pct} % -> {latest.pct} %")
    stable = None
    quarters = sum(1 for d, h in points.items()
                   if h is not None and target <= d <= latest.period_end)
    if abs(raw) < D("0.5") and quarters >= 5:
        stable = Ev(S.EVALUATED, NA, SV.GREEN, abs(raw), period_end=latest.period_end,
                    period_label=f"{prior.period_end} to {latest.period_end}", trace=trace,
                    explanation=f"promoter holding moved {raw} pp over 4 quarters")
    return ev, stable


def equity_dilution(ctx: Ctx, box12: MarketIntelligenceDataset | None) -> Ev:
    counts = sorted(ctx.request.share_counts, key=lambda c: c.as_of)
    disputed = conflicted(ctx, "share_counts")
    if disputed:
        last = max([c.as_of for c in counts] + [x.as_of for c in disputed for x in c.records])
        window = [c for c in disputed if any(
            x.as_of >= years_before(last, 3) - timedelta(days=45) for x in c.records)]
        if window:
            return conflict_ev(ctx, NA, window, what=" - share counts")
    trace = Trace()
    if counts:
        latest = counts[-1]
        anchor = years_before(latest.as_of, 3)
        earlier = [c for c in counts if abs((c.as_of - anchor).days) <= 45]
        if earlier:
            first = min(earlier, key=lambda c: (abs((c.as_of - anchor).days), c.as_of))
            for c in (first, latest):
                ctx.explicit(trace, f"shares {c.as_of}", c.shares, "SHARES", fid("shares", c),
                             c.source, period_end=c.as_of)
            if first.bonus_split_adjusted and latest.bonus_split_adjusted or (
                not first.bonus_split_adjusted and not latest.bonus_split_adjusted
                and box12 is not None and coverage_ok(box12, CoverageCategory.CORPORATE_ACTIONS)
                and not any(a.kind in (CorporateActionKind.BONUS, CorporateActionKind.SPLIT)
                            and a.ex_date and first.as_of < a.ex_date <= latest.as_of
                            for a in box12.corporate_actions)
            ):
                value = q((latest.shares / first.shares - 1) * HUNDRED)
                return Ev(S.EVALUATED, NA, grade(value, RULES["EQUITY_DILUTION"].thresholds),
                          value, period_label=f"{first.as_of} to {latest.as_of}",
                          period_end=latest.as_of, trace=trace,
                          calculation=f"({latest.shares} / {first.shares} - 1) x 100",
                          explanation=f"share count {first.shares} -> {latest.shares}")
            return unavailable(NA, "BONUS_SPLIT_NOT_EXCLUDED", trace=trace)
    end = ctx.latest.get(ctx.analytical)
    if end is None:
        return unavailable(NA, "NO_SHARE_DATA")
    cap = ctx.canonical([M.EQUITY_SHARE_CAPITAL], ctx.periods.pit(end))
    cap0 = ctx.canonical([M.EQUITY_SHARE_CAPITAL], ctx.periods.pit(shift(end, -36)))
    ctx.add(trace, "share capital (latest)", cap, "MONEY", Layer.BOX4)
    ctx.add(trace, "share capital (3 years earlier)", cap0, "MONEY", Layer.BOX4)
    if not (isinstance(cap, Fact) and isinstance(cap0, Fact)):
        return unavailable(NA, "SHARE_CAPITAL_UNAVAILABLE", trace=trace)
    if cap0.value <= 0:
        return unavailable(NA, "NON_POSITIVE_SHARE_CAPITAL", status=S.NOT_APPLICABLE, trace=trace)
    value = q((cap.value / cap0.value - 1) * HUNDRED)
    start = shift(end, -36)
    bonus = box12 is None or not coverage_ok(box12, CoverageCategory.CORPORATE_ACTIONS) or any(
        a.kind == CorporateActionKind.BONUS and any(start < x <= end for x in (
            a.ex_date, a.record_date, a.announcement_date) if x)
        for a in box12.corporate_actions)
    if value > 0 and bonus:
        return unavailable(NA, "BONUS_NOT_EXCLUDED", trace=trace, value=value,
                           explanation="share capital rose but a bonus issue cannot be excluded")
    return Ev(S.EVALUATED, NA, grade(value, RULES["EQUITY_DILUTION"].thresholds), value,
              period_label=f"{start} to {end}", period_end=end, trace=trace,
              issues=["SHARE_CAPITAL_PROXY_FOR_SHARE_COUNT"],
              calculation=f"({cap.value} / {cap0.value} - 1) x 100",
              explanation=f"equity share capital {cap0.value} -> {cap.value}")


def cheap_insider_allotment(ctx: Ctx, box12: MarketIntelligenceDataset | None) -> Ev:
    start = years_before(ctx.request.as_of, 3)
    issues = [i for i in ctx.request.preferential_issues
              if start < i.issue_date <= ctx.request.as_of]
    insiders = [i for i in issues if i.allottee in ("PROMOTER_GROUP", "KMP")]
    if not insiders:
        if issues:
            return unavailable(NA, "NO_INSIDER_ALLOTMENT", status=S.NOT_APPLICABLE)
        raised = box12 is not None and any(
            start < e.event_date <= ctx.request.as_of
            and e.event_type in (EventType.FUND_RAISE, EventType.RIGHTS) for e in box12.events)
        if box12 is not None and coverage_ok(box12, CoverageCategory.MATERIAL_ANNOUNCEMENTS) \
                and not raised:
            return unavailable(NA, "NO_ISSUE_DISCLOSED", status=S.NOT_APPLICABLE)
        return unavailable(NA, "ISSUE_TERMS_NOT_SUPPLIED")
    trace = Trace()
    worst: tuple[Decimal, str] | None = None
    for i in insiders:
        discount = q((i.reference_price - i.issue_price) / i.reference_price * HUNDRED)
        ctx.explicit(trace, f"{i.kind} {i.issue_date} discount", discount, "PERCENT",
                     fid("preferential", i), i.source, period_end=i.issue_date)
        if worst is None or discount > worst[0]:
            worst = (discount, f"{i.kind} to {i.allottee} at {i.issue_price} vs "
                     f"{i.reference_basis} {i.reference_price}")
    assert worst is not None
    return Ev(S.EVALUATED, NA, grade(worst[0], RULES["CHEAP_INSIDER_ALLOTMENT"].thresholds),
              worst[0], trace=trace, calculation=worst[1],
              explanation=f"largest discount of an insider allotment: {worst[0]} %")


# --- related parties --------------------------------------------------------------------------


def related_party(ctx: Ctx) -> tuple[RelatedParty, list[UUID]]:
    items = ctx.request.related_party
    disputes = conflicted(ctx, "related_party")
    ids: list[UUID] = []
    if not items and not disputes:
        for code in ("RELATED_PARTY_DEPENDENCE", "RELATED_PARTY_LOANS"):
            ids.append(ctx.emit(code, unavailable(NA, "RELATED_PARTY_DATA_NOT_SUPPLIED")))
        return RelatedParty(fiscal_year=None, basis=NA, totals=(), record_ids=(),
                            signal_ids=ctx.present(ids)), ids
    bases = [b for b in (ctx.analytical, ctx.control) if b is not None]
    chosen = None
    for b in bases:
        years = sorted({i.fiscal_year for i in items if i.basis == b.value} | {
            r.fiscal_year for c in disputes for r in c.records if r.basis == b.value})
        if years:
            chosen = (b, years[-1])
            break
    if chosen is None:
        for code in ("RELATED_PARTY_DEPENDENCE", "RELATED_PARTY_LOANS"):
            ids.append(ctx.emit(code, unavailable(NA, "BASIS_NOT_IN_BOX4")))
        return RelatedParty(fiscal_year=None, basis=NA, totals=(), record_ids=(),
                            signal_ids=ctx.present(ids)), ids
    basis, year = chosen
    sb = SignalBasis(basis.value)
    rows = [i for i in items if i.basis == basis.value and i.fiscal_year == year]
    open_here = [c for c in disputes if c.first.basis == basis.value
                 and c.first.fiscal_year == year]
    if open_here:  # one counterparty line is disputed: no total is graded
        for code in ("RELATED_PARTY_DEPENDENCE", "RELATED_PARTY_LOANS"):
            ids.append(ctx.emit(code, conflict_ev(ctx, sb, open_here)))
        return RelatedParty(
            fiscal_year=year, basis=sb, totals=(),
            record_ids=tuple(sorted({fid("related_party", r) for r in rows}
                                    | {i for c in open_here for i in c.record_ids()}, key=str)),
            signal_ids=ctx.present(ids)), ids
    totals: dict[RelatedPartyKind, Decimal] = {}
    for r in rows:
        totals[r.kind] = totals.get(r.kind, D(0)) + r.amount_crore
    latest_end = ctx.latest.get(basis)
    period = None
    if latest_end is not None:
        period = next((ctx.periods.fy(shift(latest_end, -12 * k)) for k in range(10)
                       if ctx.periods.fy(shift(latest_end, -12 * k)).fiscal_year == year), None)
    # dependence: revenue + purchases (subsidiaries eliminated on the consolidated basis)
    trace = Trace()
    counted = [r for r in rows if r.kind in (RelatedPartyKind.REVENUE, RelatedPartyKind.PURCHASES)
               and not (basis == ReportingBasis.CONSOLIDATED
                        and r.relation == RelatedPartyRelation.SUBSIDIARY)]
    for r in counted:
        ctx.explicit(trace, f"{r.kind.value} {r.counterparty}", r.amount_crore, "MONEY",
                     fid("related_party", r), r.source, basis=sb)
    unclear = any(r.relation == RelatedPartyRelation.UNCLEAR for r in rows)
    if period is None:
        ev = unavailable(sb, "FISCAL_YEAR_NOT_IN_BOX4", trace=trace)
    elif any(r.currency != ctx.currency for r in rows):
        ev = unavailable(sb, "CURRENCY_MISMATCH", trace=trace)
    elif not counted:
        ev = unavailable(sb, "NO_RELATED_PARTY_REVENUE_OR_PURCHASES", status=S.NOT_APPLICABLE,
                         trace=trace)
    else:
        revenue = ctx.canonical(REVENUE, period, basis)
        ctx.add(trace, "revenue", revenue, "MONEY", Layer.BOX4, basis)
        if not isinstance(revenue, Fact) or revenue.value <= 0:
            ev = unavailable(sb, "REVENUE_UNAVAILABLE", trace=trace)
        else:
            total = sum((r.amount_crore for r in counted), D(0))
            value = q(total * HUNDRED / revenue.value)
            ev = Ev(S.EVALUATED, sb, grade(value, RULES["RELATED_PARTY_DEPENDENCE"].thresholds),
                    value, period_label=period.label, period_end=period.period_end, trace=trace,
                    issues=["COUNTERPARTY_RELATION_UNCLEAR"] if unclear else [],
                    calculation=f"{total} / {revenue.value} x 100",
                    explanation=f"related-party revenue + purchases are {value} % of revenue "
                    f"(FY{year}, {basis.value.lower()}); a disclosure, not a finding of "
                    "wrongdoing")
    ids.append(ctx.emit("RELATED_PARTY_DEPENDENCE", ev))
    trace = Trace()
    loans = [r for r in rows if r.kind in (RelatedPartyKind.LOAN_GIVEN,
                                           RelatedPartyKind.ADVANCE_GIVEN,
                                           RelatedPartyKind.GUARANTEE_GIVEN)
             and r.relation in (RelatedPartyRelation.PROMOTER_ENTITY,
                                RelatedPartyRelation.KMP_ENTITY)]
    for r in loans:
        ctx.explicit(trace, f"{r.kind.value} {r.counterparty}", r.amount_crore, "MONEY",
                     fid("related_party", r), r.source, basis=sb)
    if not loans:
        ev = unavailable(sb, "NO_LOANS_TO_PROMOTER_OR_KMP_ENTITIES", status=S.NOT_APPLICABLE)
    elif period is None or period.period_end is None:
        ev = unavailable(sb, "FISCAL_YEAR_NOT_IN_BOX4", trace=trace)
    else:
        nw = ctx.m5("SHAREHOLDERS_EQUITY", ctx.periods.pit(period.period_end), basis)
        ctx.add(trace, "net worth", nw, "MONEY", Layer.BOX5, basis)
        if not isinstance(nw, Fact):
            ev = unavailable(sb, "NET_WORTH_UNAVAILABLE", trace=trace)
        elif nw.value <= 0:
            ev = unavailable(sb, "NON_POSITIVE_NET_WORTH", status=S.NOT_APPLICABLE, trace=trace)
        else:
            total = sum((r.amount_crore for r in loans), D(0))
            value = q(total * HUNDRED / nw.value)
            ev = Ev(S.EVALUATED, sb, grade(value, RULES["RELATED_PARTY_LOANS"].thresholds),
                    value, period_label=period.label, period_end=period.period_end, trace=trace,
                    calculation=f"{total} / {nw.value} x 100",
                    explanation=f"loans / advances / guarantees to promoter or KMP entities: "
                    f"{value} % of net worth")
    ids.append(ctx.emit("RELATED_PARTY_LOANS", ev))
    return RelatedParty(
        fiscal_year=year, basis=sb,
        totals=tuple(sorted(((k, q(v)) for k, v in totals.items()), key=lambda i: i[0].value)),
        record_ids=tuple(sorted({fid("related_party", r) for r in rows}, key=str)),
        signal_ids=ctx.present(ids),
    ), ids


# --- auditor ----------------------------------------------------------------------------------

MODIFIED = {AuditOpinion.QUALIFIED, AuditOpinion.ADVERSE, AuditOpinion.DISCLAIMER}
PLANNED_AUDITOR_CHANGE = {"ROTATION", "TERM_COMPLETION", "REGULATOR_APPOINTMENT"}
AUDITOR_PHRASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("QUALIFIED_OPINION", re.compile(r"\bbasis for qualified opinion\b|\bqualified opinion\b",
                                     re.I)),
    ("ADVERSE_OPINION", re.compile(r"\badverse opinion\b", re.I)),
    ("DISCLAIMER_OF_OPINION", re.compile(r"\bdisclaimer of opinion\b", re.I)),
    ("EMPHASIS_OF_MATTER", re.compile(r"\bemphasis of matters?\b", re.I)),
    ("GOING_CONCERN_UNCERTAINTY", re.compile(
        r"\bmaterial uncertainty related to going concern\b", re.I)),
    ("KEY_AUDIT_MATTER", re.compile(r"\bkey audit matters?\b", re.I)),
)


def auditor_mentions(bundle: EvidenceBundle | None) -> list[AuditorTextMention]:
    out: list[AuditorTextMention] = []
    if bundle is None:
        return out
    for x in bundle.extractions:
        for item in x.evidence:
            if item.kind != EvidenceKind.AUDITOR_DISCLOSURE and \
                    item.section_kind != SectionKind.AUDITOR_REPORT:
                continue
            for phrase, pattern in AUDITOR_PHRASES:
                if pattern.search(item.normalized_text):
                    out.append(AuditorTextMention(
                        evidence_id=item.evidence_id, document_id=item.document_id,
                        phrase=phrase, page=item.page_start,
                        source_id=f"{x.artifact_reference}#{item.locator}"[:1000]))
    out.sort(key=lambda m: (str(m.document_id), m.page, m.phrase, str(m.evidence_id)))
    return out[:20_000]


def auditor_signal(ctx: Ctx) -> Ev:
    start = years_before(ctx.request.as_of, 3)
    reports = sorted(ctx.request.auditor_reports, key=lambda r: (r.fiscal_year, r.basis))
    changes = [c for c in ctx.request.auditor_changes
               if start < c.change_date <= ctx.request.as_of]
    disputed_reports = conflicted(ctx, "auditor_reports")
    years = [r.fiscal_year for r in reports] + [c.first.fiscal_year for c in disputed_reports]
    disputes = [c for c in disputed_reports if years and c.first.fiscal_year == max(years)]
    disputes += conflicted(ctx, "auditor_changes",
                           lambda c: start < c.change_date <= ctx.request.as_of)
    if disputes:
        return conflict_ev(ctx, NA, disputes)
    trace = Trace()
    reasons: list[str] = []
    severity = SV.GREEN
    latest_year = reports[-1].fiscal_year if reports else None
    for r in (x for x in reports if x.fiscal_year == latest_year):
        flags: list[str] = []
        if r.opinion in MODIFIED:
            flags.append(f"{r.opinion.value.lower()} opinion")
            severity = SV.RED
        for flag, label in ((r.emphasis_of_matter, "emphasis of matter"),
                            (r.going_concern_uncertainty, "going-concern uncertainty"),
                            (r.restatement, "restatement"),
                            (r.accounting_policy_change, "accounting-policy change")):
            if flag:
                flags.append(label)
                severity = worse(severity, SV.YELLOW)
        if r.ifc_opinion is not None and r.ifc_opinion in MODIFIED:
            flags.append("modified internal-financial-controls opinion")
            severity = worse(severity, SV.YELLOW)
        if r.caro_adverse_remarks:
            flags.append(f"{r.caro_adverse_remarks} CARO remarks (standalone)")
            severity = worse(severity, SV.YELLOW)
        ctx.explicit(trace, f"auditor report FY{r.fiscal_year} {r.basis}",
                     D(len(flags)), "CONDITIONS", fid("auditor_report", r), r.source,
                     basis=SignalBasis(r.basis))
        reasons.extend(f"FY{r.fiscal_year} {r.basis.lower()}: {f}" for f in flags)
    for c in changes:
        ctx.explicit(trace, f"auditor change {c.change_date}", None, "EVENT",
                     fid("auditor_change", c), c.source, period_end=c.change_date)
        if c.reason == "RESIGNATION" and c.mid_term and c.concerns_unresolved:
            severity = SV.RED
            reasons.append(f"mid-term resignation on {c.change_date} with concerns stated as "
                           "unresolved")
        elif c.reason not in PLANNED_AUDITOR_CHANGE:
            severity = worse(severity, SV.YELLOW)
            reasons.append(f"auditor change on {c.change_date} ({c.reason.lower()})")
    if not reports and severity != SV.RED:
        return unavailable(NA, "AUDITOR_REPORT_NOT_SUPPLIED", trace=trace,
                           explanation="no audit report supplied; auditor changes alone cannot "
                           "give a clean reading")
    value = D(2) if severity == SV.RED else D(1) if severity == SV.YELLOW else D(0)
    return Ev(S.EVALUATED, NA, severity, value,
              period_label=f"FY{latest_year}" if latest_year else None, trace=trace,
              calculation="; ".join(reasons) or "unmodified opinion, no yellow condition",
              explanation="; ".join(reasons) or "unmodified audit opinion without emphasis, "
              "going-concern, IFC, CARO or restatement remarks")


# --- management -------------------------------------------------------------------------------


def executive_pay(ctx: Ctx) -> Ev:
    by_scope: dict[str, list[ExecutivePay]] = {}
    for p in ctx.request.executive_pay:
        by_scope.setdefault(p.scope, []).append(p)
    if not by_scope:
        if conflicted(ctx, "executive_pay"):
            return conflict_ev(ctx, NA, conflicted(ctx, "executive_pay"))
        return unavailable(NA, "PAY_HISTORY_NOT_SUPPLIED", status=S.NOT_APPLICABLE)
    scope, pays = max(by_scope.items(), key=lambda i: (len(i[1]), i[0]))
    pays = sorted(pays, key=lambda p: p.fiscal_year)[-4:]  # one per year and scope (the policy)
    disputes = conflicted(ctx, "executive_pay", lambda p: norm(p.scope) == norm(scope)
                          and p.fiscal_year >= pays[0].fiscal_year)
    if disputes:
        return conflict_ev(ctx, NA, disputes)
    if len(pays) < 2:
        return unavailable(NA, "FEWER_THAN_2_YEARS_OF_PAY", status=S.INSUFFICIENT_SAMPLE)
    first, last = pays[0], pays[-1]
    trace = Trace()
    for p in (first, last):
        ctx.explicit(trace, f"pay FY{p.fiscal_year}", p.amount_crore, "MONEY",
                     fid("executive_pay", p), p.source)
    end = ctx.latest.get(ctx.analytical)
    if end is None:
        return unavailable(NA, "NO_FISCAL_YEAR", trace=trace)
    periods = {ctx.periods.fy(shift(end, -12 * k)).fiscal_year: ctx.periods.fy(shift(end, -12 * k))
               for k in range(12)}
    if first.fiscal_year not in periods or last.fiscal_year not in periods:
        return unavailable(NA, "PAY_YEARS_NOT_IN_BOX4", trace=trace)
    pat0 = ctx.canonical([M.PAT], periods[first.fiscal_year])
    pat1 = ctx.canonical([M.PAT], periods[last.fiscal_year])
    ctx.add(trace, f"PAT FY{first.fiscal_year}", pat0, "MONEY", Layer.BOX4)
    ctx.add(trace, f"PAT FY{last.fiscal_year}", pat1, "MONEY", Layer.BOX4)
    if not (isinstance(pat0, Fact) and isinstance(pat1, Fact)):
        return unavailable(NA, "PAT_UNAVAILABLE", trace=trace)
    if first.amount_crore <= 0:
        return unavailable(NA, "ZERO_BASE_PAY", status=S.NOT_APPLICABLE, trace=trace)
    pay_growth = q((last.amount_crore / first.amount_crore - 1) * HUNDRED)
    reasons: list[str] = []
    severity = SV.GREEN
    value: Decimal = pay_growth
    if pat0.value > 0:
        pat_growth = q((pat1.value / pat0.value - 1) * HUNDRED)
        if pat_growth > 0 and pay_growth > 0:
            value = q(pay_growth / pat_growth)
            if value > D(2):
                severity = SV.YELLOW
                reasons.append(f"pay grew {pay_growth} % vs PAT {pat_growth} % ({value}x)")
        elif pay_growth > 0:
            severity = SV.YELLOW
            reasons.append(f"pay grew {pay_growth} % while PAT changed {pat_growth} %")
    if pat1.value > 0:
        share = q(last.amount_crore * HUNDRED / pat1.value)
        if share > D(5):
            severity = SV.YELLOW
            reasons.append(f"pay is {share} % of PAT")
    return Ev(S.EVALUATED, NA, severity, value,
              period_label=f"FY{first.fiscal_year}-FY{last.fiscal_year}", trace=trace,
              calculation=f"scope '{scope}': " + ("; ".join(reasons) or
                                                  f"pay growth {pay_growth} %"),
              explanation="; ".join(reasons) or f"pay growth {pay_growth} %")


QUANTITATIVE = {TargetType.POINT, TargetType.RANGE, TargetType.MINIMUM, TargetType.MAXIMUM}
COMPLETED = {OutcomeResult.MET, OutcomeResult.BEAT, OutcomeResult.MISSED, OutcomeResult.PARTIAL}


def management_reliability(
    ctx: Ctx, box8: ManagementIntelligenceDataset | None
) -> tuple[ManagementReliability, list[UUID]]:
    start, end = years_before(ctx.request.as_of, 3), ctx.request.as_of
    ids: list[UUID] = []
    if box8 is None:
        ids.append(ctx.emit("UNRELIABLE_GUIDANCE", unavailable(NA, "BOX8_NOT_SUPPLIED")))
        return ManagementReliability(
            window_start=start, window_end=end, minimum_sample=MIN_GUIDANCE_SAMPLE,
            sample_status="UNAVAILABLE", issued=0, completed=0, met=0, missed=0, partial=0,
            slipped=0, withdrawn=0, open=0, not_scored=0, accuracy_pct=None, outcome_ids=(),
            qa_answer_status=(), signal_ids=ctx.present(ids)), ids
    guidance = {g.guidance_id: g for g in box8.guidance}
    outcomes = {o.guidance_id: o for o in box8.outcomes}
    events = {e.event_id: e for e in box8.events}
    # one target per timeline: its latest statement (reiterations never inflate the sample)
    targets: list[UUID] = []
    lifecycles: dict[UUID, tuple[Lifecycle, ...]] = {}
    in_timeline: set[UUID] = set()
    for t in box8.timelines:
        if t.guidance_ids:
            targets.append(t.guidance_ids[-1])
            lifecycles[t.guidance_ids[-1]] = t.lifecycles
            in_timeline |= set(t.guidance_ids)
    targets += [g for g in guidance if g not in in_timeline]
    counts: Counter[str] = Counter()
    trace = Trace()
    considered: list[UUID] = []
    for gid in sorted(set(targets), key=str):
        g = guidance[gid]
        period_end = g.target_period.period_end if g.target_period else None
        if g.target_type not in QUANTITATIVE or g.measure in (Measure.QUALITATIVE,
                                                              Measure.DIRECTION):
            continue
        if period_end is None or not start <= period_end <= end + timedelta(days=3650):
            continue
        if period_end > end:
            counts["open"] += 1
            continue
        counts["issued"] += 1
        lcs = lifecycles.get(gid, (g.lifecycle,))
        if Lifecycle.LOWERED in lcs or Lifecycle.WIDENED in lcs:
            counts["slipped"] += 1
        if lcs and lcs[-1] == Lifecycle.WITHDRAWN or g.lifecycle == Lifecycle.WITHDRAWN:
            counts["withdrawn"] += 1
            continue
        o = outcomes.get(gid)
        if o is None or o.result == OutcomeResult.NOT_YET_DUE:
            counts["open"] += 1
            continue
        if o.result not in COMPLETED:
            counts["not_scored"] += 1
            continue
        considered.append(o.outcome_id)
        counts["completed"] += 1
        counts["met" if o.result in (OutcomeResult.MET, OutcomeResult.BEAT)
               else "partial" if o.result == OutcomeResult.PARTIAL else "missed"] += 1
        event = events.get(g.event_id)
        label = g.target_period.raw_text if g.target_period else ""
        ctx.record(trace, f"{g.subject.subject_code} {label}",
                   o.actual.value if o.actual else None, o.result.value, Layer.BOX8, o.outcome_id,
                   event.source_url if event else None, period_end=period_end,
                   evidence=g.statement_ids,
                   confidence=Confidence(g.confidence.value))
    completed = counts["completed"]
    accuracy = q(D(counts["met"]) * HUNDRED / completed) if completed else None
    status = "SUFFICIENT" if completed >= MIN_GUIDANCE_SAMPLE else "INSUFFICIENT_SAMPLE"
    if completed < MIN_GUIDANCE_SAMPLE:
        ev = unavailable(NA, "INSUFFICIENT_SAMPLE", status=S.INSUFFICIENT_SAMPLE, trace=trace,
                         value=accuracy, explanation=f"{completed} completed quantitative "
                         f"targets (minimum {MIN_GUIDANCE_SAMPLE}); open guidance is not counted")
    else:
        assert accuracy is not None
        ev = Ev(S.EVALUATED, NA, grade(accuracy, RULES["UNRELIABLE_GUIDANCE"].thresholds),
                accuracy, period_label=f"{start} to {end}", period_end=end, trace=trace,
                calculation=f"{counts['met']} met / {completed} completed x 100",
                explanation=f"{counts['met']} of {completed} completed targets met "
                f"({counts['missed']} missed, {counts['partial']} partial)")
    ids.append(ctx.emit("UNRELIABLE_GUIDANCE", ev))
    if ev.status == S.EVALUATED and accuracy is not None:
        good = Ev(S.EVALUATED, NA,
                  grade(accuracy, RULES["CONSISTENT_GUIDANCE_DELIVERY"].thresholds),
                  accuracy, period_end=end, trace=trace,
                  explanation=f"{accuracy} % of {completed} completed targets met")
        ids.append(ctx.emit("CONSISTENT_GUIDANCE_DELIVERY", good))
    qa = Counter(p.answer_status.value for p in box8.qa_pairs)
    return ManagementReliability(
        window_start=start, window_end=end, minimum_sample=MIN_GUIDANCE_SAMPLE,
        sample_status=status,  # type: ignore[arg-type]
        issued=counts["issued"], completed=completed, met=counts["met"],
        missed=counts["missed"], partial=counts["partial"], slipped=counts["slipped"],
        withdrawn=counts["withdrawn"], open=counts["open"], not_scored=counts["not_scored"],
        accuracy_pct=accuracy, outcome_ids=tuple(sorted(considered, key=str)),
        qa_answer_status=tuple(sorted(qa.items())), signal_ids=ctx.present(ids),
    ), ids


DEPARTURES = {"RESIGNATION", "RETIREMENT", "REMOVAL", "TERM_END", "DEATH"}


def management_churn(ctx: Ctx, box12: MarketIntelligenceDataset | None) -> Ev:
    start = years_before(ctx.request.as_of, 3)
    changes = [c for c in ctx.request.management_changes
               if start < c.change_date <= ctx.request.as_of]
    auditors = [c for c in ctx.request.auditor_changes
                if start < c.change_date <= ctx.request.as_of]
    disputes = conflicted(ctx, "management_changes",
                          lambda c: start < c.change_date <= ctx.request.as_of)
    disputes += conflicted(ctx, "auditor_changes",
                           lambda c: start < c.change_date <= ctx.request.as_of)
    if disputes:
        return conflict_ev(ctx, NA, disputes)
    if not ctx.request.management_changes and not ctx.request.auditor_changes:
        return unavailable(NA, "MANAGEMENT_CHANGES_NOT_SUPPLIED")
    trace = Trace()
    counted = 0
    for c in changes:
        if c.role in (ManagementRole.CEO_MD, ManagementRole.CFO) and c.kind in DEPARTURES \
                and not c.planned_succession:
            counted += 1
            ctx.explicit(trace, f"{c.role.value} {c.kind.lower()} {c.change_date}", None,
                         "EVENT", fid("management_change", c), c.source, period_end=c.change_date)
    for a in auditors:
        if a.reason not in PLANNED_AUDITOR_CHANGE:
            counted += 1
            ctx.explicit(trace, f"auditor change {a.change_date}", None, "EVENT",
                         fid("auditor_change", a), a.source, period_end=a.change_date)
    issues = ["BASED_ON_SUPPLIED_CHANGES"]
    if box12 is not None:
        filed = sum(1 for e in box12.events if start < e.event_date <= ctx.request.as_of
                    and e.event_type in (EventType.KMP_CHANGE, EventType.AUDITOR_CHANGE))
        if filed > len(changes) + len(auditors):
            issues.append("MORE_EXCHANGE_FILINGS_THAN_SUPPLIED_CHANGES")
    if not trace.inputs:
        for m in changes:
            ctx.explicit(trace, f"change {m.change_date}", None, "EVENT",
                         fid("management_change", m), m.source, period_end=m.change_date)
        for a in auditors:
            ctx.explicit(trace, f"change {a.change_date}", None, "EVENT",
                         fid("auditor_change", a), a.source, period_end=a.change_date)
    return Ev(S.EVALUATED, NA, grade(D(counted), RULES["MANAGEMENT_CHURN"].thresholds),
              D(counted), period_label=f"{start} to {ctx.request.as_of}",
              period_end=ctx.request.as_of, trace=trace, issues=issues,
              calculation=f"{counted} unplanned CEO / CFO departures and auditor changes",
              explanation=f"{counted} unplanned CEO / CFO departures or auditor changes in 3 years")


def board_independence(ctx: Ctx) -> Ev:
    disputes = conflicted(ctx, "board")
    latest = max([b.as_of for b in ctx.request.board] + [c.first.as_of for c in disputes],
                 default=None)
    if any(c.first.as_of == latest for c in disputes):
        return conflict_ev(ctx, NA, [c for c in disputes if c.first.as_of == latest])
    if not ctx.request.board:
        return unavailable(NA, "BOARD_DATA_NOT_SUPPLIED")
    b = max(ctx.request.board, key=lambda x: x.as_of)  # one record per date (the policy)
    trace = Trace()
    pct = q(D(b.independent_directors) * HUNDRED / D(b.directors))
    ctx.explicit(trace, "independent share", pct, "PERCENT", fid("board", b), b.source,
                 period_end=b.as_of)
    third = D("33.333333")
    if b.executive_chair is True:
        severity = SV.YELLOW if pct < 50 else SV.GREEN
        need = "50 % (executive chair)"
    elif b.executive_chair is False:
        severity = SV.YELLOW if pct < third else SV.GREEN
        need = "one third (non-executive chair)"
    elif pct < third:
        severity, need = SV.YELLOW, "one third (chair status unknown)"
    elif pct < 50:
        return Ev(S.INFORMATIONAL, NA, None, pct, reason="CHAIR_STATUS_UNKNOWN",
                  period_end=b.as_of, trace=trace,
                  explanation=f"{pct} % independent; the required share (one third or half) "
                  "depends on the chair's status, which is not stated")
    else:
        severity, need = SV.GREEN, "50 %"
    return Ev(S.EVALUATED, NA, severity, pct, period_label=str(b.as_of), period_end=b.as_of,
              trace=trace, calculation=f"{b.independent_directors} / {b.directors} x 100",
              explanation=f"{b.independent_directors} of {b.directors} directors independent "
              f"({pct} %); listing minimum {need}")


# --- exchange events --------------------------------------------------------------------------

EVENT_CATEGORY: dict[EventType, RiskCategory] = {
    EventType.EXCHANGE_PENALTY: RiskCategory.REGULATORY,
    EventType.REGULATORY: RiskCategory.REGULATORY,
    EventType.LITIGATION: RiskCategory.LEGAL,
    EventType.AUDITOR_CHANGE: RiskCategory.AUDITOR,
    EventType.KMP_CHANGE: RiskCategory.MANAGEMENT,
    EventType.BOARD_CHANGE: RiskCategory.GOVERNANCE,
    EventType.CREDIT_RATING: RiskCategory.LIQUIDITY,
    EventType.ORDER_CANCELLATION: RiskCategory.ORDER_BOOK,
    EventType.ORDER_REVISION: RiskCategory.ORDER_BOOK,
    EventType.FUND_RAISE: RiskCategory.OWNERSHIP,
    EventType.RIGHTS: RiskCategory.OWNERSHIP,
    EventType.ACQUISITION: RiskCategory.OTHER,
    EventType.DIVESTMENT: RiskCategory.OTHER,
    EventType.INSIDER_TRADE: RiskCategory.OWNERSHIP,
    EventType.BULK_DEAL: RiskCategory.OWNERSHIP,
    EventType.BLOCK_DEAL: RiskCategory.OWNERSHIP,
    EventType.SHAREHOLDING: RiskCategory.OWNERSHIP,
}
GRADED_EVENTS = {
    EventType.ORDER_CANCELLATION: "MATERIAL_ORDER_CANCELLATION",
    EventType.LITIGATION: "MATERIAL_LITIGATION",
    EventType.REGULATORY: "MATERIAL_REGULATORY_ACTION",
    EventType.EXCHANGE_PENALTY: "MATERIAL_REGULATORY_ACTION",
}
#: the event type itself is a loss to, or a levy on, the company
ADVERSE_BY_TYPE = {EventType.ORDER_CANCELLATION, EventType.EXCHANGE_PENALTY}
#: a topic, not an outcome (an award in the company's favour is LITIGATION too): graded only
#: with a cited ADVERSE direction
DIRECTION_NEEDED = {EventType.LITIGATION, EventType.REGULATORY}
MATERIAL_EVENT_PCT = D(1)


def event_direction(ctx: Ctx, event_id: UUID) -> tuple[str, ExchangeEventAssessment | None]:
    """The cited direction of an exchange event (the input policy has already resolved or
    withheld disagreeing assessments)."""
    if conflicted(ctx, "event_assessments", lambda a: a.event_id == event_id):
        return "CONFLICTING", None
    cited = next((a for a in ctx.request.event_assessments if a.event_id == event_id), None)
    if cited is None:
        return "NOT_ESTABLISHED", None
    return cited.direction.value, cited
SALE_WORDS = re.compile(r"\b(sell|sale|sold|disposal|dispose)\b", re.I)
PROMOTER_WORDS = re.compile(r"promoter", re.I)


def exchange_events(
    ctx: Ctx, box12: MarketIntelligenceDataset | None, market_cap_crore: Decimal | None
) -> tuple[ExchangeEvents, list[UUID]]:
    start, end = years_before(ctx.request.as_of, 3), ctx.request.as_of
    entries: list[ExchangeEventRisk] = []
    ids: list[UUID] = []
    if box12 is not None:
        for e in box12.events:
            category = EVENT_CATEGORY.get(e.event_type)
            if category is None or not start < e.event_date <= end:
                continue
            ratio = e.materiality.ratio_pct if e.materiality and \
                e.materiality.status == MetricStatus.AVAILABLE else None
            severity = None
            rule = "INFORMATIONAL: no numeric materiality (wording is never graded)"
            direction: str = "NOT_ASSESSED"
            cited: ExchangeEventAssessment | None = None
            if e.event_type in ADVERSE_BY_TYPE:
                direction = "ADVERSE_BY_EVENT_TYPE"
            elif e.event_type in DIRECTION_NEEDED:
                direction, cited = event_direction(ctx, e.event_id)
            if e.event_type in GRADED_EVENTS and ratio is not None:
                if direction in ("ADVERSE_BY_EVENT_TYPE", "ADVERSE"):
                    severity = SV.RED if ratio >= 5 else SV.YELLOW if ratio >= 1 else SV.GREEN
                    rule = "EVENT_MATERIALITY/2: adverse event; stated value / reference " \
                           "revenue (>= 1 % YELLOW, >= 5 % RED)"
                else:
                    rule = f"INFORMATIONAL: direction {direction} - a {e.event_type.value} " \
                           "event and its size never establish an adverse outcome"
            source = event_source(box12, e.event_id)
            entries.append(ExchangeEventRisk(
                event_id=e.event_id, event_date=e.event_date, event_type=e.event_type.value,
                category=category, title=e.title[:1000], value_crore=e.financial_value_crore,
                materiality_pct=ratio, status="EVALUATED" if severity else "INFORMATIONAL",
                severity=severity, rule=rule, source_ids=(source[:1000],) if source else (),
                direction=direction))  # type: ignore[arg-type]
            if severity in (SV.YELLOW, SV.RED):
                trace = Trace()
                ctx.record(trace, f"{e.event_type.value} {e.event_date}", ratio, "PERCENT",
                           Layer.BOX12, e.event_id, source, period_end=e.event_date,
                           evidence=[e.event_id], detail=e.title)
                if cited is not None:
                    ctx.explicit(trace, f"direction of {e.event_type.value} {e.event_date}",
                                 None, "ADVERSE", fid("event_assessment", cited),
                                 cited.source, period_end=e.event_date)
                ids.append(ctx.emit(GRADED_EVENTS[e.event_type], Ev(
                    S.EVALUATED, NA, severity, ratio, period_label=str(e.event_date),
                    period_end=e.event_date, trace=trace, discriminator=str(e.event_id),
                    calculation=f"stated value {e.financial_value_crore} crore = {ratio} % of "
                    "reference revenue (Box 12 materiality)",
                    explanation=f"{e.event_type.value.lower().replace('_', ' ')} filed on "
                    f"{e.event_date}: stated value = {ratio} % of reference revenue")))
        for t in box12.insider_trades:
            when = t.to_date or t.intimation_date or t.from_date
            if when is None or not start < when <= end:
                continue
            if not (SALE_WORDS.search(t.transaction_type or "")
                    and PROMOTER_WORDS.search(t.person_category or "")):
                continue
            ratio = None
            severity = None
            rule = "INFORMATIONAL: promoter sale without a market capitalisation to size it"
            if t.value_inr is not None and market_cap_crore:
                ratio = q(t.value_inr / D(10_000_000) * HUNDRED / market_cap_crore)
                severity = SV.RED if ratio >= 2 else SV.YELLOW if ratio >= D("0.5") else SV.GREEN
                rule = "INSIDER_SALE/1: sale value / market cap (>= 0.5 % YELLOW, >= 2 % RED)"
            source = next((s.source_url for s in t.sources if s.source_url), None)
            entries.append(ExchangeEventRisk(
                event_id=t.record_id, event_date=when, event_type="PROMOTER_SALE",
                category=RiskCategory.OWNERSHIP, title=f"{t.person}: {t.transaction_type}"[:1000],
                value_crore=q(t.value_inr / D(10_000_000)) if t.value_inr is not None else None,
                materiality_pct=ratio, status="EVALUATED" if severity else "INFORMATIONAL",
                severity=severity, rule=rule, source_ids=(source[:1000],) if source else (),
                direction="ADVERSE_BY_EVENT_TYPE"))  # a promoter-category sale disclosure
            if severity in (SV.YELLOW, SV.RED):
                trace = Trace()
                ctx.record(trace, f"promoter sale {when}", ratio, "PERCENT", Layer.BOX12,
                           t.record_id, source or f"BOX12:insider:{t.record_id}",
                           period_end=when, evidence=[t.record_id], detail=t.person)
                ids.append(ctx.emit("LARGE_PROMOTER_SALE", Ev(
                    S.EVALUATED, NA, severity, ratio, period_end=when, trace=trace,
                    discriminator=str(t.record_id),
                    explanation=f"promoter-category sale of {t.value_inr} INR = {ratio} % of "
                    "market capitalisation")))
    entries.sort(key=lambda x: (x.event_date, x.event_type, str(x.event_id)))
    ids.append(ctx.emit("EXCHANGE_PENALTIES", exchange_penalties(ctx, box12)))
    counts = Counter(x.event_type for x in entries)
    return ExchangeEvents(window_start=start, window_end=end, entries=tuple(entries[:20_000]),
                          counts=tuple(sorted(counts.items())), signal_ids=ctx.present(ids)), ids


def exchange_penalties(ctx: Ctx, box12: MarketIntelligenceDataset | None) -> Ev:
    start, end = years_before(ctx.request.as_of, 3), ctx.request.as_of
    trace = Trace()
    days: set[date] = set()
    amounts: list[Decimal] = []
    if box12 is not None:
        for e in box12.events:
            if e.event_type == EventType.EXCHANGE_PENALTY and start < e.event_date <= end:
                days.add(e.event_date)
                ctx.record(trace, f"penalty filing {e.event_date}", e.financial_value_crore,
                           "INR_CRORE", Layer.BOX12, e.event_id, event_source(box12, e.event_id),
                           period_end=e.event_date, evidence=[e.event_id], detail=e.title)
                if e.financial_value_crore is not None:
                    amounts.append(e.financial_value_crore)
    for p in ctx.request.penalties:
        if start < p.penalty_date <= end:
            days.add(p.penalty_date)
            crore = q(p.amount_inr / D(10_000_000)) if p.amount_inr is not None else None
            ctx.explicit(trace, f"penalty {p.authority} {p.penalty_date}", crore, "INR_CRORE",
                         fid("penalty", p), p.source, period_end=p.penalty_date)
            if crore is not None:
                amounts.append(crore)
    if not days:
        if box12 is None:
            return unavailable(NA, "NO_EVENT_LEDGER", trace=trace)
        if not coverage_ok(box12, CoverageCategory.MATERIAL_ANNOUNCEMENTS):
            return unavailable(NA, "ANNOUNCEMENT_COVERAGE_INCOMPLETE", trace=trace)
    count = D(len(days))
    severity = SV.YELLOW if count >= 1 else SV.GREEN
    reasons = [f"{len(days)} penalty dates in 3 years"]
    end_fy = ctx.latest.get(ctx.analytical)
    if amounts and end_fy is not None:
        pat = ctx.canonical([M.PAT], ctx.periods.fy(end_fy))
        ctx.add(trace, "PAT (latest FY)", pat, "MONEY", Layer.BOX4)
        if isinstance(pat, Fact) and pat.value > 0:
            largest = max(amounts)
            share = q(largest * HUNDRED / pat.value)
            reasons.append(f"largest stated penalty {largest} crore = {share} % of PAT")
            if share >= 1:
                severity = SV.RED
    if count == 0 and box12 is not None:
        # a clean reading from a complete announcement ledger: cite the ledger itself
        item = next(i for i in box12.coverage.items
                    if i.category == CoverageCategory.MATERIAL_ANNOUNCEMENTS)
        ctx.record(trace, "announcement coverage", D(item.count), "FILINGS", Layer.BOX12,
                   box12.dataset_id, f"BOX12:{box12.dataset_id}:coverage")
    return Ev(S.EVALUATED, NA, severity, count, period_label=f"{start} to {end}", period_end=end,
              trace=trace, calculation="; ".join(reasons), explanation="; ".join(reasons))


# --- governance catalogue ---------------------------------------------------------------------

CATALOGUE_EVENTS = {EventType.BOARD_CHANGE, EventType.KMP_CHANGE, EventType.AUDITOR_CHANGE,
                    EventType.EXCHANGE_PENALTY, EventType.REGULATORY, EventType.FUND_RAISE,
                    EventType.RIGHTS, EventType.SHAREHOLDING, EventType.INSIDER_TRADE,
                    EventType.LITIGATION, EventType.POSTAL_BALLOT, EventType.AGM_EGM}


def governance_catalogue(
    ctx: Ctx, box12: MarketIntelligenceDataset | None
) -> list[GovernanceEntry]:
    out: list[GovernanceEntry] = []
    start = years_before(ctx.request.as_of, 3)

    def add(kind: str, day: date | None, title: str, layer: Layer, rid: UUID,
            source: str | None) -> None:
        out.append(GovernanceEntry(
            entry_id=uuid5(NS, f"{ctx.dd_key}|governance|{kind}|{rid}"), kind=kind,
            entry_date=day, title=title[:1000], layer=layer, record_id=rid,
            source_id=source[:1000] if source else None))

    if box12 is not None:
        for e in box12.events:
            if e.event_type in CATALOGUE_EVENTS and start < e.event_date <= ctx.request.as_of:
                add(e.event_type.value, e.event_date, e.title, Layer.BOX12, e.event_id,
                    event_source(box12, e.event_id))
        for t in box12.insider_trades:
            when = t.to_date or t.intimation_date or t.from_date
            if when and start < when <= ctx.request.as_of and (
                    PROMOTER_WORDS.search(t.person_category or "")
                    or re.search(r"pledge", t.transaction_type or "", re.I)):
                add("INSIDER_TRADE", when, f"{t.person}: {t.transaction_type}", Layer.BOX12,
                    t.record_id, next((s.source_url for s in t.sources if s.source_url), None))
    r = ctx.request
    for b in r.board:
        add("BOARD", b.as_of, f"{b.independent_directors} of {b.directors} directors "
            "independent", Layer.REQUEST, fid("board", b), source_label(b.source))
    for c in r.management_changes:
        add("KMP_CHANGE", c.change_date, f"{c.role.value} {c.kind.lower()}"
            + (f" ({c.person})" if c.person else ""), Layer.REQUEST,
            fid("management_change", c), source_label(c.source))
    for a in r.auditor_changes:
        add("AUDITOR_CHANGE", a.change_date, f"auditor change ({a.reason.lower()})",
            Layer.REQUEST, fid("auditor_change", a), source_label(a.source))
    for p in r.penalties:
        add("PENALTY", p.penalty_date, f"{p.authority}: {p.reason}", Layer.REQUEST,
            fid("penalty", p), source_label(p.source))
    for i in r.preferential_issues:
        add("PREFERENTIAL_ISSUE" if i.kind != "WARRANTS" else "WARRANTS", i.issue_date,
            f"{i.kind} to {i.allottee} at {i.issue_price}", Layer.REQUEST,
            fid("preferential", i), source_label(i.source))
    for x in r.related_party:
        add("RELATED_PARTY", None, f"FY{x.fiscal_year} {x.kind.value} {x.counterparty} "
            f"({x.relation.value}) {x.amount_crore} crore", Layer.REQUEST,
            fid("related_party", x), source_label(x.source))
    for pl in r.pledges:
        add("PLEDGE", pl.period_end, f"{pl.pledged_pct_of_promoter_holding} % of promoter "
            "holding pledged", Layer.REQUEST, fid("pledge", pl), source_label(pl.source))
    for c2 in r.complaints:
        add("SHAREHOLDER_COMPLAINTS", None, f"FY{c2.fiscal_year}: {c2.received} received, "
            f"{c2.pending_at_year_end} pending", Layer.REQUEST, fid("complaints", c2),
            source_label(c2.source))
    out.sort(key=lambda g: (g.entry_date or date.min, g.kind, str(g.record_id)))
    return out[:20_000]


# --- section builders -------------------------------------------------------------------------


def build_ownership(ctx: Ctx, box12: MarketIntelligenceDataset | None) -> tuple[Ownership,
                                                                                 list[UUID]]:
    ids: list[UUID] = []
    pledge, no_pledge = promoter_pledge(ctx)
    ids.append(ctx.emit("PROMOTER_PLEDGE", pledge))
    if no_pledge is not None:
        ids.append(ctx.emit("NO_PLEDGE", no_pledge))
    selling, stable = promoter_selling(ctx, box12)
    ids.append(ctx.emit("PROMOTER_SELLING", selling))
    if stable is not None:
        ids.append(ctx.emit("STABLE_PROMOTER_HOLDING", stable))
    ids.append(ctx.emit("EQUITY_DILUTION", equity_dilution(ctx, box12)))
    unparsed: set[str] = set()
    if box12 is not None:
        for f in box12.shareholding:
            unparsed |= set(f.unparsed_categories)
    covered = {h.category.value for h in ctx.request.holdings}
    if ctx.request.pledges:
        covered.add("PROMOTER_PLEDGE")
    if ctx.request.shareholder_counts:
        covered.add("SHAREHOLDER_COUNT")
    return Ownership(series=tuple(ownership_series(ctx, box12)),
                     unparsed_categories=tuple(sorted(unparsed - covered)),
                     signal_ids=ctx.present(ids)), ids


def build_governance(ctx: Ctx, box12: MarketIntelligenceDataset | None) -> tuple[Governance,
                                                                                   list[UUID]]:
    ids = [
        ctx.emit("CHEAP_INSIDER_ALLOTMENT", cheap_insider_allotment(ctx, box12)),
        ctx.emit("EXECUTIVE_PAY_VS_PROFIT", executive_pay(ctx)),
        ctx.emit("BOARD_INDEPENDENCE", board_independence(ctx)),
        ctx.emit("MANAGEMENT_CHURN", management_churn(ctx, box12)),
    ]
    return Governance(catalogue=tuple(governance_catalogue(ctx, box12)),
                      signal_ids=ctx.present(ids)), ids


def build_auditor(ctx: Ctx, bundle: EvidenceBundle | None) -> tuple[AuditorSignals, list[UUID]]:
    ids = [ctx.emit("AUDITOR_SIGNAL", auditor_signal(ctx))]
    return AuditorSignals(
        reports=tuple(fid("auditor_report", r) for r in ctx.request.auditor_reports),
        changes=tuple(fid("auditor_change", c) for c in ctx.request.auditor_changes),
        text_mentions=tuple(auditor_mentions(bundle)), signal_ids=ctx.present(ids)), ids
