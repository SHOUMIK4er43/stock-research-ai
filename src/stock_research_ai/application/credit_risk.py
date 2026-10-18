"""DETAILED_RESEARCH_V1 Box 13: debt, liquidity, credit ratings, contingencies and CWIP ageing.

Reported debt (Box 4 / Box 5) and rating-agency adjusted debt are kept side by side, never
merged. Ratings are mapped only on the Indian long-term scale (AAA = 1 ... D = 20); short-term,
structured (SO) and credit-enhanced (CE) grades are never mapped. Notch movements are measured
only between ratings of the same agency and the same stated instrument.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from stock_research_ai.application.diligence_inputs import conflict_ev, conflicted, fid
from stock_research_ai.application.earnings_quality import (
    box5_metric,
    canonical_value,
    fy,
    metric_of,
    net_debt_to_operating_ebitda,
    pit,
)
from stock_research_ai.application.financial_metrics import Fact
from stock_research_ai.application.governance_risk import coverage_ok, years_before
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
from stock_research_ai.domain.document_intelligence import EvidenceBundle, EvidenceKind
from stock_research_ai.domain.due_diligence import (
    ContingentCategory,
    ContingentLiabilities,
    CreditProfile,
    ForensicMetric,
    Layer,
    MetricKind,
    RatingActionKind,
    RatingObservation,
    RatingOutlook,
    SignalBasis,
    SignalStatus,
)
from stock_research_ai.domain.due_diligence import Severity as SV
from stock_research_ai.domain.financial_canonical import CanonicalMetric, ReportingBasis
from stock_research_ai.domain.market_intelligence import (
    CoverageCategory,
    FilingCategory,
    MarketIntelligenceDataset,
)

M = CanonicalMetric
S = SignalStatus
NA = SignalBasis.NOT_APPLICABLE
D = Decimal
HUNDRED = D(100)

LONG_TERM = ("AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB",
             "BB-", "B+", "B", "B-", "C+", "C", "C-", "D")
NOTCH = {grade_: n for n, grade_ in enumerate(LONG_TERM, start=1)}
INVESTMENT_GRADE_WORST = NOTCH["BBB-"]
_PREFIX = re.compile(
    r"^\s*(?:provisional\s+)?\[?(?:crisil|icra|care|ind|acuite|acuité|bwr|brickwork|ivr|"
    r"infomerics)\]?\s*", re.I)
_OUTLOOK = re.compile(r"\s*[/;,]\s*(stable|positive|negative|developing)\s*$", re.I)
_WATCH = re.compile(r"(?:rating\s+)?watch\s+with\s+(negative|positive|developing)\s+"
                    r"implications|\bcredit\s+watch\s+(negative|positive)", re.I)
_LONG = re.compile(r"^(AAA|AA|A|BBB|BB|B|C|D)([+-])?$")
_SHORT = re.compile(r"^A[1-4]\+?$")
_STRUCTURED = re.compile(r"\((?:SO|CE)\)|\b(?:SO|CE)\b")
_TITLE = re.compile(r"^(?P<agency>[^:]+): (?P<rating>.*) \((?P<action>[^()]*)\)$")


@dataclass(frozen=True)
class ParsedRating:
    scale: str
    notch: int | None
    outlook: RatingOutlook


def parse_rating(text: str) -> ParsedRating:
    """Deterministic parse of an Indian rating as written ('CRISIL AA+/Stable', '[ICRA]A1+',
    'CARE AA (CE)'). Anything else is UNSUPPORTED - never guessed."""
    outlook = RatingOutlook.NOT_STATED
    watch = _WATCH.search(text)
    body = _WATCH.sub("", text).strip(" ;,/")
    if watch:
        word = (watch.group(1) or watch.group(2)).upper()
        outlook = RatingOutlook(f"WATCH_{word}")
    match = _OUTLOOK.search(body)
    if match:
        if outlook == RatingOutlook.NOT_STATED:
            outlook = RatingOutlook(match.group(1).upper())
        body = body[: match.start()]
    if _STRUCTURED.search(body):
        return ParsedRating("UNSUPPORTED", None, outlook)
    body = _PREFIX.sub("", body).strip().upper().replace(" ", "")
    long_ = _LONG.match(body)
    if long_:
        return ParsedRating("INDIA_LONG_TERM", NOTCH[body], outlook)
    if _SHORT.match(body):
        return ParsedRating("INDIA_SHORT_TERM", None, outlook)
    return ParsedRating("UNSUPPORTED", None, outlook)


def action_of(text: str) -> RatingActionKind:
    t = text.lower()
    for word, kind in (("upgrad", RatingActionKind.UPGRADED),
                       ("downgrad", RatingActionKind.DOWNGRADED),
                       ("withdr", RatingActionKind.WITHDRAWN),
                       ("reaffirm", RatingActionKind.REAFFIRMED),
                       ("assign", RatingActionKind.ASSIGNED),
                       ("outlook", RatingActionKind.OUTLOOK_REVISED),
                       ("watch", RatingActionKind.WATCH)):
        if word in t:
            return kind
    return RatingActionKind.NOT_STATED


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def rating_observations(
    ctx: Ctx, box12: MarketIntelligenceDataset | None
) -> list[RatingObservation]:
    out: list[RatingObservation] = []
    for r in ctx.request.ratings:
        parsed = parse_rating(r.rating)
        outlook = r.outlook if r.outlook != RatingOutlook.NOT_STATED else parsed.outlook
        out.append(RatingObservation(
            observation_id=fid("rating", r), action_date=r.action_date, agency=r.agency,
            instrument=r.instrument, rating_text=r.rating, scale=parsed.scale,  # type: ignore[arg-type]
            notch=parsed.notch,
            investment_grade=None if parsed.notch is None
            else parsed.notch <= INVESTMENT_GRADE_WORST,
            outlook=outlook, action=r.action, layer=Layer.REQUEST, record_id=fid("rating", r),
            source_id=source_label(r.source)))
    if box12 is not None:
        for f in box12.filings:
            if f.category != FilingCategory.CREDIT_RATING:
                continue
            m = _TITLE.match(f.title)
            day = f.period_end or f.published_on
            if m is None or day is None:
                continue
            parsed = parse_rating(m.group("rating"))
            source = f.url or next((s.source_url for s in f.sources if s.source_url), None)
            out.append(RatingObservation(
                observation_id=f.document_id, action_date=day, agency=m.group("agency"),
                instrument=None, rating_text=m.group("rating"), scale=parsed.scale,  # type: ignore[arg-type]
                notch=parsed.notch,
                investment_grade=None if parsed.notch is None
                else parsed.notch <= INVESTMENT_GRADE_WORST,
                outlook=parsed.outlook, action=action_of(m.group("action")), layer=Layer.BOX12,
                record_id=f.document_id,
                source_id=(source or f"BOX12:filing:{f.document_id}")[:1000]))
    out.sort(key=lambda o: (o.action_date, norm(o.agency), norm(o.instrument or ""),
                            str(o.observation_id)))
    return out


def credit_deterioration(
    ctx: Ctx,
    ratings: list[RatingObservation],
    box12: MarketIntelligenceDataset | None,
) -> tuple[Ev, Ev | None]:
    start, end = years_before(ctx.request.as_of, 3), ctx.request.as_of
    coverage_complete = coverage_ok(box12, CoverageCategory.CREDIT_RATINGS, start, end)
    disputes = conflicted(ctx, "ratings", lambda r: r.action_date <= end)
    if disputes:  # one rating action stated two ways: no notch move is measured from either
        return conflict_ev(ctx, NA, disputes, what=" - rating actions"), None
    if not ratings:
        if coverage_complete:
            return unavailable(NA, "NO_RATING_INSTRUMENT", status=S.NOT_APPLICABLE,
                               explanation="complete source coverage found no rated "
                               "instrument"), None
        return unavailable(NA, "NO_RATING_DATA", explanation="no rating action supplied or "
                           "filed"), None
    trace = Trace()
    severity = SV.GREEN
    reasons: list[str] = []
    worst_delta = 0
    best_delta = 0
    upgrade = False
    groups: dict[tuple[str, str], list[RatingObservation]] = defaultdict(list)
    for o in ratings:
        if o.instrument is not None:
            groups[(norm(o.agency), norm(o.instrument))].append(o)

    def cite(o: RatingObservation, role: str) -> None:
        ctx.record(trace, role, D(o.notch) if o.notch is not None else None, "NOTCH", o.layer,
                   o.record_id, o.source_id, period_end=o.action_date,
                   evidence=[o.record_id] if o.layer == Layer.BOX12 else [],
                   detail=f"{o.agency} {o.instrument or ''} {o.rating_text}")

    for _, group_rows in sorted(groups.items()):
        rows = [r for r in group_rows
                if r.notch is not None and r.action != RatingActionKind.WITHDRAWN]
        for prev, cur in zip(rows, rows[1:], strict=False):
            if not start < cur.action_date <= end or prev.notch is None or cur.notch is None:
                continue
            delta = cur.notch - prev.notch
            if delta > 0:
                cite(prev, "previous rating")
                cite(cur, "rating")
                worst_delta = max(worst_delta, delta)
                reasons.append(f"{cur.agency} {cur.instrument}: {prev.rating_text} -> "
                               f"{cur.rating_text} ({delta} notch)")
            elif delta < 0:
                best_delta = min(best_delta, delta)
                cite(prev, "previous rating")
                cite(cur, "rating")
    if worst_delta:
        severity = worse(severity, grade(D(worst_delta), RULES["CREDIT_DETERIORATION"].thresholds))
    # the latest rating per agency and instrument (the exchange feed states no instrument: its
    # rows are one series per agency, and every row of its latest date is read - same-day rows
    # may cover different instruments, so none is picked over another)
    latest: dict[tuple[str, str], list[RatingObservation]] = {}
    for o in ratings:
        if o.action_date <= end:
            key = (norm(o.agency), norm(o.instrument) if o.instrument else "#EXCHANGE_FEED")
            rows = latest.setdefault(key, [])
            if rows and rows[0].action_date < o.action_date:
                rows.clear()
            if not rows or rows[0].action_date == o.action_date:
                rows.append(o)
    withdrawn = False
    for o in (row for rows in latest.values() for row in rows):
        if o.action == RatingActionKind.WITHDRAWN:
            withdrawn = True
            continue
        if o.notch is not None and o.notch > INVESTMENT_GRADE_WORST and o.action_date > start:
            cite(o, "current rating")
            severity = SV.RED
            reasons.append(f"{o.agency}: {o.rating_text} is below investment grade")
        if o.outlook in (RatingOutlook.NEGATIVE, RatingOutlook.WATCH_NEGATIVE) and \
                o.action_date > start:
            cite(o, "outlook")
            severity = worse(severity, SV.YELLOW)
            reasons.append(f"{o.agency}: {o.outlook.value.lower()} outlook")
    for o in ratings:
        if start < o.action_date <= end and o.action == RatingActionKind.DOWNGRADED and \
                not worst_delta:
            cite(o, "stated downgrade")
            severity = worse(severity, SV.YELLOW)
            reasons.append(f"{o.agency} states a downgrade on {o.action_date}")
        if start < o.action_date <= end and o.action == RatingActionKind.UPGRADED:
            upgrade = True
            cite(o, "stated upgrade")
    mapped = [o for o in ratings if o.notch is not None]
    if not mapped and severity == SV.GREEN:
        return unavailable(NA, "UNSUPPORTED_RATING_SCALE", trace=trace,
                           explanation="no rating on the mapped Indian long-term scale"), None
    if severity == SV.GREEN:
        if not coverage_complete:
            positive = None
            if best_delta < 0 or upgrade:
                positive = Ev(S.EVALUATED, NA, SV.GREEN,
                              D(-best_delta) if best_delta else D(1), period_end=end,
                              trace=trace, explanation="a rating upgrade in the last 3 years")
            unavailable_ev = unavailable(
                NA, "RATING_COVERAGE_INCOMPLETE", trace=trace,
                explanation="rating deterioration cannot be ruled out without complete, fresh "
                "rating coverage for the three-year window",
            )
            if withdrawn:
                unavailable_ev.issues.append("RATING_WITHDRAWN")
            return unavailable_ev, positive
    if not trace.inputs:
        current = max(mapped, key=lambda o: (o.action_date, str(o.observation_id)))
        cite(current, "current rating")
    issues = ([] if coverage_complete else ["RATING_COVERAGE_INCOMPLETE"])
    if withdrawn:
        issues.append("RATING_WITHDRAWN")
    ev = Ev(S.EVALUATED, NA, severity, D(worst_delta), period_label=f"{start} to {end}",
            period_end=end, trace=trace, issues=issues,
            calculation="; ".join(reasons) or "no downgrade, negative outlook or sub-investment "
            "grade rating in 3 years",
            explanation=f"{len(reasons)} deterioration condition(s) in 3 years; largest notch "
            f"move {worst_delta}" if reasons else "no credit-rating deterioration in 3 years")
    positive = None
    if best_delta < 0 or upgrade:
        positive = Ev(S.EVALUATED, NA, SV.GREEN, D(-best_delta) if best_delta else D(1),
                      period_end=end, trace=trace,
                      explanation="a rating upgrade in the last 3 years")
    return ev, positive


# --- contingencies and CWIP -------------------------------------------------------------------


def _latest_basis(
    ctx: Ctx, rows: Sequence[Any], attr: str = "as_of"
) -> tuple[ReportingBasis, date] | None:
    for b in (ctx.analytical, ctx.control):
        if b is None:
            continue
        dates = sorted({getattr(r, attr) for r in rows if r.basis == b.value})
        if dates:
            return b, dates[-1]
    return None


def contingent_liabilities(ctx: Ctx) -> tuple[ContingentLiabilities, Ev]:
    rows = list(ctx.request.contingent_liabilities)
    empty = ContingentLiabilities(as_of=None, basis=NA, by_category=(), total_crore=None,
                                  total_method="UNAVAILABLE", capital_commitments_crore=None,
                                  record_ids=(), issues=(), signal_ids=())
    disputes = conflicted(ctx, "contingent_liabilities")
    chosen = _latest_basis(ctx, rows + [r for c in disputes for r in c.records])
    if chosen is None:
        reason = "CONTINGENT_DATA_NOT_SUPPLIED" if not rows else "BASIS_NOT_IN_BOX4"
        return empty, unavailable(NA, reason)
    basis, day = chosen
    sb = SignalBasis(basis.value)
    rows = [r for r in rows if r.basis == basis.value and r.as_of == day]
    here = [c for c in disputes if c.first.basis == basis.value and c.first.as_of == day]
    if here:  # a line of the note is stated two ways: no total is graded
        return empty.model_copy(update={
            "as_of": day, "basis": sb, "issues": ("INPUT_CONFLICT",),
            "record_ids": tuple(sorted({fid("contingent", r) for r in rows}
                                       | {i for c in here for i in c.record_ids()}, key=str))}), \
            conflict_ev(ctx, sb, here, what=" - contingent liabilities")
    trace = Trace()
    issues: list[str] = []
    for r in rows:
        ctx.explicit(trace, f"{r.category.value} {r.description or ''}".strip(), r.amount_crore,
                     "MONEY", fid("contingent", r), r.source, basis=sb, period_end=day)
    by_cat: dict[ContingentCategory, Decimal] = defaultdict(lambda: D(0))
    for r in rows:
        by_cat[r.category] += r.amount_crore
    stated = sorted({r.amount_crore for r in rows if r.category == ContingentCategory.TOTAL})
    parts = sum((v for k, v in by_cat.items()
                 if k not in (ContingentCategory.TOTAL, ContingentCategory.CAPITAL_COMMITMENT)),
                D(0))
    has_parts = any(k not in (ContingentCategory.TOTAL, ContingentCategory.CAPITAL_COMMITMENT)
                    for k in by_cat)
    total: Decimal | None
    method: str
    if any(r.currency != ctx.currency for r in rows):
        total, method = None, "UNAVAILABLE"
        issues.append("CURRENCY_MISMATCH")
    elif len(stated) > 1:
        total, method = None, "UNAVAILABLE"
        issues.append("CONFLICTING_STATED_TOTALS")
    elif stated:
        total, method = stated[0], "STATED_TOTAL"
        if has_parts and parts > total * D("1.005"):
            issues.append("CATEGORIES_EXCEED_STATED_TOTAL_POSSIBLE_DOUBLE_COUNT")
    elif has_parts:
        total, method = parts, "SUM_OF_CATEGORIES"
    else:
        total, method = None, "UNAVAILABLE"
        issues.append("ONLY_CAPITAL_COMMITMENTS")
    commitments = by_cat.get(ContingentCategory.CAPITAL_COMMITMENT)
    section = ContingentLiabilities(
        as_of=day, basis=sb,
        by_category=tuple(sorted(((k, q(v)) for k, v in by_cat.items()), key=lambda i: i[0].value)),
        total_crore=q(total) if total is not None else None, total_method=method,  # type: ignore[arg-type]
        capital_commitments_crore=q(commitments) if commitments is not None else None,
        record_ids=tuple(sorted({fid("contingent", r) for r in rows}, key=str)),
        issues=tuple(sorted(issues)), signal_ids=())
    if total is None:
        return section, unavailable(sb, issues[0] if issues else "UNAVAILABLE", trace=trace)
    nw = ctx.m5("SHAREHOLDERS_EQUITY", ctx.periods.pit(day), basis)
    ctx.add(trace, "net worth", nw, "MONEY", Layer.BOX5, basis)
    if not isinstance(nw, Fact):
        return section, unavailable(sb, "NO_NET_WORTH_AT_DATE", trace=trace)
    if nw.value <= 0:
        return section, unavailable(sb, "NON_POSITIVE_NET_WORTH", status=S.NOT_APPLICABLE,
                                    trace=trace)
    value = q(total * HUNDRED / nw.value)
    return section, Ev(
        S.EVALUATED, sb, grade(value, RULES["CONTINGENT_LIABILITY"].thresholds), value,
        period_label=str(day), period_end=day, trace=trace, issues=issues,
        calculation=f"{total} ({method.lower()}) / net worth {nw.value} x 100",
        explanation=f"contingent liabilities {total} crore = {value} % of net worth "
        "(capital commitments excluded)")


def stalled_cwip(ctx: Ctx) -> Ev:
    rows = list(ctx.request.cwip_ageing)
    disputes = conflicted(ctx, "cwip_ageing")
    chosen = _latest_basis(ctx, rows + [r for c in disputes for r in c.records])
    if chosen is None:
        return unavailable(NA, "CWIP_AGEING_NOT_SUPPLIED")
    basis, day = chosen
    sb = SignalBasis(basis.value)
    here = [c for c in disputes if c.first.basis == basis.value and c.first.as_of == day]
    if here:
        return conflict_ev(ctx, sb, here, what=" - CWIP ageing")
    rows = [r for r in rows if r.basis == basis.value and r.as_of == day]
    trace = Trace()
    for r in rows:
        ctx.explicit(trace, f"CWIP {r.bucket.value} {r.status}", r.amount_crore, "MONEY",
                     fid("cwip", r), r.source, basis=sb, period_end=day)
    total = sum((r.amount_crore for r in rows), D(0))
    old = sum((r.amount_crore for r in rows if r.bucket.value == "GT_3Y"), D(0))
    if total == 0:
        return unavailable(sb, "NO_CWIP", status=S.NOT_APPLICABLE, trace=trace)
    issues: list[str] = []
    box4 = ctx.canonical([M.CWIP], ctx.periods.pit(day), basis)
    ctx.add(trace, "CWIP (Box 4)", box4, "MONEY", Layer.BOX4, basis)
    if isinstance(box4, Fact) and box4.value and abs(box4.value - total) > abs(box4.value) / 100:
        issues.append("AGEING_TOTAL_DIFFERS_FROM_BOX4_CWIP")
    value = q(old * HUNDRED / total)
    return Ev(S.EVALUATED, sb, grade(value, RULES["STALLED_CWIP"].thresholds), value,
              period_label=str(day), period_end=day, trace=trace, issues=issues,
              calculation=f"{old} / {total} x 100",
              explanation=f"{value} % of CWIP is older than 3 years")


# --- credit profile ---------------------------------------------------------------------------


def credit_profile(ctx: Ctx, box12: MarketIntelligenceDataset | None,
                   bundle: EvidenceBundle | None) -> tuple[CreditProfile, list[UUID],
                                                           list[RatingObservation]]:
    metrics: list[ForensicMetric] = []
    end = ctx.latest.get(ctx.analytical)
    if end is not None:
        p, b = fy(ctx, end), pit(ctx, end)
        for code, unit, calc in (
            ("GROSS_DEBT", "MONEY", box5_metric(ctx, "GROSS_DEBT", b, "MONEY")),
            ("NET_DEBT", "MONEY", box5_metric(ctx, "NET_DEBT", b, "MONEY")),
            ("SHORT_TERM_BORROWINGS", "MONEY",
             canonical_value(ctx, [M.SHORT_TERM_BORROWINGS], b)),
            ("LONG_TERM_BORROWINGS", "MONEY", canonical_value(ctx, [M.LONG_TERM_BORROWINGS], b)),
            ("LEASE_LIABILITIES", "MONEY", canonical_value(ctx, [M.LEASE_LIABILITIES], b)),
            ("CASH_AND_CASH_EQUIVALENTS", "MONEY",
             canonical_value(ctx, [M.CASH_AND_CASH_EQUIVALENTS], b)),
            ("BANK_BALANCES", "MONEY", canonical_value(ctx, [M.BANK_BALANCES], b)),
            ("CURRENT_INVESTMENTS", "MONEY", canonical_value(ctx, [M.CURRENT_INVESTMENTS], b)),
            ("CFO", "MONEY", canonical_value(ctx, [M.CFO], p)),
            ("FCF", "MONEY", box5_metric(ctx, "FCF", p, "MONEY")),
            ("DEBT_TO_EQUITY", "TIMES", box5_metric(ctx, "DEBT_TO_EQUITY", p, "TIMES")),
            ("INTEREST_COVERAGE", "TIMES", box5_metric(ctx, "INTEREST_COVERAGE", p, "TIMES")),
            ("NET_DEBT_TO_OPERATING_EBITDA", "TIMES", net_debt_to_operating_ebitda(ctx, end)[0]),
        ):
            layer = Layer.BOX13 if code == "NET_DEBT_TO_OPERATING_EBITDA" else (
                Layer.BOX5 if code in ("GROSS_DEBT", "NET_DEBT", "FCF", "DEBT_TO_EQUITY",
                                       "INTEREST_COVERAGE") else Layer.BOX4)
            metrics.append(metric_of(ctx, f"CREDIT_{code}", calc,
                                     MetricKind.REPORTED if layer == Layer.BOX4
                                     else MetricKind.DERIVED, layer, unit, code,
                                     f"{layer.value}:{code}"))
    adjusted = [_explicit_metric(ctx, "AGENCY_ADJUSTED_DEBT_" + norm(a.agency).upper(),
                                 a.value_crore, a.as_of, fid("agency_debt", a),
                                 f"{a.agency}: {a.definition}", NA) for a in
                sorted(ctx.request.agency_adjusted_debt, key=lambda a: (a.as_of, a.agency))]
    maturities = [_explicit_metric(ctx, f"MATURITY_{m.bucket}", m.amount_crore, m.as_of,
                                   fid("maturity", m), f"debt maturing {m.bucket}",
                                   SignalBasis(m.basis))
                  for m in sorted(ctx.request.debt_maturities, key=lambda m: (m.as_of, m.bucket))]
    ratings = rating_observations(ctx, box12)
    ids: list[UUID] = []
    ev, upgrade = credit_deterioration(ctx, ratings, box12)
    ids.append(ctx.emit("CREDIT_DETERIORATION", ev))
    if upgrade is not None:
        ids.append(ctx.emit("RATING_UPGRADE", upgrade))
    evidence: list[UUID] = []
    if bundle is not None:
        for x in bundle.extractions:
            evidence.extend(e.evidence_id for e in x.evidence
                            if e.kind in (EvidenceKind.LIQUIDITY_DISCLOSURE,
                                          EvidenceKind.RATING_DISCLOSURE,
                                          EvidenceKind.DEBT_DISCLOSURE))
    return CreditProfile(
        metrics=tuple(metrics), ratings=tuple(ratings), agency_adjusted_debt=tuple(adjusted),
        maturities=tuple(maturities),
        liquidity_evidence_ids=tuple(sorted(set(evidence), key=str)[:500]),
        signal_ids=ctx.present(ids)), ids, ratings


def _explicit_metric(ctx: Ctx, code: str, value: Decimal, day: date, record: UUID, label: str,
                     basis: SignalBasis) -> ForensicMetric:
    clean = re.sub(r"[^A-Z0-9_]", "_", code.upper())[:80]
    return ForensicMetric(
        metric_id=uuid5(NS, f"{ctx.dd_key}|explicit|{clean}|{day}|{record}"), code=clean,
        label=label[:1000], kind=MetricKind.REPORTED, layer=Layer.REQUEST, status="AVAILABLE",
        value=q(value), unit="MONEY", period=str(day), period_end=day, basis=basis,
        formula_id="EXPLICIT_INPUT", record_ids=(record,))
