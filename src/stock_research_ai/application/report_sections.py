"""Box 14 section builders: Boxes 4-13 -> claims, typed tables and structured views.

Each builder copies upstream values into claims (never computes a new economic number), keeps
the claim type honest (FACT / CALCULATION / MANAGEMENT_STATEMENT / SCENARIO / DATA_LIMITATION)
and selects a material core layer deterministically; everything else goes to an appendix.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID, uuid5

from stock_research_ai.application.diligence_inputs import FAMILIES, fid
from stock_research_ai.application.red_flags import NS as NS13
from stock_research_ai.application.report_claims import (
    NS,
    Book,
    Resolver,
    basis_of,
    cell,
    col,
    number,
    row,
    text,
    up,
)
from stock_research_ai.domain.business_intelligence import BusinessIntelligenceDataset
from stock_research_ai.domain.document_intelligence import EvidenceBundle
from stock_research_ai.domain.due_diligence import (
    DiligenceSource,
    DueDiligenceDataset,
    ForensicMetric,
    RiskSignal,
    Severity,
    SignalClass,
    SignalStatus,
)
from stock_research_ai.domain.final_research_report import (
    ClaimBasis,
    ClaimType,
    ColumnKind,
    DiligenceEntry,
    DisagreementValueView,
    DisagreementView,
    GuidanceTrackerEntry,
    InputFamily,
    InvestmentCase,
    Layer,
    LimitationEntry,
    MonitoringItem,
    OrderBookView,
    OrderBucket,
    OrderBucketKind,
    Polarity,
    ReconciliationEntry,
    ReportIssue,
    ReportLimits,
    RiskEntry,
    RiskKind,
    RiskPresentation,
    RowKind,
    Scenario,
    ScenarioCase,
    SectionId,
    SectionStatus,
    SourceType,
    TableCell,
    TableRow,
    ThresholdRef,
    UpstreamRecord,
    ValuationMethodView,
    ValuationView,
    ViewChangeEntry,
    ViewDirection,
)
from stock_research_ai.domain.financial_analytics import (
    CalculatedMetric,
    CalculationStatus,
    FinancialAnalyticsDataset,
)
from stock_research_ai.domain.financial_business import RESTRICTED_INDUSTRIES
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalFinancialValue,
    CanonicalMetric,
    Currency,
    ValueKind,
)
from stock_research_ai.domain.financial_charts import FinancialChartBundle
from stock_research_ai.domain.forecasting import ForecastDataset, ForecastValue
from stock_research_ai.domain.fundamental_valuation import ValuationDataset
from stock_research_ai.domain.industry_intelligence import IndustryIntelligenceDataset
from stock_research_ai.domain.management_intelligence import ManagementIntelligenceDataset
from stock_research_ai.domain.market_intelligence import MarketIntelligenceDataset

M = CanonicalMetric
S = SectionId
CT = ClaimType
NA = ClaimBasis.NOT_APPLICABLE

#: deterministic core-event selection (selection rules report-selection/2)
ALWAYS_MATERIAL_EVENTS = (
    "ORDER_CANCELLATION", "FUND_RAISE", "ACQUISITION", "DIVESTMENT", "JV", "AUDITOR_CHANGE",
    "CREDIT_RATING", "BUYBACK", "BONUS", "SPLIT", "RIGHTS", "KMP_CHANGE", "EXCHANGE_PENALTY",
    "LITIGATION", "REGULATORY", "ORDER_REVISION",
)
SIZE_GATED_EVENTS = ("ORDER_AWARD", "LOA", "L1_BIDDER", "MOU", "DIVIDEND")
MATERIAL_EVENT_PCT = Decimal(1)  # stated value >= 1 % of Box 12's reference annual revenue
CORE_GOVERNANCE_KINDS = ("AUDITOR_CHANGE", "KMP_CHANGE", "EXCHANGE_PENALTY", "PENALTY",
                         "LITIGATION", "REGULATORY", "AUDITOR_REPORT", "BOARD")
STATEMENT_PRIORITY = ("GUIDANCE", "TARGET", "CAPITAL_ALLOCATION", "CAPEX", "ORDER_BOOK",
                      "MARGIN", "GROWTH", "DEMAND", "WORKING_CAPITAL", "DEBT", "RISK",
                      "DIVIDEND_BUYBACK", "M_AND_A", "CAPACITY", "PRICING", "COST",
                      "EXPECTATION", "STRATEGY", "GOVERNANCE")
CATEGORY_SECTIONS: dict[str, tuple[SectionId, ...]] = {
    "CASH_FLOW": (S.CASH_FLOW_BALANCE_SHEET, S.ACCOUNTING_FORENSICS),
    "WORKING_CAPITAL": (S.CASH_FLOW_BALANCE_SHEET, S.ACCOUNTING_FORENSICS),
    "ACCOUNTING": (S.ACCOUNTING_FORENSICS,),
    "AUDITOR": (S.ACCOUNTING_FORENSICS, S.MANAGEMENT_GOVERNANCE),
    "FINANCIAL": (S.ACCOUNTING_FORENSICS,),
    "LEVERAGE": (S.CREDIT_LIQUIDITY, S.CASH_FLOW_BALANCE_SHEET),
    "LIQUIDITY": (S.CREDIT_LIQUIDITY,),
    "GOVERNANCE": (S.MANAGEMENT_GOVERNANCE,),
    "MANAGEMENT": (S.MANAGEMENT_GOVERNANCE,),
    "REGULATORY": (S.MANAGEMENT_GOVERNANCE,),
    "LEGAL": (S.MANAGEMENT_GOVERNANCE,),
    "OWNERSHIP": (S.SHAREHOLDING,),
    "ORDER_BOOK": (S.ORDER_BOOK,),
    "CUSTOMER_CONCENTRATION": (S.ORDER_BOOK, S.BUSINESS_MODEL),
    "INDUSTRY": (S.INDUSTRY_COMPETITION,),
    "VALUATION_INPUT": (S.VALUATION,),
}
AUTHORITY_TYPE = {
    "EXCHANGE_FILING": SourceType.FILING, "COMPANY_DOCUMENT": SourceType.COMPANY_DOCUMENT,
    "RATING_AGENCY": SourceType.RATING_AGENCY, "MARKET_PROVIDER": SourceType.PROVIDER_RECORD,
    "SCREENER": SourceType.PROVIDER_RECORD, "ANALYST_INPUT": SourceType.ANALYST_INPUT,
}
LIMITATION_TEXT = {
    "DATA_UNAVAILABLE": "Data unavailable",
    "SOURCE_CONFLICT_UNRESOLVED": "Source conflict unresolved",
    "NOT_COMPARABLE_ACROSS_BASES": "Not comparable across reporting bases",
    "INSUFFICIENT_GUIDANCE_SAMPLE": "Insufficient completed guidance sample",
    "PROVIDER_UNAVAILABLE": "Provider unavailable",
    "LAYER_NOT_SUPPLIED": "Data unavailable (upstream layer not supplied)",
    "APPENDIX_LIMIT_REACHED": "Appendix limit reached; the upstream ledger holds more entries",
    "CORE_EVENT_LIMIT_REACHED": "Core event limit reached; the excluded events stay in the "
                                "event ledger appendix",
}


def limitation_text(code: str, detail: str | None = None) -> str:
    base = LIMITATION_TEXT.get(code, "Data limitation")
    return f"{base}: {detail}" if detail else f"{base}."


def human(code: str) -> str:
    return code.replace("_", " ").lower()


@dataclass
class Draft:
    claims: list[UUID] = field(default_factory=list)
    limits: list[UUID] = field(default_factory=list)
    tables: list[UUID] = field(default_factory=list)
    charts: list[UUID] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    status: SectionStatus | None = None
    reason: str | None = None
    missing: set[InputFamily] = field(default_factory=set)
    not_applicable: set[InputFamily] = field(default_factory=set)
    #: upstream items that do not apply to this company (never counted as missing)
    inapplicable: list[str] = field(default_factory=list)
    #: applicable structured outputs the section owns outside claims / tables / charts
    #: (monitoring items, view-change entries): real section content
    items: list[UUID] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        """Any applicable content: a claim, limitation, table, chart or structured item."""
        return bool(self.claims or self.limits or self.tables or self.charts or self.items)


@dataclass
class Ctx:
    book: Book
    res: Resolver
    as_of: date
    limits: ReportLimits
    box4: CanonicalFinancialDataset
    box5: FinancialAnalyticsDataset
    box13: DueDiligenceDataset
    bundle: EvidenceBundle | None
    box6: FinancialChartBundle | None
    box7: BusinessIntelligenceDataset | None
    box8: ManagementIntelligenceDataset | None
    box9: IndustryIntelligenceDataset | None
    box10: ForecastDataset | None
    box11: ValuationDataset | None
    box12: MarketIntelligenceDataset | None
    analytical: ClaimBasis
    control: ClaimBasis | None
    currency: Currency
    financial: bool
    drafts: dict[SectionId, Draft] = field(default_factory=dict)
    issues: list[ReportIssue] = field(default_factory=list)
    signal_claims: dict[UUID, UUID] = field(default_factory=dict)
    risk_entries: list[RiskEntry] = field(default_factory=list)
    explicit: dict[UUID, tuple[str, Any]] = field(default_factory=dict)
    appendix: dict[str, tuple[list[UUID], int, int, UUID | None]] = field(default_factory=dict)
    b4: dict[tuple[str, str, str, date], CanonicalFinancialValue] = field(default_factory=dict)
    b5: dict[tuple[str, str, str, date], CalculatedMetric] = field(default_factory=dict)
    monitoring_seed: list[tuple[str, str, str, list[UUID], list[UUID], str, str]] = field(
        default_factory=list)

    def __post_init__(self) -> None:
        for s in SectionId:
            self.drafts[s] = Draft()
        for v in self.box4.canonical_values:
            if v.period.period_end is not None:
                self.b4.setdefault((v.metric.value, v.reporting_basis.value,
                                    v.period.period_type.value, v.period.period_end), v)
        for m in _metrics(self.box5):
            if m.period.period_end is not None:
                self.b5.setdefault((m.code, m.reporting_basis.value, m.period.period_type.value,
                                    m.period.period_end), m)
        request = self.box13.request
        for fam in FAMILIES:
            for item in getattr(request, fam.field):
                self.explicit[fid(fam.record, item)] = (fam.field, item)
        for name, family in (("market_cap", "market_cap"), ("statutory_tax_rate",
                             "statutory_tax"), ("leverage_override", "leverage_override")):
            item = getattr(request, name)
            if item is not None:
                self.explicit[uuid5(NS13, f"input|{family}|{item.model_dump_json()}")] = (
                    name, item)

    # --- membership
    def add(self, section: SectionId, *claim_ids: UUID | None) -> None:
        d = self.drafts[section]
        for i in claim_ids:
            if i is None:
                continue
            claim = self.book.get(i)
            target = d.limits if claim.claim_type == CT.DATA_LIMITATION else d.claims
            if i not in target:
                target.append(i)
            self.book.attach((i,), section)

    def limit(self, section: SectionId | None, topic: str, code: str, detail: str | None, *,
              upstream: Iterable[UpstreamRecord] | None = None,
              basis: ClaimBasis = NA) -> UUID:
        ups = tuple(upstream) if upstream is not None else (
            up(Layer.BOX13, "DATASET", self.box13.dataset_id),)
        cid = self.book.limitation(None, topic, limitation_text(code, detail), code,
                                   upstream=ups, basis=basis)
        if section is not None:
            self.add(section, cid)
        return cid

    def not_applicable(self, section: SectionId | None, code: str) -> None:
        """An upstream item that does not apply here: section metadata, never a gap."""
        if section is None:
            return
        d = self.drafts[section]
        d.notes.append(f"NOT_APPLICABLE_{code}"[:80])
        if code not in d.inapplicable:
            d.inapplicable.append(code)

    def item(self, section: SectionId, item_id: UUID) -> None:
        """An applicable structured output of the section (counts as section content)."""
        if item_id not in self.drafts[section].items:
            self.drafts[section].items.append(item_id)

    def inapplicable(self, f: ForensicMetric) -> bool:
        """Box 13 NOT_APPLICABLE, or a gap Box 13 traces (reason code UNSUPPORTED_SECTOR) to
        a Box 5 metric typed UNSUPPORTED_SECTOR: the metric does not apply to this sector."""
        if f.status == "NOT_APPLICABLE":
            return True
        if f.status != "UNAVAILABLE" or (f.reason or "").split(":", 1)[0] != \
                CalculationStatus.UNSUPPORTED_SECTOR.value:
            return False
        return any(m.status == CalculationStatus.UNSUPPORTED_SECTOR
                   for m in (self.res.metrics.get(r) for r in f.record_ids) if m is not None)

    def dataset(self, layer: Layer) -> UUID | None:
        return self.book.dataset_sources.get(layer)

    # --- explicit (cited) Box 13 inputs
    def cited(self, source: DiligenceSource) -> UUID:
        kind = AUTHORITY_TYPE.get(source.authority.value, SourceType.ANALYST_INPUT)
        return self.book.source(Layer.BOX13, kind, f"CITED/{source.authority.value}",
                                url=source.url, document_id=source.document_id,
                                reference=source.reference, title=source.reference)

    # --- claim factories ------------------------------------------------------------------
    def fact(self, v: CanonicalFinancialValue, section: SectionId | None = None) -> UUID:
        sources, ups, evidence = self.res.canonical(v.canonical_value_id)
        unit = unit_of(v)
        cid = self.book.claim(
            None, CT.FACT, v.metric.value,
            f"{v.metric.value} reported {number(v.value)} {unit} for "
            f"{v.period.label} ({v.reporting_basis.value.lower()}).",
            basis=basis_of(v.reporting_basis), upstream=ups, sources=sources, value=v.value,
            metric=v.metric.value, unit=unit, currency=v.currency, period=v.period.label,
            period_end=v.period.period_end, evidence_ids=evidence, confidence=v.confidence)
        if section is not None:
            self.add(section, cid)
        return cid

    def metric(self, m: CalculatedMetric, section: SectionId | None = None) -> UUID | None:
        if m.status != CalculationStatus.CALCULATED or m.value is None:
            return None
        sources, ups, evidence = self.res.metric(m.metric_id)
        if not sources:
            self.issues.append(ReportIssue(code="METRIC_WITHOUT_DOCUMENT_SOURCE",
                                           severity="WARNING", detail=f"{m.code} {m.period.label}"))
            return None
        unit = m.unit.value if m.currency is None or m.unit.value != "MONEY" else \
            f"{m.currency.value} MONEY"
        cid = self.book.claim(
            None, CT.CALCULATION, m.code,
            f"{m.label}: {number(m.value)} {unit.lower()} for {m.period.label} "
            f"({m.reporting_basis.value.lower()}; Box 5 {m.formula_id}).",
            basis=basis_of(m.reporting_basis), upstream=ups, sources=sources, value=m.value,
            metric=m.code, unit=unit, currency=m.currency, period=m.period.label,
            period_end=m.period.period_end, evidence_ids=evidence,
            calculation_ids=(m.metric_id,), formula_ids=(m.formula_id,), confidence=m.confidence)
        if section is not None:
            self.add(section, cid)
        return cid

    def forensic(self, f: ForensicMetric, section: SectionId | None = None) -> UUID | None:
        """A Box 13 forensic / credit metric: the Box 5 claim when Box 5 computed it."""
        if f.status != "AVAILABLE":
            if self.inapplicable(f):  # kept visible as not applicable, never as missing
                self.not_applicable(section, f.code)
                return None
            cid = self.limit(None, f.code, "DATA_UNAVAILABLE",
                             f"{f.label} ({f.period or 'no period'}): {f.reason or 'unavailable'}",
                             upstream=(up(Layer.BOX13, "FORENSIC_METRIC", f.metric_id),),
                             basis=basis_of(f.basis.value))
            if section is not None:
                self.add(section, cid)
            return cid
        if f.layer.value == "BOX5":
            for r in f.record_ids:
                m = self.res.metrics.get(r)
                if m is not None and m.code == f.code and m.value == f.value:
                    return self.metric(m, section)
        if f.layer.value == "BOX4" and len(f.record_ids) == 1 and \
                f.record_ids[0] in self.res.values:
            v = self.res.values[f.record_ids[0]]
            if v.value == f.value:
                return self.fact(v, section)
        sources, ups, evidence = self.res.records(f.record_ids)
        sources += tuple(self.cited(self.explicit[r][1].source) for r in f.record_ids
                         if r in self.explicit)
        if not sources:
            return None
        cid = self.book.claim(
            None, CT.CALCULATION, f.code,
            f"{f.label}: {number(f.value)} {f.unit.lower()} ({f.period or 'no period'}, "
            f"{f.basis.value.lower()}; Box 13 {f.formula_id}).",
            basis=basis_of(f.basis.value), value=f.value, metric=f.code, unit=f.unit,
            period=f.period, period_end=f.period_end, sources=sources, evidence_ids=evidence,
            upstream=(up(Layer.BOX13, "FORENSIC_METRIC", f.metric_id), *ups),
            calculation_ids=(f.metric_id,), formula_ids=(f.formula_id,))
        if section is not None:
            self.add(section, cid)
        return cid

    def box12_label(self, label: str) -> list[UUID]:
        """A Box 13 'BOX12:<kind>:<id>' / 'BOX12:<dataset>:<scope>' label -> the exchange
        records behind it (a dataset-wide reading rests on every fetched feed payload)."""
        b12 = self.box12
        parts = label.split(":")
        if b12 is None or len(parts) != 3:
            return []
        kind, ident = parts[1], parts[2]
        refs: list[Any] = []
        if kind == "filing":
            refs = [r for f in b12.filings if str(f.document_id) == ident for r in f.sources]
        elif kind == "shareholding":
            refs = [r for f in b12.shareholding if str(f.filing_id) == ident for r in f.sources]
        elif kind == "insider":
            refs = [r for t in b12.insider_trades if str(t.record_id) == ident
                    for r in t.sources]
        elif kind == str(b12.dataset_id):
            return [x for x in (self.res.payload(p.payload_id) for p in b12.payloads
                                if p.status.value == "OK") if x]
        return list(self.res.refs(refs))

    def signal_sources(self, s: RiskSignal) -> tuple[UUID, ...]:
        out: list[UUID] = []
        for label in s.source_ids:
            resolved = self.box12_label(label) if label.startswith("BOX12:") else []
            out += resolved or [self.res.label(label, Layer.BOX13, "SIGNAL_SOURCE")]
        out += list(self.res.records(s.record_ids)[0])
        out += [self.cited(self.explicit[r][1].source) for r in s.record_ids
                if r in self.explicit]
        out += [e for e in (self.res.evidence(x) for x in s.evidence_ids) if e]
        return tuple(dict.fromkeys(out))


def unit_of(v: CanonicalFinancialValue) -> str:
    if v.value_kind == ValueKind.MONEY:
        return f"{v.currency.value} {v.scale.value}"
    if v.value_kind == ValueKind.PER_SHARE:
        return f"{v.currency.value} per share"
    return v.value_kind.value


def _metrics(box5: FinancialAnalyticsDataset) -> list[CalculatedMetric]:
    out: list[CalculatedMetric] = []
    for name in ("building_blocks", "growth", "profitability", "returns", "dupont", "leverage",
                 "liquidity", "working_capital", "cash_flow_quality", "capital_allocation",
                 "accruals", "banking"):
        out.extend(getattr(box5, name))
    return out


# --- risk register (built first: every section cites it) ------------------------------------------


def build_signals(ctx: Ctx) -> None:
    d = ctx.box13
    likelihood = {e.risk_id: e for e in d.risk_map}
    for s in d.signals:
        polarity = Polarity.NEUTRAL
        if s.signal_class == SignalClass.POSITIVE_CONTROL:
            polarity = Polarity.POSITIVE
        elif s.severity in (Severity.YELLOW, Severity.RED):
            polarity = Polarity.NEGATIVE
        ups = (up(Layer.BOX13, "SIGNAL", s.risk_id),)
        gated = s.status == SignalStatus.UNSUPPORTED_SECTOR_RULE
        if s.status in (SignalStatus.EVALUATED, SignalStatus.INFORMATIONAL,
                        SignalStatus.NOT_APPLICABLE) or gated:
            grade = s.severity.value if s.severity else s.status.value
            sources = ctx.signal_sources(s) if s.value is not None else ()
            value = s.value if sources else None
            extra: tuple[str, ...] = ()
            if s.value is not None and not sources:
                extra = ("VALUE_WITHOUT_DOCUMENT_SOURCE",)
                ctx.issues.append(ReportIssue(code="SIGNAL_VALUE_WITHOUT_SOURCE",
                                              severity="WARNING", detail=s.code))
            cid = ctx.book.claim(
                None, CT.CALCULATION, s.code,
                f"{s.code} ({s.signal_class.value.lower().replace('_', ' ')}, "
                f"{s.category.value.lower()}): {grade}; {s.metric} = {number(value)} "
                f"{s.unit.lower()} ({s.period or 'no period'}, {s.basis.value.lower()}; rule "
                f"{s.rule_id})." + (f" Not applicable: {s.reason}." if s.reason else ""),
                basis=basis_of(s.basis.value), upstream=ups, sources=sources, value=value,
                metric=s.metric, unit=s.unit, period=s.period, period_end=s.period_end,
                evidence_ids=(), calculation_ids=(s.risk_id,), formula_ids=(s.formula_id,),
                risk_ids=(s.risk_id,), confidence=s.confidence, polarity=polarity,
                source_text=s.explanation,
                limitations=(("NOT_APPLICABLE",) if s.status == SignalStatus.NOT_APPLICABLE
                             else ("NOT_APPLICABLE", s.status.value) if gated else ()) + extra)
        else:
            code = "SOURCE_CONFLICT_UNRESOLVED" if s.reason == "INPUT_CONFLICT" else \
                (s.reason or s.status.value)
            cid = ctx.book.claim(
                None, CT.DATA_LIMITATION, s.code,
                limitation_text("SOURCE_CONFLICT_UNRESOLVED" if s.reason == "INPUT_CONFLICT"
                                else "DATA_UNAVAILABLE",
                                f"rule {s.code} is {s.status.value.lower()} ({s.reason})"),
                basis=basis_of(s.basis.value), upstream=ups, limitations=(code,))
        inputs: list[UUID] = []
        if s.severity in (Severity.YELLOW, Severity.RED) or (
                s.signal_class == SignalClass.POSITIVE_CONTROL):
            inputs = signal_inputs(ctx, s)
        m = likelihood.get(s.risk_id)
        ctx.signal_claims[s.risk_id] = cid
        ctx.risk_entries.append(RiskEntry(
            risk_id=s.risk_id, code=s.code,
            kind=RiskKind.POSITIVE_CONTROL if s.signal_class == SignalClass.POSITIVE_CONTROL
            else RiskKind.COMPANY_RISK, category=s.category.value, status=s.status.value,
            severity=s.severity.value if s.severity else None, reason=s.reason, claim_id=cid,
            input_claim_ids=tuple(inputs), mitigating_risk_ids=s.mitigating_signal_ids,
            likelihood=m.likelihood.value if m else None, impact=m.impact.value if m else None))
        for section in CATEGORY_SECTIONS.get(s.category.value, ()):
            if gated:  # the rule does not apply to this sector: section metadata only
                ctx.not_applicable(section, s.code)
            else:
                ctx.add(section, cid)


def signal_inputs(ctx: Ctx, s: RiskSignal) -> list[UUID]:
    """The calculation inputs of a flag / control, each as its own (deduplicated) claim."""
    out: list[UUID] = []
    for i in s.inputs:
        if i.value is None or i.record_id is None:
            continue
        rid = i.record_id
        if rid in ctx.res.values and ctx.res.values[rid].value == i.value:
            out.append(ctx.fact(ctx.res.values[rid]))
            continue
        if rid in ctx.res.metrics and ctx.res.metrics[rid].value == i.value:
            m = ctx.metric(ctx.res.metrics[rid])
            if m is not None:
                out.append(m)
            continue
        if rid in ctx.explicit:
            family, item = ctx.explicit[rid]
            source = ctx.cited(item.source)
            kind = CT.FACT if ctx.book.get_source_type(source) != SourceType.ANALYST_INPUT \
                else CT.CALCULATION
            if kind == CT.CALCULATION:
                continue  # an uncited analyst number is never presented as a company fact
            out.append(ctx.book.claim(
                None, CT.FACT, f"INPUT_{family.upper()}"[:80],
                f"Cited input ({human(family)}): {i.role} = {number(i.value)} {i.unit.lower()}"
                f"{' (' + i.period + ')' if i.period else ''}.",
                basis=basis_of(i.basis.value), upstream=(up(Layer.BOX13, "REQUEST_INPUT", rid),),
                sources=(source,), value=i.value, unit=i.unit, period=i.period,
                period_end=i.period_end, evidence_ids=getattr(item.source, "evidence_ids", ()),
                source_text=getattr(item.source, "reference", None)))
    return list(dict.fromkeys(out))


# --- 2. company overview --------------------------------------------------------------------------


def overview(ctx: Ctx) -> None:
    sec = S.COMPANY_OVERVIEW
    d = ctx.drafts[sec]
    b12 = ctx.box12
    if b12 is not None:
        ident = b12.identity
        sources = tuple(s for s in (ctx.res.payload(e.payload_id) for e in ident.evidence
                                    if e.payload_id) if s)
        if sources:
            for name, value in (("ISIN", ident.isin), ("NSE_SYMBOL", ident.nse_symbol),
                                ("BSE_CODE", ident.bse_code)):
                if value is not None:
                    ctx.add(sec, ctx.book.claim(
                        None, CT.FACT, f"IDENTITY_{name}",
                        f"Verified {human(name)} (Box 12 identity {ident.status.value}).",
                        basis=NA, upstream=(up(Layer.BOX12, "IDENTITY", ident.identity_id),),
                        sources=sources, source_text=value))
        if ident.status.value != "RESOLVED":
            ctx.limit(sec, "IDENTITY_PARTIAL", "DATA_UNAVAILABLE",
                      f"Box 12 identity is {ident.status.value}",
                      upstream=(up(Layer.BOX12, "IDENTITY", ident.identity_id),))
    else:
        d.missing.add(InputFamily.BOX12_MARKET)
    b7 = ctx.box7
    if b7 is None:
        d.missing.add(InputFamily.BOX7_BUSINESS)
        ctx.limit(sec, "BUSINESS_DESCRIPTION", "LAYER_NOT_SUPPLIED",
                  "Box 7 business intelligence")
    else:
        for c in b7.business_structure.classification:
            src = ctx.book.source(Layer.BOX7, SourceType.PROVIDER_RECORD, "CLASSIFICATION",
                                  url=c.source_url, reference=c.name)
            ctx.add(sec, ctx.book.claim(
                None, CT.FACT, "CLASSIFICATION", f"Classification ({c.level}).", basis=NA,
                upstream=(up(Layer.BOX7, "DATASET", b7.dataset_id),), sources=(src,),
                source_text=c.name))
        kinds = ("BUSINESS_DESCRIPTION", "PRODUCT_SERVICE", "END_MARKET", "CUSTOMER_CHANNEL",
                 "CAPACITY", "PROJECT")
        for kind in kinds:
            items = sorted((e for e in b7.business_evidence if e.evidence_kind.value == kind),
                           key=lambda e: (e.source.page_start or 0, str(e.evidence_id)))
            for e in items[:3 if kind in ("CAPACITY", "PROJECT") else 2]:
                ctx.add(S.BUSINESS_MODEL if kind in ("END_MARKET", "CUSTOMER_CHANNEL")
                        else sec, business_evidence(ctx, e))
            if not items and kind in ("BUSINESS_DESCRIPTION", "PRODUCT_SERVICE"):
                ctx.limit(sec, kind, "DATA_UNAVAILABLE", f"no {human(kind)} evidence in Box 7")
    # group structure: JV / associate share of profit and NCI, as reported
    latest = ctx.box13.latest_fiscal_year_end
    if latest is not None:
        for metric in (M.SHARE_OF_ASSOCIATES_JV, M.MINORITY_INTEREST):
            v = ctx.b4.get((metric.value, ctx.analytical.value, "FY", latest))
            if v is not None:
                ctx.fact(v, sec)
    for series in ctx.box13.ownership_risks.series:
        if series.category in ("PROMOTER", "GOVERNMENT") and series.points:
            ctx.add(sec, ownership_point(ctx, series.category, series.points[-1]))
    if ctx.box13.business_model.value == "ORDER_BOOK_DRIVEN" and \
            ctx.box13.request.business_model_source is not None:
        src = ctx.cited(ctx.box13.request.business_model_source)
        ctx.add(sec, ctx.book.claim(
            None, CT.FACT, "BUSINESS_MODEL_ORDER_BOOK_DRIVEN",
            "Order-book-driven business model, as cited to Box 13.", basis=NA,
            upstream=(up(Layer.BOX13, "DATASET", ctx.box13.dataset_id),), sources=(src,),
            source_text=ctx.box13.request.business_model_source.reference))
    ctx.limit(sec, "CUSTOMERS_HISTORY", "DATA_UNAVAILABLE",
              "company history, named customers and subsidiaries are not extracted upstream")


def business_evidence(ctx: Ctx, e: Any) -> UUID:
    src = ctx.res.location(e.source)
    return ctx.book.claim(
        None, CT.FACT, f"BUSINESS_{e.evidence_kind.value}"[:80],
        f"{human(e.evidence_kind.value).capitalize()} disclosed by the company "
        f"(p.{e.source.page_start or '-'}).", basis=NA,
        upstream=(up(Layer.BOX7, "BUSINESS_EVIDENCE", e.evidence_id),), sources=(src,),
        source_text=e.raw_text, confidence=e.confidence)


def ownership_point(ctx: Ctx, category: str, p: Any) -> UUID:
    layer = Layer.BOX12 if p.layer.value == "BOX12" else Layer.BOX13
    kind = "SHAREHOLDING_FILING" if layer == Layer.BOX12 else "REQUEST_INPUT"
    if p.record_id in ctx.explicit:
        source = ctx.cited(ctx.explicit[p.record_id][1].source)
    else:
        resolved = ctx.box12_label(p.source_id) if p.source_id.startswith("BOX12:") else []
        source = resolved[0] if resolved else ctx.res.label(p.source_id, layer, "SHAREHOLDING")
    return ctx.book.claim(
        None, CT.FACT, f"HOLDING_{category}"[:80],
        f"{human(category).capitalize()} holding {number(p.pct)} % at {p.period_end}.",
        basis=NA, upstream=(up(layer, kind, p.record_id),), sources=(source,), value=p.pct,
        unit="PERCENT", period=str(p.period_end), period_end=p.period_end)


# --- 3. business model and segments ---------------------------------------------------------------


COST_DRIVERS = ("EMPLOYEE_COST_TO_REVENUE", "RAW_MATERIAL_COST_TO_REVENUE",
                "OTHER_EXPENSES_TO_REVENUE", "OTHER_OPERATING_COST_TO_REVENUE",
                "DEPRECIATION_TO_REVENUE", "FINANCE_COST_TO_REVENUE", "CAPEX_TO_REVENUE",
                "ASSET_TURNOVER", "FIXED_ASSET_TURNOVER", "NWC_TO_REVENUE",
                "CASH_CONVERSION_CYCLE")


def business(ctx: Ctx) -> None:
    sec = S.BUSINESS_MODEL
    d = ctx.drafts[sec]
    latest = ctx.box13.latest_fiscal_year_end
    rows: list[TableRow] = []
    if latest is not None:
        for code in COST_DRIVERS:
            m = ctx.b5.get((code, ctx.analytical.value, "FY", latest))
            cid = ctx.metric(m, sec) if m else None
            not_app = m is not None and m.status in (CalculationStatus.NOT_APPLICABLE,
                                                     CalculationStatus.UNSUPPORTED_SECTOR)
            rows.append(row(code, human(code).capitalize(), RowKind.CALCULATION, [
                cell("LATEST", ctx.book, cid, note=m.status.value if m else "NOT_CALCULATED",
                     not_applicable=not_app)], unit=m.unit.value if m else None))
        ctx_table(ctx, sec, "COST_AND_CAPITAL_INTENSITY",
                  "Cost structure, capital intensity and working-capital model", ctx.analytical,
                  [col("LATEST", f"FY ending {latest}", ColumnKind.HISTORICAL,
                       period_end=latest)], rows)
    b7 = ctx.box7
    if b7 is None:
        d.missing |= {InputFamily.BOX7_BUSINESS, InputFamily.SEGMENT_DATA}
    else:
        for kind in ("SEGMENT_DESCRIPTION", "CUSTOMER_CONCENTRATION", "ORDER_BOOK"):
            items = sorted((e for e in b7.business_evidence if e.evidence_kind.value == kind),
                           key=lambda e: (e.source.page_start or 0, str(e.evidence_id)))
            for e in items[:2]:
                ctx.add(sec, business_evidence(ctx, e))
        segments(ctx, b7)
    for kind in ("PRICING_MODEL", "CYCLICALITY", "COMPETITIVE_ADVANTAGES"):
        ctx.limit(sec, kind, "DATA_UNAVAILABLE",
                  f"{human(kind)}: no upstream-supported company evidence")


def segments(ctx: Ctx, b7: BusinessIntelligenceDataset) -> None:
    sec = S.BUSINESS_MODEL
    values = [v for v in b7.canonical_segment_values
              if basis_of(v.reporting_basis) == ctx.analytical
              and v.period.period_type.value == "FY" and v.period.period_end is not None
              and v.period.period_end <= ctx.as_of and v.segment_role.value == "REPORTABLE"]
    if not values:
        ctx.drafts[sec].missing.add(InputFamily.SEGMENT_DATA)
        ctx.limit(sec, "SEGMENT_DATA", "DATA_UNAVAILABLE",
                  "no reportable segment values on the analytical basis")
        return
    ends = sorted({v.period.period_end for v in values if v.period.period_end})[-3:]
    rows: list[TableRow] = []
    keyed = {(v.segment_key, v.metric.value, v.period.period_end): v for v in values}
    names = sorted({(v.segment_key, v.segment_name) for v in values})
    for key, name in names:
        for metric in ("REVENUE", "SEGMENT_RESULT", "EBIT", "ASSETS", "CAPITAL_EMPLOYED"):
            if not any((key, metric, e) in keyed for e in ends):
                continue
            cells: list[TableCell] = []
            for e in ends:
                v = keyed.get((key, metric, e))
                cid = segment_value(ctx, v) if v else None
                cells.append(cell(f"FY_{e.year}", ctx.book, cid, note="NOT_REPORTED"))
            rows.append(row(f"{key}|{metric}", f"{name}: {human(metric)}", RowKind.HISTORICAL,
                            cells))
    analytics = [a for a in b7.segment_analytics if a.status.value == "CALCULATED"
                 and a.value is not None and basis_of(a.reporting_basis) == ctx.analytical
                 and a.period.period_end in ends]
    for a in analytics:
        sources = tuple(s for s in (ctx.res.location(ctx.res.box7_obs[o]) for o in
                                    a.input_observation_ids if o in ctx.res.box7_obs) if s)
        if not sources:
            continue
        ctx.add(sec, ctx.book.claim(
            None, CT.CALCULATION, f"SEGMENT_{a.code}"[:80],
            f"{a.subject_name}: {human(a.code)} {number(a.value)} % ({a.period.label}).",
            basis=ctx.analytical, upstream=(up(Layer.BOX7, "SEGMENT_ANALYTIC", a.analytic_id),),
            sources=sources, value=a.value, unit="PERCENT", period=a.period.label,
            period_end=a.period.period_end, calculation_ids=(a.analytic_id,),
            formula_ids=(a.formula_id,)))
    ctx_table(ctx, sec, "SEGMENTS", "Segment analysis", ctx.analytical,
              [col(f"FY_{e.year}", f"FY ending {e}", ColumnKind.HISTORICAL, period_end=e)
               for e in ends], rows)
    geographies(ctx, b7)


def geographies(ctx: Ctx, b7: BusinessIntelligenceDataset) -> None:
    sec = S.BUSINESS_MODEL
    values = [v for v in b7.canonical_geography_values
              if basis_of(v.reporting_basis) in (ctx.analytical, NA)
              and v.metric.value == "REVENUE" and v.period.period_type.value == "FY"
              and v.period.period_end is not None and v.period.period_end <= ctx.as_of]
    if not values:
        return
    latest = max(v.period.period_end for v in values if v.period.period_end)
    rows: list[TableRow] = []
    for v in sorted((v for v in values if v.period.period_end == latest),
                    key=lambda x: (x.is_total, x.geography_name)):
        loc = ctx.res.box7_obs.get(v.selected_observation_id)
        if loc is None:
            continue
        cid = ctx.book.claim(
            None, CT.FACT, "GEOGRAPHY_REVENUE",
            f"Revenue attributed to {v.geography_name} ({v.attribution.value.lower()}): "
            f"{number(v.value)} {v.currency.value} {v.scale.value} ({v.period.label}).",
            basis=basis_of(v.reporting_basis), value=v.value,
            upstream=(up(Layer.BOX7, "GEOGRAPHY_VALUE", v.canonical_value_id),),
            sources=(ctx.res.location(loc),), unit=f"{v.currency.value} {v.scale.value}",
            period=v.period.label, period_end=v.period.period_end, confidence=v.confidence)
        rows.append(row(v.geography_key, v.geography_name, RowKind.HISTORICAL,
                        [cell("REVENUE", ctx.book, cid)]))
    ctx_table(ctx, sec, "GEOGRAPHY", f"Revenue by geography (FY ending {latest})",
              ctx.analytical, [col("REVENUE", "Revenue", ColumnKind.HISTORICAL,
                                   period_end=latest)], rows)


def segment_value(ctx: Ctx, v: Any) -> UUID | None:
    loc = ctx.res.box7_obs.get(v.selected_observation_id)
    if loc is None:
        return None
    return ctx.book.claim(
        None, CT.FACT, f"SEGMENT_{v.metric.value}"[:80],
        f"Segment {v.segment_name}: {human(v.metric.value)} {number(v.value)} "
        f"{v.currency.value} {v.scale.value} ({v.period.label}).",
        basis=basis_of(v.reporting_basis), value=v.value,
        upstream=(up(Layer.BOX7, "SEGMENT_VALUE", v.canonical_value_id),),
        sources=(ctx.res.location(loc),), unit=f"{v.currency.value} {v.scale.value}",
        period=v.period.label, period_end=v.period.period_end, confidence=v.confidence)


def ctx_table(ctx: Ctx, section: SectionId, code: str, title: str, basis: ClaimBasis,
              columns: list[Any], rows: list[TableRow], *, appendix: bool = False,
              scenario: Scenario | None = None, footnotes: Iterable[str] = (),
              upstream: int | None = None) -> UUID | None:
    """A typed table. An appendix ledger longer than the bound keeps its first rows and
    surfaces the truncation as a data limitation (never silently)."""
    if not rows:
        return None
    held = upstream if upstream is not None else len(rows)
    limitation = None
    if appendix and held > len(rows):
        limitation = ctx.limit(S.DATA_QUALITY_RECONCILIATION, f"APPENDIX_{code}"[:80],
                               "APPENDIX_LIMIT_REACHED",
                               f"{len(rows)} of {held} {human(code)} entries listed")
    tid = ctx.book.table(section, code, title, basis, columns, rows, appendix=appendix,
                         scenario=scenario, footnotes=footnotes, truncated=held > len(rows))
    if appendix:
        ctx.drafts[S.APPENDICES].tables.append(tid)
        ctx.appendix[code] = ([r.claim_id for r in rows if r.claim_id], len(rows), held,
                              limitation)
    else:
        ctx.drafts[section].tables.append(tid)
        for r in rows:
            for c in (r.claim_id, *(x.claim_id for x in r.cells)):
                if c is not None:
                    ctx.add(section, c)
    return tid


# --- 4. industry (Box 9) --------------------------------------------------------------------------


def industry(ctx: Ctx) -> None:
    sec = S.INDUSTRY_COMPETITION
    b9 = ctx.box9
    if b9 is None:
        ctx.drafts[sec].missing.add(InputFamily.BOX9_INDUSTRY)
        ctx.limit(sec, "INDUSTRY_INTELLIGENCE", "LAYER_NOT_SUPPLIED",
                  "Box 9 industry intelligence (industry structure, peers, market size)")
        return
    dataset = (up(Layer.BOX9, "DATASET", b9.dataset_id),)
    for c in b9.classification:
        src = ctx.book.source(Layer.BOX9, SourceType.PROVIDER_RECORD, "CLASSIFICATION",
                              url=c.url, reference=c.name)
        ctx.add(sec, ctx.book.claim(None, CT.FACT, "INDUSTRY_CLASSIFICATION",
                                    f"Industry classification ({c.level}).", basis=NA,
                                    upstream=dataset, sources=(src,), source_text=c.name))
    for o in sorted(b9.structure_observations, key=lambda x: (x.topic, str(x.observation_id))):
        claim = industry_text(ctx, o.nature.value, o.source, f"INDUSTRY_{o.topic}"[:80],
                              f"Industry structure ({human(o.topic)}).", o.statement,
                              up(Layer.BOX9, "STRUCTURE_OBSERVATION", o.observation_id))
        ctx.add(sec, claim)
    for sig in (*b9.demand_signals, *b9.pricing_signals):
        ctx.add(sec, industry_text(ctx, sig.nature.value, sig.source,
                                   f"INDUSTRY_{sig.kind}_SIGNAL",
                                   f"{sig.kind.capitalize()} signal: "
                                   f"{sig.direction.value.lower()}.",
                                   sig.quote, up(Layer.BOX9, "MARKET_SIGNAL", sig.signal_id)))
    for mc in b9.market_claims:
        if mc.company_key != ctx.box4.company_key:
            continue
        ctx.add(sec, industry_text(ctx, mc.nature.value, mc.source, "MARKET_POSITION_CLAIM",
                                   f"Market-position claim ({mc.status.value.lower()}).",
                                   mc.quote, up(Layer.BOX9, "MARKET_CLAIM", mc.claim_id)))
    rows: list[TableRow] = []
    pages = {c.peer_company_key: (c.table_ref.company_url or c.table_ref.source_url)
             for c in b9.candidates if c.table_ref is not None}
    for b in sorted(b9.financial_benchmarks, key=lambda x: (x.metric_code, x.period_end)):
        peer_sources = [ctx.book.source(Layer.BOX9, SourceType.PROVIDER_RECORD, "PEER_DATA",
                                        url=(p.source.source_url if p.source else None) or
                                        pages.get(p.company_key), reference=p.company_key)
                        for p in b.peers if p.status.value == "COMPARABLE" and (
                            (p.source is not None and p.source.source_url)
                            or pages.get(p.company_key))]
        own = [x for x in b.target.upstream_ids if x in ctx.res.values or x in ctx.res.metrics]
        target = None
        if b.target.value is not None and own:
            s_, u_, e_ = ctx.res.records(own[:1])
            target = ctx.book.claim(
                None, CT.CALCULATION, f"PEER_TARGET_{b.metric_code}"[:80],
                f"Company {human(b.metric_code)} in the peer benchmark: "
                f"{number(b.target.value)} ({b.period_end}).", basis=basis_of(
                    b.reporting_basis), upstream=(up(Layer.BOX9, "BENCHMARK", b.benchmark_id),
                                                  *u_),
                sources=s_, value=b.target.value, unit=b.unit, period=str(b.period_end),
                period_end=b.period_end, calculation_ids=(b.benchmark_id,),
                formula_ids=(f"BOX9/{b.layer}",))
        median = None
        if b.statistics.median is not None and peer_sources:
            median = ctx.book.claim(
                None, CT.CALCULATION, f"PEER_MEDIAN_{b.metric_code}"[:80],
                f"Peer median {human(b.metric_code)}: {number(b.statistics.median)} over "
                f"{b.statistics.peer_count} peers ({b.period_end}).",
                basis=basis_of(b.reporting_basis),
                upstream=(up(Layer.BOX9, "BENCHMARK", b.benchmark_id),), sources=peer_sources,
                value=b.statistics.median, unit=b.unit, period=str(b.period_end),
                period_end=b.period_end, calculation_ids=(b.benchmark_id,),
                formula_ids=("BOX9/PEER_MEDIAN",))
        if basis_of(b.reporting_basis) != ctx.analytical:
            continue
        rows.append(row(f"{b.metric_code}|{b.period_end}", human(b.metric_code).capitalize(),
                        RowKind.CALCULATION, [cell("COMPANY", ctx.book, target),
                                              cell("PEER_MEDIAN", ctx.book, median)],
                        unit=b.unit))
    ctx_table(ctx, sec, "PEER_BENCHMARKS", "Peer financial benchmarks (Box 9)", ctx.analytical,
              [col("COMPANY", "Company"), col("PEER_MEDIAN", "Peer median")], rows)
    for n in b9.not_applicable_metrics:
        ctx.drafts[sec].notes.append(f"NOT_APPLICABLE_{n.metric_code}"[:80])


def industry_text(ctx: Ctx, nature: str, ref: Any, topic: str, statement: str, quote: str,
                  record: UpstreamRecord) -> UUID | None:
    """Box 9 text by its nature: a company assertion is a management statement (via Box 8)."""
    ups = [record]
    if ref.layer.value in ("BOX8_STATEMENT", "BOX8_TURN") and ref.record_id is not None and \
            ctx.box8 is not None:
        ups.append(up(Layer.BOX8, "STATEMENT" if ref.layer.value == "BOX8_STATEMENT" else
                      "TURN", ref.record_id))
    if nature == "ANALYST_QUESTION":
        return None  # a question is not evidence about the company or the industry
    src = ctx.book.source(Layer.BOX9, SourceType.FILING if ref.source_url else
                          SourceType.UPSTREAM_DATASET, f"{ref.layer.value}/{nature}",
                          url=ref.source_url, reference=ref.locator or str(record.record_id))
    if nature == "COMPANY_ASSERTION":
        if len(ups) == 1:
            return None
        return ctx.book.claim(None, CT.MANAGEMENT_STATEMENT, topic, statement, basis=NA,
                              upstream=ups, sources=(src,), source_text=quote)
    return ctx.book.claim(None, CT.FACT, topic, statement, basis=NA, upstream=ups,
                          sources=(src,), source_text=quote)


# --- 5 / 6. historical financials, quarterly, cash flow and balance sheet -------------------------

Spec = tuple[str, str, str, tuple[str, ...], str]  # key, label, layer, codes, period type
ANNUAL: tuple[Spec, ...] = (
    ("REVENUE", "Revenue", "B4", ("REVENUE_FROM_OPERATIONS", "REVENUE"), "FY"),
    ("REVENUE_GROWTH_YOY", "Revenue growth (YoY)", "B5", ("REVENUE_GROWTH_YOY",), "FY"),
    ("OPERATING_PROFIT", "Operating profit", "B4", ("OPERATING_PROFIT",), "FY"),
    ("OPERATING_MARGIN", "Operating margin", "B5", ("OPERATING_MARGIN",), "FY"),
    ("EBITDA", "EBITDA", "B5", ("EBITDA",), "FY"),
    ("EBIT", "EBIT", "B5", ("EBIT",), "FY"),
    ("OTHER_INCOME", "Other income", "B4", ("OTHER_INCOME",), "FY"),
    ("FINANCE_COST", "Finance cost", "B4", ("FINANCE_COST",), "FY"),
    ("PBT", "PBT", "B4", ("PBT",), "FY"),
    ("PAT", "PAT", "B4", ("PAT",), "FY"),
    ("PAT_ATTRIBUTABLE", "PAT attributable to owners", "B4", ("PAT_ATTRIBUTABLE_TO_OWNERS",),
     "FY"),
    ("EPS", "EPS", "B4", ("EPS", "DILUTED_EPS", "BASIC_EPS"), "FY"),
    ("CFO", "Cash from operations", "B4", ("CFO",), "FY"),
    ("CAPEX", "Capex", "B5", ("CAPEX",), "FY"),
    ("FCF", "Free cash flow", "B5", ("FCF",), "FY"),
    ("TOTAL_ASSETS", "Total assets", "B4", ("TOTAL_ASSETS",), "POINT_IN_TIME"),
    ("EQUITY", "Shareholders' equity", "B5", ("SHAREHOLDERS_EQUITY",), "POINT_IN_TIME"),
    ("GROSS_DEBT", "Gross debt", "B5", ("GROSS_DEBT",), "POINT_IN_TIME"),
    ("CASH", "Cash and equivalents", "B4", ("CASH_AND_CASH_EQUIVALENTS",), "POINT_IN_TIME"),
    ("NET_DEBT", "Net debt", "B5", ("NET_DEBT",), "POINT_IN_TIME"),
    ("NET_WORKING_CAPITAL", "Net working capital", "B5", ("NET_WORKING_CAPITAL",),
     "POINT_IN_TIME"),
    ("ROE", "ROE", "B5", ("ROE",), "FY"),
    ("ROCE", "ROCE", "B5", ("ROCE",), "FY"),
)
QUARTERLY: tuple[Spec, ...] = (
    ("REVENUE", "Revenue", "B4", ("REVENUE_FROM_OPERATIONS", "REVENUE"), "QUARTER"),
    ("REVENUE_GROWTH_YOY", "Revenue growth (YoY)", "B5", ("REVENUE_GROWTH_YOY",), "QUARTER"),
    ("REVENUE_GROWTH_QOQ", "Revenue growth (QoQ)", "B5", ("REVENUE_GROWTH_QOQ",), "QUARTER"),
    ("OPERATING_PROFIT", "Operating profit", "B4", ("OPERATING_PROFIT",), "QUARTER"),
    ("OPERATING_MARGIN", "Operating margin", "B5", ("OPERATING_MARGIN",), "QUARTER"),
    ("OTHER_INCOME", "Other income", "B4", ("OTHER_INCOME",), "QUARTER"),
    ("PBT", "PBT", "B4", ("PBT",), "QUARTER"),
    ("PAT", "PAT", "B4", ("PAT",), "QUARTER"),
    ("PAT_GROWTH_YOY", "PAT growth (YoY)", "B5", ("PAT_GROWTH_YOY",), "QUARTER"),
    ("EPS", "EPS", "B4", ("EPS", "DILUTED_EPS", "BASIC_EPS"), "QUARTER"),
)
CASH_BALANCE: tuple[Spec, ...] = (
    ("CFO", "Cash from operations", "B4", ("CFO",), "FY"),
    ("CFO_TO_PAT", "CFO / PAT", "B5", ("CFO_TO_PAT",), "FY"),
    ("CFO_TO_EBITDA", "CFO / EBITDA", "B5", ("CFO_TO_EBITDA",), "FY"),
    ("CAPEX", "Capex", "B5", ("CAPEX",), "FY"),
    ("FCF", "Free cash flow", "B5", ("FCF",), "FY"),
    ("DIVIDENDS_PAID", "Dividends paid", "B5", ("DIVIDENDS_PAID",), "FY"),
    ("TRADE_RECEIVABLES", "Trade receivables", "B4", ("TRADE_RECEIVABLES",), "POINT_IN_TIME"),
    ("INVENTORY", "Inventory", "B4", ("INVENTORY",), "POINT_IN_TIME"),
    ("TRADE_PAYABLES", "Trade payables", "B4", ("TRADE_PAYABLES",), "POINT_IN_TIME"),
    ("RECEIVABLE_DAYS", "Receivable days", "B5", ("RECEIVABLE_DAYS",), "FY"),
    ("INVENTORY_DAYS", "Inventory days", "B5", ("INVENTORY_DAYS",), "FY"),
    ("PAYABLE_DAYS", "Payable days", "B5", ("PAYABLE_DAYS",), "FY"),
    ("CASH_CONVERSION_CYCLE", "Cash conversion cycle", "B5", ("CASH_CONVERSION_CYCLE",), "FY"),
    ("NWC_TO_REVENUE", "Net working capital / revenue", "B5", ("NWC_TO_REVENUE",), "FY"),
    ("GROSS_DEBT", "Gross debt", "B5", ("GROSS_DEBT",), "POINT_IN_TIME"),
    ("CASH", "Cash and equivalents", "B4", ("CASH_AND_CASH_EQUIVALENTS",), "POINT_IN_TIME"),
    ("NET_DEBT", "Net debt", "B5", ("NET_DEBT",), "POINT_IN_TIME"),
    ("DEBT_TO_EQUITY", "Debt / equity", "B5", ("DEBT_TO_EQUITY",), "FY"),
    ("CURRENT_RATIO", "Current ratio", "B5", ("CURRENT_RATIO",), "FY"),
    ("INTEREST_COVERAGE", "Interest coverage", "B5", ("INTEREST_COVERAGE",), "FY"),
)


def periods(ctx: Ctx, basis: ClaimBasis, period_type: str) -> list[date]:
    return sorted({v.period.period_end for v in ctx.box4.canonical_values
                   if v.period.period_end is not None and v.period.period_type.value ==
                   period_type and basis_of(v.reporting_basis) == basis
                   and v.period.period_end <= ctx.as_of})


def labels(ctx: Ctx, basis: ClaimBasis, period_type: str) -> dict[date, str]:
    out: dict[date, str] = {}
    for v in ctx.box4.canonical_values:
        if v.period.period_type.value == period_type and basis_of(v.reporting_basis) == basis \
                and v.period.period_end is not None:
            out.setdefault(v.period.period_end, v.period.label)
    return out


def series_table(ctx: Ctx, section: SectionId, code: str, title: str, basis: ClaimBasis,
                 specs: tuple[Spec, ...], ends: list[date], period_type: str, *,
                 appendix: bool = False) -> UUID | None:
    if not ends:
        return None
    names = labels(ctx, basis, period_type)
    columns = [col(f"P_{e.isoformat().replace('-', '_')}", names.get(e, str(e)),
                   ColumnKind.HISTORICAL, period=names.get(e), period_end=e) for e in ends]
    rows: list[TableRow] = []
    for key, label, layer, codes, ptype in specs:
        cells: list[TableCell] = []
        unit: str | None = None
        for e, c in zip(ends, columns, strict=True):
            cid: UUID | None = None
            note, not_app = "NOT_REPORTED", False
            for code_ in codes:
                if (layer == "B4" and code_ == "OPERATING_PROFIT"
                        and ctx.box4.industry in RESTRICTED_INDUSTRIES):
                    note, not_app = "NOT_APPLICABLE", True
                    break
                if layer == "B4":
                    v = ctx.b4.get((code_, basis.value, ptype, e))
                    if v is not None:
                        cid = ctx.fact(v)
                        break
                else:
                    m = ctx.b5.get((code_, basis.value, ptype, e))
                    if m is not None:
                        cid = ctx.metric(m)
                        note = m.status.value
                        not_app = m.status in (CalculationStatus.NOT_APPLICABLE,
                                               CalculationStatus.UNSUPPORTED_SECTOR)
                        break
            if cid is not None and unit is None:
                unit = ctx.book.get(cid).unit
            cells.append(cell(c.key, ctx.book, cid, note=note, not_applicable=not_app))
        if all(x.status.value == "UNAVAILABLE" and x.note == "NOT_REPORTED" for x in cells):
            continue
        rows.append(row(key, label, RowKind.HISTORICAL if layer == "B4" else RowKind.CALCULATION,
                        cells, unit=unit))
    footnotes = (f"Reporting basis: {basis.value}. Values as reported upstream (Box 4) or "
                 "calculated upstream (Box 5); no Box 14 arithmetic.",)
    return ctx_table(ctx, section, code, title, basis, columns, rows, appendix=appendix,
                     footnotes=footnotes)


def historical(ctx: Ctx) -> None:
    sec = S.HISTORICAL_FINANCIALS
    years = periods(ctx, ctx.analytical, "FY")
    if not years:
        ctx.limit(sec, "ANNUAL_FINANCIALS", "DATA_UNAVAILABLE", "no annual statements")
    series_table(ctx, sec, "ANNUAL_FINANCIALS", "Historical annual financials (core, up to 5 "
                 "years)", ctx.analytical, ANNUAL, years[-5:], "FY")
    series_table(ctx, sec, "ANNUAL_FINANCIALS_FULL", "Annual financials (all years)",
                 ctx.analytical, ANNUAL, years, "FY", appendix=True)
    for code in ("REVENUE_CAGR_3Y", "REVENUE_CAGR_5Y", "PAT_CAGR_3Y", "PAT_CAGR_5Y",
                 "EPS_CAGR_5Y"):
        if years:
            m = ctx.b5.get((code, ctx.analytical.value, "FY", years[-1]))
            if m is not None:
                ctx.metric(m, sec)
    quarters = periods(ctx, ctx.analytical, "QUARTER")
    if not quarters:
        ctx.drafts[sec].missing.add(InputFamily.QUARTERLY_FINANCIALS)
        ctx.limit(sec, "QUARTERLY_FINANCIALS", "DATA_UNAVAILABLE", "no quarterly results")
    series_table(ctx, sec, "QUARTERLY_FINANCIALS", "Quarterly financials (core, up to 8 "
                 "quarters; not annualised)", ctx.analytical, QUARTERLY, quarters[-8:],
                 "QUARTER")
    series_table(ctx, sec, "QUARTERLY_FINANCIALS_FULL", "Quarterly financials (up to 13 "
                 "quarters)", ctx.analytical, QUARTERLY, quarters[-13:], "QUARTER",
                 appendix=True)
    if ctx.control is not None:
        control_years = periods(ctx, ctx.control, "FY")
        series_table(ctx, sec, "STANDALONE_FINANCIALS", "Standalone financials (forensic / "
                     "control basis)", ctx.control, ANNUAL, control_years, "FY", appendix=True)


def cash_balance(ctx: Ctx) -> None:
    sec = S.CASH_FLOW_BALANCE_SHEET
    years = periods(ctx, ctx.analytical, "FY")
    series_table(ctx, sec, "CASH_FLOW_BALANCE_SHEET", "Cash conversion, working capital and "
                 "balance sheet", ctx.analytical, CASH_BALANCE, years[-5:], "FY")
    for m in ctx.box13.earnings_quality.derived:
        if m.code.startswith("CUMULATIVE_"):
            ctx.forensic(m, sec)
    for series in ctx.box13.working_capital_risks.disclosures:
        ctx.forensic(series, sec)


# --- 7. accounting / forensics --------------------------------------------------------------------


def forensics(ctx: Ctx) -> None:
    sec = S.ACCOUNTING_FORENSICS
    d13 = ctx.box13
    seen: set[UUID] = set()
    for f in (*d13.earnings_quality.reported, *d13.earnings_quality.derived,
              *d13.earnings_quality.adjusted, *d13.financial_forensics.metrics):
        if f.metric_id in seen:
            continue
        seen.add(f.metric_id)
        ctx.forensic(f, sec)
    rows: list[TableRow] = []
    for model in d13.financial_forensics.models:
        name = f"{model.model}/{model.variant}"
        ups = tuple(up(Layer.BOX5, "FORENSIC_RESULT", r) for r in model.record_ids
                    if r in {x.result_id for x in ctx.box5.forensic_scores})
        if model.status == "CALCULATED" and model.score is not None:
            box5 = [x for x in ctx.box5.forensic_scores if x.result_id in model.record_ids]
            sources: list[UUID] = []
            for x in box5:
                for comp in x.components:
                    sources += ctx.res.records(comp.canonical_value_ids)[0]
            cid = ctx.book.claim(
                None, CT.CALCULATION, model.model,
                f"{name}: score {number(model.score)} ({model.zone or 'no zone'}); Box 5 / "
                f"Box 13 reported model.", basis=ctx.analytical,
                upstream=ups or (up(Layer.BOX13, "DATASET", d13.dataset_id),),
                sources=tuple(dict.fromkeys(sources)) or (ctx.book.dataset_sources[Layer.BOX5],),
                value=model.score if sources else None, metric=model.model,
                calculation_ids=model.record_ids or (d13.dataset_id,),
                formula_ids=(model.formula[:1000],))
            ctx.add(sec, cid)
            rows.append(row(name, name, RowKind.CALCULATION, [cell("SCORE", ctx.book, cid)]))
        elif model.status == "NOT_APPLICABLE":
            rows.append(row(name, name, RowKind.CALCULATION, [
                cell("SCORE", ctx.book, None, note="NOT_APPLICABLE", not_applicable=True)]))
        else:
            cid = ctx.limit(sec, model.model, "DATA_UNAVAILABLE",
                            f"{name} not reported upstream (missing: "
                            f"{', '.join(model.missing_components[:8]) or 'inputs'})",
                            upstream=ups or None)
            rows.append(row(name, name, RowKind.DATA_LIMITATION, [
                cell("SCORE", ctx.book, None, note="UNAVAILABLE")]))
    ctx_table(ctx, sec, "FORENSIC_MODELS", "Forensic models (shown only as upstream reports "
              "them)", ctx.analytical, [col("SCORE", "Score")], rows)
    contingent(ctx)
    for check in d13.earnings_quality.box5_checks:
        if check.observed is None:
            continue
        found = [q for q in ctx.box5.earnings_quality if q.check_id == check.check_id]
        if not found:
            continue
        chk = found[0]
        check_sources = ctx.res.records((*chk.canonical_value_ids, *chk.metric_ids))[0]
        if not check_sources:
            continue
        ctx.add(sec, ctx.book.claim(
            None, CT.CALCULATION, f"BOX5_CHECK_{check.code}"[:80],
            f"Box 5 earnings-quality check {check.code}: {check.status} "
            f"(observed {number(check.observed)}; {check.period or 'no period'}).",
            basis=basis_of(chk.reporting_basis), upstream=(up(Layer.BOX5, "EARNINGS_CHECK",
                                                              check.check_id),),
            sources=check_sources, value=check.observed, period=check.period,
            calculation_ids=(check.check_id,), formula_ids=(chk.rule_id,)))
    related = d13.related_party_risks
    for kind, total in related.totals:
        items = [r for r in related.record_ids if r in ctx.explicit]
        rp_sources = tuple(ctx.cited(ctx.explicit[r][1].source) for r in items)
        if not rp_sources:
            continue
        ctx.add(sec, ctx.book.claim(
            None, CT.CALCULATION, f"RELATED_PARTY_{kind.value}"[:80],
            f"Related-party {human(kind.value)} total {number(total)} crore (FY "
            f"{related.fiscal_year}, {related.basis.value.lower()}; Box 13 sum of cited items).",
            basis=basis_of(related.basis.value), upstream=tuple(up(
                Layer.BOX13, "REQUEST_INPUT", r) for r in items), sources=rp_sources, value=total,
            unit="INR CRORE", calculation_ids=tuple(items), formula_ids=("BOX13/RPT_TOTAL",)))
    if not related.totals:
        ctx.limit(sec, "RELATED_PARTIES", "DATA_UNAVAILABLE",
                  "related-party transactions not supplied to Box 13")


def contingent(ctx: Ctx) -> None:
    sec = S.ACCOUNTING_FORENSICS
    c = ctx.box13.contingent_liabilities
    items = [(r, ctx.explicit[r][1]) for r in c.record_ids if r in ctx.explicit]
    claims: list[UUID] = []
    rows: list[TableRow] = []
    for rid, item in sorted(items, key=lambda x: (x[1].category.value, str(x[0]))):
        cid = ctx.book.claim(
            None, CT.FACT, f"CONTINGENT_{item.category.value}"[:80],
            f"Contingent item ({human(item.category.value)}): {number(item.amount_crore)} "
            f"crore at {item.as_of} ({item.basis.lower()}).", basis=basis_of(item.basis),
            upstream=(up(Layer.BOX13, "REQUEST_INPUT", rid),), sources=(ctx.cited(
                item.source),), value=item.amount_crore, unit="INR CRORE",
            period=str(item.as_of), period_end=item.as_of,
            evidence_ids=item.source.evidence_ids, source_text=item.description)
        claims.append(cid)
        rows.append(row(str(rid), item.description or item.category.value, RowKind.FACT,
                        [text("CATEGORY", item.category.value, origin="REPORT"),
                         cell("AMOUNT", ctx.book, cid)], unit="INR CRORE"))
    if c.total_crore is not None and claims:
        total = ctx.book.claim(
            None, CT.CALCULATION, "CONTINGENT_LIABILITIES_TOTAL",
            f"Contingent liabilities total {number(c.total_crore)} crore at {c.as_of} "
            f"({c.basis.value.lower()}; Box 13 {c.total_method.lower()}; capital commitments "
            "excluded).", basis=basis_of(c.basis.value),
            upstream=tuple(up(Layer.BOX13, "REQUEST_INPUT", r) for r, _ in items),
            sources=tuple(dict.fromkeys(s for i in claims for s in ctx.book.get(i).source_ids)),
            value=c.total_crore, unit="INR CRORE", period=str(c.as_of), period_end=c.as_of,
            calculation_ids=c.record_ids, formula_ids=(f"BOX13/{c.total_method}",),
            derived_from=claims)
        ctx.add(sec, total)
        basis = basis_of(c.basis.value)
        ctx_table(ctx, sec, "CONTINGENT_LIABILITIES", "Contingent liabilities and commitments "
                  "(as cited)", basis, [col("CATEGORY", "Category", ColumnKind.TEXT),
                                        col("AMOUNT", "Amount (crore)")], rows,
                  footnotes=("Capital commitments are commitments, never added to the "
                             "contingent-liability total.",))
    elif c.total_method == "UNAVAILABLE":
        ctx.limit(sec, "CONTINGENT_LIABILITIES", "DATA_UNAVAILABLE",
                  "contingent liabilities not supplied to Box 13")


# --- 8 / 9. management, governance, concalls and guidance -----------------------------------------


def management(ctx: Ctx) -> None:
    sec = S.MANAGEMENT_GOVERNANCE
    d13 = ctx.box13
    r = d13.management_reliability
    if r.sample_status != "SUFFICIENT":
        ctx.limit(sec, "MANAGEMENT_RELIABILITY", "INSUFFICIENT_GUIDANCE_SAMPLE"
                  if r.sample_status == "INSUFFICIENT_SAMPLE" else "DATA_UNAVAILABLE",
                  f"{r.completed} completed of {r.issued} issued quantitative targets "
                  f"(minimum {r.minimum_sample}); open guidance is never counted as missed")
    elif r.accuracy_pct is not None and ctx.box8 is not None:
        sources = tuple(outcome_sources(ctx, r.outcome_ids))
        if sources:
            ctx.add(sec, ctx.book.claim(
                None, CT.CALCULATION, "GUIDANCE_ACCURACY",
                f"Guidance delivery: {r.met} met / {r.missed} missed / {r.partial} partial of "
                f"{r.completed} completed targets ({number(r.accuracy_pct)} %; {r.open} still "
                "open).", basis=NA, upstream=tuple(up(Layer.BOX8, "OUTCOME", o)
                                                   for o in r.outcome_ids[:400]),
                sources=sources, value=r.accuracy_pct, unit="PERCENT",
                calculation_ids=r.outcome_ids[:400], formula_ids=("BOX13/GUIDANCE_ACCURACY",)))
    rows: list[TableRow] = []
    entries = sorted(d13.governance_risks.catalogue,
                     key=lambda g: (g.entry_date or date.min, g.kind, str(g.entry_id)),
                     reverse=True)
    core = [g for g in entries if g.kind in CORE_GOVERNANCE_KINDS][:25]
    for g in entries:
        claim = governance_claim(ctx, g)
        if claim is None:
            continue
        rows.append(row(str(g.entry_id), g.kind, RowKind.FACT,
                        [text("DATE", str(g.entry_date) if g.entry_date else None,
                              origin="REPORT"), text("TITLE", g.title)], claim_id=claim))
        if g in core:
            ctx.add(sec, claim)
    ctx_table(ctx, sec, "GOVERNANCE_CATALOGUE", "Governance catalogue (full)", NA,
              [col("DATE", "Date", ColumnKind.TEXT), col("TITLE", "Entry", ColumnKind.TEXT)],
              rows[: ctx.limits.max_appendix_rows], appendix=True, upstream=len(rows))
    if ctx.box8 is None:
        ctx.drafts[sec].missing.add(InputFamily.BOX8_MANAGEMENT)
    else:
        for s in sorted((x for x in ctx.box8.statements if any(
                t.value in ("CAPITAL_ALLOCATION", "DIVIDEND_BUYBACK", "M_AND_A", "CAPEX")
                for t in x.statement_types)), key=lambda x: str(x.statement_id))[:8]:
            ctx.add(sec, statement_claim(ctx, s))


def governance_claim(ctx: Ctx, g: Any) -> UUID | None:
    if g.layer.value == "BOX12" and g.record_id in ctx.res.events:
        sources = ctx.res.event_sources(ctx.res.events[g.record_id])
    elif g.record_id in ctx.explicit:
        sources = (ctx.cited(ctx.explicit[g.record_id][1].source),)
    elif g.source_id:
        sources = (ctx.res.label(g.source_id, Layer.BOX13, "GOVERNANCE"),)
    else:
        return None
    return ctx.book.claim(
        None, CT.FACT, f"GOVERNANCE_{g.kind}"[:80],
        f"Governance record ({human(g.kind)}) dated {g.entry_date or 'not stated'}.", basis=NA,
        upstream=(up(Layer.BOX13, "GOVERNANCE_ENTRY", g.entry_id),), sources=sources,
        source_text=g.title, period=str(g.entry_date) if g.entry_date else None,
        period_end=g.entry_date)


def statement_claim(ctx: Ctx, s: Any) -> UUID:
    event = next((e for e in ctx.box8.events if e.event_id == s.event_id), None) \
        if ctx.box8 else None
    when = event.call_date if event else None
    src = ctx.res.box8(s.provenance, when)
    types = ", ".join(t.value.lower() for t in s.statement_types[:4]) or "statement"
    return ctx.book.claim(
        None, CT.MANAGEMENT_STATEMENT, f"MANAGEMENT_{s.statement_types[0].value}"[:80]
        if s.statement_types else "MANAGEMENT_STATEMENT",
        f"Management statement ({types}; {'forward-looking' if s.forward_looking else 'not '
        'forward-looking'}) in {event.title if event and event.title else 'a company event'}"
        f"{' on ' + str(when) if when else ''}.", basis=NA,
        upstream=(up(Layer.BOX8, "STATEMENT", s.statement_id),), sources=(src,),
        evidence_ids=s.provenance.evidence_ids, source_text=s.quote, confidence=None)


def outcome_sources(ctx: Ctx, outcome_ids: Iterable[UUID]) -> list[UUID]:
    out: list[UUID] = []
    if ctx.box8 is None:
        return out
    outcomes = {o.outcome_id: o for o in ctx.box8.outcomes}
    for i in outcome_ids:
        o = outcomes.get(i)
        if o is not None and o.actual is not None:
            out += ctx.res.record(o.actual.record_id)[0]
    return list(dict.fromkeys(out))


def concalls(ctx: Ctx) -> list[GuidanceTrackerEntry]:
    sec = S.CONCALL_GUIDANCE
    b8 = ctx.box8
    d = ctx.drafts[sec]
    if b8 is None:
        d.missing |= {InputFamily.BOX8_MANAGEMENT, InputFamily.CONCALLS, InputFamily.GUIDANCE}
        ctx.limit(sec, "CONCALLS", "LAYER_NOT_SUPPLIED", "Box 8 management intelligence")
        return []
    calls = [e for e in b8.events if e.event_type.value in ("EARNINGS_CALL", "ANALYST_MEET",
                                                            "INVESTOR_DAY")
             and (e.call_date is None or e.call_date <= ctx.as_of)]
    if not calls:
        d.missing.add(InputFamily.CONCALLS)
        ctx.limit(sec, "CONCALLS", "DATA_UNAVAILABLE", "no earnings-call transcripts in Box 8")
    rows: list[TableRow] = []
    order = {t: i for i, t in enumerate(STATEMENT_PRIORITY)}
    for e in sorted(calls, key=lambda x: (x.call_date or date.min, str(x.event_id))):
        src = ctx.book.source(Layer.BOX8, SourceType.FILING if e.authority.value ==
                              "PRIMARY_REGULATORY" else SourceType.COMPANY_DOCUMENT,
                              f"{e.document_type.value}/{e.authority.value}", url=e.source_url,
                              document_id=e.document_id, sha=e.artifact_sha256,
                              title=e.title or e.event_type.value, document_date=e.call_date)
        claim = ctx.book.claim(
            None, CT.FACT, "MANAGEMENT_EVENT",
            f"{human(e.event_type.value).capitalize()} "
            f"{'held on ' + str(e.call_date) if e.call_date else '(date not stated)'}.",
            basis=NA, upstream=(up(Layer.BOX8, "EVENT", e.event_id),), sources=(src,),
            source_text=e.title, period_end=e.call_date,
            period=e.reporting_period.raw_text if e.reporting_period else None)
        ctx.add(sec, claim)
        rows.append(row(str(e.event_id), e.title or e.event_type.value, RowKind.FACT, [
            text("DATE", str(e.call_date) if e.call_date else None, origin="REPORT"),
            text("PERIOD", e.reporting_period.raw_text if e.reporting_period else None)],
            claim_id=claim))
        statements = [s for s in b8.statements if s.event_id == e.event_id
                      and s.speaker_role.value == "MANAGEMENT"]
        statements.sort(key=lambda s: (
            min((order.get(t.value, 99) for t in s.statement_types), default=99),
            not s.forward_looking, str(s.statement_id)))
        for s in statements[: ctx.limits.max_core_statements_per_call]:
            ctx.add(sec, statement_claim(ctx, s))
    ctx_table(ctx, sec, "MANAGEMENT_EVENTS", "Management events (calls / meets)", NA,
              [col("DATE", "Date", ColumnKind.TEXT), col("PERIOD", "Period", ColumnKind.TEXT)],
              rows)
    all_rows = [row(str(s.statement_id), ", ".join(t.value for t in s.statement_types[:3])
                    or "STATEMENT", RowKind.MANAGEMENT_STATEMENT, [text("QUOTE", s.quote)],
                    claim_id=statement_claim(ctx, s))
                for s in sorted(b8.statements, key=lambda x: str(x.statement_id))[
                    : ctx.limits.max_appendix_rows]]
    ctx_table(ctx, sec, "MANAGEMENT_STATEMENTS", "Management statements (all, verbatim)", NA,
              [col("QUOTE", "Quote", ColumnKind.TEXT)], all_rows, appendix=True,
              upstream=len(b8.statements))
    for t in sorted(b8.themes, key=lambda x: (-x.mentions, x.topic, str(x.theme_id)))[:10]:
        events = [e for e in b8.events if e.event_id in t.event_ids]
        sources = tuple(dict.fromkeys(
            ctx.book.source(Layer.BOX8, SourceType.FILING, "THEME_EVENT", url=e.source_url,
                            document_id=e.document_id, sha=e.artifact_sha256) for e in events))
        if not sources:
            continue
        ctx.add(sec, ctx.book.claim(
            None, CT.CALCULATION, f"THEME_{t.topic}"[:80],
            f"Recurring {t.source.lower()} theme '{human(t.topic)}': {t.mentions} mentions "
            f"({t.first_date or '-'} to {t.last_date or '-'}; Box 8 theme count).",
            basis=NA, upstream=(up(Layer.BOX8, "THEME", t.theme_id),), sources=sources,
            value=Decimal(t.mentions), unit="COUNT", calculation_ids=(t.theme_id,),
            formula_ids=("BOX8/THEME_COUNT",)))
    tracker = guidance_tracker(ctx, b8)
    guidance = {g.guidance_id: g for g in b8.guidance}
    for tl in sorted(b8.timelines, key=lambda x: (x.subject_code, x.target_period_key)):
        if len(tl.guidance_ids) < 2:
            continue  # one statement: nothing repeated or changed across calls
        timeline_sources = [s for gid in tl.guidance_ids if gid in guidance
                            for s in ctx.book.get(guidance_claim(ctx, guidance[gid])).source_ids]
        if not timeline_sources:
            continue
        ctx.add(sec, ctx.book.claim(
            None, CT.MANAGEMENT_STATEMENT, f"GUIDANCE_TIMELINE_{tl.subject_code}"[:80],
            f"Guidance on {human(tl.subject_code)} for {tl.target_period_key} across "
            f"{len(tl.guidance_ids)} statements: "
            f"{' -> '.join(x.value.lower() for x in tl.lifecycles)} (Box 8 timeline).",
            basis=NA, upstream=(up(Layer.BOX8, "TIMELINE", tl.timeline_id),
                                *(up(Layer.BOX8, "GUIDANCE", g) for g in tl.guidance_ids)),
            sources=tuple(dict.fromkeys(timeline_sources))))
    if not b8.guidance:
        d.missing.add(InputFamily.GUIDANCE)
        ctx.limit(sec, "GUIDANCE", "DATA_UNAVAILABLE", "no quantified guidance in Box 8")
    return tracker


def guidance_claim(ctx: Ctx, g: Any) -> UUID:
    assert ctx.box8 is not None
    events = {e.event_id: e for e in ctx.box8.events}
    statements = {s.statement_id: s for s in ctx.box8.statements}
    event = events.get(g.event_id)
    sources = [ctx.res.box8(statements[s].provenance, event.call_date if event else None)
               for s in g.statement_ids if s in statements]
    if not sources and event is not None:
        sources.append(ctx.book.source(Layer.BOX8, SourceType.FILING, "GUIDANCE_EVENT",
                                       url=event.source_url, document_id=event.document_id))
    return ctx.book.claim(
        None, CT.MANAGEMENT_STATEMENT, f"GUIDANCE_{g.subject.subject_code}"[:80],
        f"Management guidance on {human(g.subject.subject_code)} ({g.target_type.value.lower()}"
        f", {g.lifecycle.value.lower()}) for "
        f"{g.target_period.raw_text if g.target_period else 'an unstated period'}.",
        basis=basis_of(g.reporting_basis), value=g.value, unit=g.unit.value,
        upstream=(up(Layer.BOX8, "GUIDANCE", g.guidance_id),
                  *(up(Layer.BOX8, "STATEMENT", s) for s in g.statement_ids
                    if s in statements)),
        sources=sources, source_text=g.raw_target_text, period=g.target_period.raw_text
        if g.target_period else None)


def guidance_tracker(ctx: Ctx, b8: ManagementIntelligenceDataset) -> list[GuidanceTrackerEntry]:
    sec = S.CONCALL_GUIDANCE
    outcomes = {o.guidance_id: o for o in b8.outcomes}
    statements = {s.statement_id: s for s in b8.statements}
    out: list[GuidanceTrackerEntry] = []
    for g in sorted(b8.guidance, key=lambda x: (x.issued_date or date.min, str(x.guidance_id))):
        target = g.raw_target_text
        claim = guidance_claim(ctx, g)
        ctx.add(sec, claim)
        o = outcomes.get(g.guidance_id)
        actual = None
        if o is not None and o.actual is not None:
            a = o.actual
            if a.record_id in ctx.res.values and ctx.res.values[a.record_id].value == a.value:
                actual = ctx.fact(ctx.res.values[a.record_id])
            elif a.record_id in ctx.res.metrics and ctx.res.metrics[a.record_id].value == a.value:
                actual = ctx.metric(ctx.res.metrics[a.record_id])
            else:
                s_, u_, e_ = ctx.res.record(a.record_id)
                if s_:
                    actual = ctx.book.claim(
                        None, CT.FACT, f"GUIDANCE_ACTUAL_{a.code}"[:80],
                        f"Reported outcome for {human(a.code)}: {number(a.value)} {a.unit}.",
                        basis=basis_of(a.reporting_basis), value=a.value, unit=a.unit,
                        upstream=(up(Layer.BOX8, "OUTCOME", o.outcome_id), *u_), sources=s_,
                        period_end=a.period_end)
            if actual is not None:
                ctx.add(sec, actual)
        out.append(GuidanceTrackerEntry(
            guidance_id=g.guidance_id, statement_claim_id=claim, date_made=g.issued_date,
            deadline=g.target_period.raw_text if g.target_period else None,
            deadline_end=g.target_period.period_end if g.target_period else None,
            metric=g.subject.subject_code, target=target, outcome_id=o.outcome_id if o else None,
            result=o.result.value if o else "NOT_ASSESSED", actual_claim_id=actual,
            lifecycle=g.lifecycle.value, evidence_ids=tuple(
                e for s in g.statement_ids if s in statements
                for e in statements[s].provenance.evidence_ids)[:50]))
    return out


# --- 10. corporate events -------------------------------------------------------------------------


#: core-event tiers: Box 12 events a RED / YELLOW Box 13 company risk cites as its evidence,
#: events Box 13 graded one by one, then the event-type rules (selection only, never severity)
TIER_RED_EVIDENCE, TIER_YELLOW_EVIDENCE, TIER_GRADED, TIER_TYPE = 0, 1, 2, 3


def risk_evidence(d13: DueDiligenceDataset, event_ids: set[UUID]) -> dict[UUID, list[RiskSignal]]:
    """Box 12 event ID -> the RED / YELLOW company risks citing it by typed record / evidence
    ID (an aggregate risk such as EXCHANGE_PENALTIES cites every event behind its count)."""
    out: dict[UUID, list[RiskSignal]] = {}
    for s in d13.signals:
        if s.signal_class != SignalClass.COMPANY_RISK or s.severity not in (Severity.RED,
                                                                            Severity.YELLOW):
            continue
        for x in dict.fromkeys((*s.record_ids, *s.evidence_ids)):
            if x in event_ids:
                out.setdefault(x, []).append(s)
    return out


def select_core_events(ledger: Iterable[Any], linked: dict[UUID, list[RiskSignal]],
                       graded: set[UUID], cap: int) -> tuple[list[Any], list[Any]]:
    """(core events, risk-evidence / graded events left out by the cap). Ranking is total
    and independent of input order: tier, then upstream materiality (risk evidence) or the
    event-type priority, then the newest date, then the event ID; one row per event ID."""
    priority = {t: i for i, t in enumerate(ALWAYS_MATERIAL_EVENTS + SIZE_GATED_EVENTS)}

    def tier(e: Any) -> int | None:
        severities = {s.severity for s in linked.get(e.event_id, ())}
        if Severity.RED in severities:
            return TIER_RED_EVIDENCE
        if Severity.YELLOW in severities:
            return TIER_YELLOW_EVIDENCE
        if e.event_id in graded:
            return TIER_GRADED
        if e.status.value != "ACTIVE":
            return None
        if e.event_type.value in ALWAYS_MATERIAL_EVENTS:
            return TIER_TYPE
        m = e.materiality
        if e.event_type.value in SIZE_GATED_EVENTS and m is not None and \
                m.status.value == "AVAILABLE" and m.ratio_pct is not None and \
                m.ratio_pct >= MATERIAL_EVENT_PCT:
            return TIER_TYPE
        return None

    def key(item: tuple[int, Any]) -> tuple[int, int, Decimal, int, str]:
        t, e = item
        if t == TIER_TYPE:
            return t, priority.get(e.event_type.value, 99), Decimal(0), \
                -e.event_date.toordinal(), str(e.event_id)
        m = e.materiality
        ratio = m.ratio_pct if m is not None and m.status.value == "AVAILABLE" and \
            m.ratio_pct is not None else None
        return t, 0 if ratio is not None else 1, -(ratio or Decimal(0)), \
            -e.event_date.toordinal(), str(e.event_id)

    unique: dict[UUID, Any] = {}
    for e in ledger:
        unique.setdefault(e.event_id, e)
    tiers = ((tier(e), e) for e in unique.values())
    ranked = sorted(((t, e) for t, e in tiers if t is not None), key=key)
    core = [e for _, e in ranked[:cap]]
    dropped = [e for t, e in ranked[cap:] if t < TIER_TYPE]
    return core, dropped


def events(ctx: Ctx) -> None:
    sec = S.CORPORATE_EVENTS
    b12 = ctx.box12
    if b12 is None:
        ctx.drafts[sec].missing.add(InputFamily.BOX12_MARKET)
        ctx.limit(sec, "CORPORATE_EVENTS", "LAYER_NOT_SUPPLIED",
                  "Box 12 market and exchange intelligence")
        return
    for p in b12.payloads:
        if p.status.value != "OK":
            ctx.res.payload(p.payload_id)  # a blocked / failed source stays in the register
    graded = {e.event_id for e in ctx.box13.exchange_event_risks.entries
              if e.severity in (Severity.YELLOW, Severity.RED)}
    ledger = sorted((e for e in b12.events if e.event_date <= ctx.as_of),
                    key=lambda e: (e.event_date, e.event_type.value, str(e.event_id)),
                    reverse=True)
    linked = risk_evidence(ctx.box13, {e.event_id for e in ledger})
    cap = ctx.limits.max_core_events
    core, dropped = select_core_events(ledger, linked, graded, cap)
    core_ids = {e.event_id for e in core}

    def event_row(e: Any) -> TableRow:
        claim, ratio = event_claims(ctx, e, tuple(s.risk_id for s in linked.get(e.event_id, ())))
        return row(str(e.event_id), e.event_type.value, RowKind.FACT, [
            text("DATE", str(e.event_date), origin="REPORT"), text("TITLE", e.title),
            cell("VALUE", ctx.book, claim, note="NOT_STATED"),
            cell("MATERIALITY", ctx.book, ratio, note="NOT_AVAILABLE")], claim_id=claim)

    columns = [col("DATE", "Date", ColumnKind.TEXT), col("TITLE", "Filing title",
                                                          ColumnKind.TEXT),
               col("VALUE", "Stated value (crore)"),
               col("MATERIALITY", "Stated value / reference revenue (%)")]
    core_rows = [event_row(e) for e in sorted(core, key=lambda e: (e.event_date, str(
        e.event_id)), reverse=True)]
    ctx_table(ctx, sec, "MATERIAL_EVENTS", "Material corporate / exchange events (selected "
              "deterministically)", NA, columns, core_rows,
              footnotes=("Selection: events a RED Box 13 company risk cites as evidence, then "
                         "those a YELLOW one cites, then events Box 13 graded YELLOW / RED, then "
                         "always-material event types, then size-gated types >= 1 % of reference "
                         f"revenue; at most {cap} events. Citation by a risk sets selection "
                         "priority only; it is never a severity of the event itself.",))
    if dropped:
        ctx.limit(sec, "CORE_EVENT_SELECTION", "CORE_EVENT_LIMIT_REACHED",
                  f"{len(dropped)} events cited by a RED / YELLOW risk or graded by Box 13 "
                  f"exceed the {cap}-event core table",
                  upstream=tuple(up(Layer.BOX12, "EVENT", e.event_id) for e in dropped[:500]))
    cap_rows = ctx.limits.max_appendix_rows
    appendix_rows = [event_row(e) for e in ledger[:cap_rows]]
    ctx_table(ctx, sec, "EVENT_LEDGER", "Full exchange event ledger (newest first)", NA,
              columns, appendix_rows, appendix=True, upstream=len(ledger))
    ctx.drafts[sec].notes.append(f"CORE_EVENTS_{len(core_ids)}_OF_{len(ledger)}"[:80])
    cited = sum(1 for e in core if e.event_id in linked)
    ctx.drafts[sec].notes.append(
        f"CORE_RISK_EVIDENCE_EVENTS_{cited}_OF_{len(linked)}"[:80])


def event_claims(ctx: Ctx, e: Any, risk_ids: tuple[UUID, ...] = ()) -> tuple[UUID,
                                                                              UUID | None]:
    sources = ctx.res.event_sources(e)
    if not sources:
        sources = (ctx.book.dataset_sources[Layer.BOX12],)
    value = e.financial_value_crore if e.value_currency == "INR" else None
    claim = ctx.book.claim(
        None, CT.FACT, f"EVENT_{e.event_type.value}"[:80],
        f"{human(e.event_type.value).capitalize()} filed on {e.event_date}"
        + (f"; stated value {number(value)} crore ({e.value_tax_basis.value.lower() if
           e.value_tax_basis else 'tax basis unknown'})" if value is not None else "") + ".",
        basis=NA, upstream=(up(Layer.BOX12, "EVENT", e.event_id),), sources=sources,
        value=value if any(ctx.book.get_source_type(s) != SourceType.UPSTREAM_DATASET
                           for s in sources) else None, unit="INR CRORE" if value is not None
        else None, period=str(e.event_date), period_end=e.event_date, source_text=e.title,
        risk_ids=risk_ids)  # the risks citing it: traceability, never the event's severity
    ratio = None
    m = e.materiality
    if m is not None and m.status.value == "AVAILABLE" and m.ratio_pct is not None and \
            ctx.book.get(claim).value is not None:
        ratio = ctx.book.claim(
            None, CT.CALCULATION, f"EVENT_MATERIALITY_{e.event_type.value}"[:80],
            f"Stated value equals {number(m.ratio_pct)} % of reference annual revenue (FY "
            f"{m.denominator_fiscal_year}; Box 12 {m.method.lower()}).", basis=NA,
            upstream=(up(Layer.BOX12, "EVENT", e.event_id),), sources=sources,
            value=m.ratio_pct, unit="PERCENT", period=str(e.event_date),
            period_end=e.event_date, calculation_ids=(e.event_id,),
            formula_ids=(f"BOX12/{m.method}",), derived_from=(claim,))
    return claim, ratio


# --- 11. order book -------------------------------------------------------------------------------

BUCKET_METRICS = {
    OrderBucketKind.CONFIRMED_ORDER_BOOK: ("DISCLOSED_CONFIRMED_INFLOW",
                                           "JV_ATTRIBUTABLE_CONFIRMED_VALUE"),
    OrderBucketKind.PENDING_L1: ("DISCLOSED_PENDING_L1_VALUE", "PENDING_L1_TRUE_PENDING_VALUE"),
    OrderBucketKind.UNRESOLVED_L1: ("PENDING_L1_UNRESOLVED_VALUE",),
    OrderBucketKind.SOURCE_INCOMPLETE: (),
}
BUCKET_LABELS = {
    OrderBucketKind.CONFIRMED_ORDER_BOOK: "CONFIRMED ORDER BOOK (LOA / contract stage)",
    OrderBucketKind.PENDING_L1: "PENDING L1 (lowest bidder; not an award, not backlog)",
    OrderBucketKind.UNRESOLVED_L1: "UNRESOLVED L1 (award linkage not established)",
    OrderBucketKind.SOURCE_INCOMPLETE: "SOURCE INCOMPLETE (L1 without enough source text)",
}


def order_book(ctx: Ctx) -> OrderBookView:
    sec = S.ORDER_BOOK
    q = ctx.box13.order_book_quality
    b12 = ctx.box12
    if not q.applicable:
        reason = "bank / NBFC / insurer" if ctx.financial else \
            "the company is not declared order-book driven to Box 13"
        ctx.drafts[sec].status = SectionStatus.NOT_APPLICABLE
        ctx.drafts[sec].reason = f"NOT_APPLICABLE: {reason}"
        return OrderBookView(applicable=False, applicability=reason, buckets=(),
                             reported_order_book_claim_ids=(), metric_claim_ids=())
    if b12 is None:
        ctx.drafts[sec].missing |= {InputFamily.BOX12_MARKET, InputFamily.ORDER_LEDGER}
    dataset = up(Layer.BOX13, "DATASET", ctx.box13.dataset_id)
    chains = list(b12.order_chains) if b12 else []
    by_bucket: dict[OrderBucketKind, list[Any]] = {k: [] for k in OrderBucketKind}
    for c in chains:
        res = c.l1_linkage.resolution.value if c.l1_linkage else None
        if c.counted_in_confirmed:
            by_bucket[OrderBucketKind.CONFIRMED_ORDER_BOOK].append(c)
        elif c.status.value == "PENDING_L1" and res == "TRUE_PENDING":
            by_bucket[OrderBucketKind.PENDING_L1].append(c)
        elif c.status.value == "PENDING_L1" and res == "UNRESOLVED_LINKAGE":
            by_bucket[OrderBucketKind.UNRESOLVED_L1].append(c)
        elif c.status.value == "PENDING_L1":
            by_bucket[OrderBucketKind.SOURCE_INCOMPLETE].append(c)
    pending = q.pending_l1
    counts = {
        OrderBucketKind.CONFIRMED_ORDER_BOOK: ("ORDER_CONFIRMED_CHAIN_COUNT",
                                               q.confirmed_chains),
        OrderBucketKind.PENDING_L1: ("ORDER_PENDING_L1_TRUE_PENDING_COUNT",
                                     pending.true_pending if pending else None),
        OrderBucketKind.UNRESOLVED_L1: ("ORDER_UNRESOLVED_L1_COUNT",
                                        pending.unresolved_linkage if pending else None),
        OrderBucketKind.SOURCE_INCOMPLETE: ("ORDER_L1_SOURCE_INCOMPLETE_COUNT",
                                            pending.source_incomplete if pending else None),
    }
    metrics = {m.kind.value: m for m in (b12.order_book.metrics if b12 and b12.order_book
                                         else ())}
    buckets: list[OrderBucket] = []
    for kind in OrderBucketKind:
        topic, count = counts[kind]
        members = by_bucket[kind]
        sources = tuple(dict.fromkeys(s for c in members[:50] for s in ctx.res.chain(c)))
        count_claim = None
        if count is not None and sources:
            count_claim = ctx.book.claim(
                None, CT.CALCULATION, topic,
                f"{BUCKET_LABELS[kind]}: {count} order chains (Box 13 / Box 12 ledger).",
                basis=NA, upstream=(dataset, *(up(Layer.BOX12, "ORDER_CHAIN", c.chain_id)
                                               for c in members[:400])),
                sources=sources[:500], value=Decimal(count), unit="COUNT",
                calculation_ids=(ctx.box13.dataset_id,),
                formula_ids=(f"BOX13/{topic}",))
            ctx.add(sec, count_claim)
        values: list[UUID] = []
        for name in BUCKET_METRICS[kind]:
            m = metrics.get(name)
            if m is None:
                continue
            cid = order_metric(ctx, m, members)
            if cid is not None and ctx.book.get(cid).claim_type != CT.DATA_LIMITATION:
                values.append(cid)
            ctx.add(sec, cid)
        buckets.append(OrderBucket(
            bucket=kind, label=BUCKET_LABELS[kind],
            chain_ids=tuple(c.chain_id for c in members), count_claim_id=count_claim,
            value_claim_ids=tuple(values),
            note="Never added to any other bucket; L1 is not an award and not revenue."))
    if pending is not None and pending.chains:
        l1 = [c for k in (OrderBucketKind.PENDING_L1, OrderBucketKind.UNRESOLVED_L1,
                          OrderBucketKind.SOURCE_INCOMPLETE) for c in by_bucket[k]]
        l1_sources = tuple(dict.fromkeys(s for c in l1[:50] for s in ctx.res.chain(c)))
        if l1_sources:
            ctx.add(sec, ctx.book.claim(
                None, CT.CALCULATION, "ORDER_L1_CHAIN_COUNT",
                f"L1 chains in the ledger: {pending.chains} ({pending.true_pending} true pending, "
                f"{pending.unresolved_linkage} unresolved, {pending.source_incomplete} source "
                f"incomplete); {pending.counted_in_confirmed} counted in confirmed backlog "
                "(Box 13).", basis=NA, upstream=(dataset, *(up(Layer.BOX12, "ORDER_CHAIN",
                                                              c.chain_id) for c in l1[:400])),
                sources=l1_sources[:500], value=Decimal(pending.chains), unit="COUNT",
                calculation_ids=(ctx.box13.dataset_id,), formula_ids=("BOX13/PENDING_L1",)))
    reported: list[UUID] = []
    if q.reported_order_book_crore is not None:
        reported_claim = reported_book(ctx, q)
        if reported_claim is not None:
            reported.append(reported_claim)
            ctx.add(sec, reported_claim)
    else:
        ctx.limit(sec, "REPORTED_ORDER_BOOK", "DATA_UNAVAILABLE",
                  "no company-reported order book supplied")
    metric_ids: list[UUID] = []
    used = {n for names in BUCKET_METRICS.values() for n in names}
    for name, m in sorted(metrics.items()):
        if name in used:
            continue
        cid = order_metric(ctx, m, by_bucket[OrderBucketKind.CONFIRMED_ORDER_BOOK])
        if cid is not None:
            ctx.add(sec, cid)
            if ctx.book.get(cid).claim_type != CT.DATA_LIMITATION:
                metric_ids.append(cid)
    ledger_rows: list[TableRow] = []
    for c in sorted(chains, key=lambda x: (x.first_announced, str(x.chain_id)))[
            : ctx.limits.max_appendix_rows]:
        ledger_rows.append(chain_row(ctx, c))
    ctx_table(ctx, sec, "ORDER_LEDGER", "Order ledger (every chain, by stage)", NA, [
        col("STATUS", "Status", ColumnKind.TEXT), col("CLIENT", "Client", ColumnKind.TEXT),
        col("FIRST", "First announced", ColumnKind.TEXT),
        col("CONFIRMED", "Confirmed gross value (crore)"),
        col("PENDING_L1", "Pending L1 value (crore)")], ledger_rows, appendix=True,
        upstream=len(chains))
    bucket_rows = [row(b.bucket.value, b.label, RowKind.CALCULATION, [
        cell("CHAINS", ctx.book, b.count_claim_id, note="NOT_AVAILABLE")]) for b in buckets]
    ctx_table(ctx, sec, "ORDER_BUCKETS", "Order book by stage (never summed across stages)", NA,
              [col("CHAINS", "Order chains")], bucket_rows,
              footnotes=("L1 != LOA != contract != revenue. Pending and unresolved L1 bids are "
                         "a pipeline indicator only and never enter confirmed backlog, "
                         "order-book/revenue or book-to-bill.",))
    return OrderBookView(
        applicable=True, applicability=", ".join(q.applicability_basis) or "DECLARED",
        buckets=tuple(buckets), reported_order_book_claim_ids=tuple(reported),
        metric_claim_ids=tuple(metric_ids))


def order_metric(ctx: Ctx, m: Any, members: list[Any]) -> UUID:
    ups = (up(Layer.BOX12, "DATASET", ctx.box12.dataset_id),) if ctx.box12 else ()
    if m.status.value != "AVAILABLE" or m.value is None:
        return ctx.limit(None, f"ORDER_{m.kind.value}"[:80], "DATA_UNAVAILABLE",
                         f"{human(m.kind.value)} ({m.reason or 'unavailable'})", upstream=ups)
    sources = tuple(dict.fromkeys(s for c in members[:50] for s in ctx.res.chain(c)))
    if not sources:
        return ctx.limit(None, f"ORDER_{m.kind.value}"[:80], "DATA_UNAVAILABLE",
                         f"{human(m.kind.value)}: no source documents", upstream=ups)
    return ctx.book.claim(
        None, CT.CALCULATION, f"ORDER_{m.kind.value}"[:80],
        f"{human(m.kind.value).capitalize()}: {number(m.value)} {m.unit.lower()} "
        f"({m.period_start or '-'} to {m.period_end or '-'}; {m.completeness.lower()}; "
        f"{m.chains} chains).", basis=NA, upstream=ups, sources=sources[:500], value=m.value,
        unit=m.unit, period=f"{m.period_start} to {m.period_end}", period_end=m.period_end,
        calculation_ids=(ctx.box12.dataset_id,) if ctx.box12 else (),
        formula_ids=(f"BOX12/{m.method}"[:1000],))


def reported_book(ctx: Ctx, q: Any) -> UUID | None:
    if q.reported_order_book_layer is not None and q.reported_order_book_layer.value == \
            "REQUEST":
        items = [(fid("order_book", r), r) for r in ctx.box13.request.order_book_reports
                 if r.value_crore == q.reported_order_book_crore]
        if not items:
            return None
        rid, item = items[0]
        return ctx.book.claim(
            None, CT.FACT, "ORDER_BOOK_REPORTED",
            f"Company-reported order book {number(item.value_crore)} crore at {item.as_of} "
            f"({item.basis.lower()}; tax basis {item.tax_basis.lower()}; definition as "
            "stated).", basis=basis_of(item.basis),
            upstream=(up(Layer.BOX13, "REQUEST_INPUT", rid),), sources=(ctx.cited(
                item.source),), value=item.value_crore, unit="INR CRORE",
            period=str(item.as_of), period_end=item.as_of, source_text=item.definition,
            evidence_ids=item.source.evidence_ids)
    if ctx.box7 is not None:
        for f in ctx.box7.order_book_facts:
            if f.normalized_value == q.reported_order_book_crore:
                return ctx.book.claim(
                    None, CT.FACT, "ORDER_BOOK_REPORTED",
                    f"Company-reported order book {number(f.normalized_value)} crore "
                    f"({f.as_of or 'date not stated'}).", basis=NA,
                    upstream=(up(Layer.BOX7, "ORDER_BOOK_FACT", f.fact_id),),
                    sources=(ctx.res.location(f.source),), value=f.normalized_value,
                    unit="INR CRORE", period_end=f.as_of, source_text=f.raw_label)
    return None


def chain_row(ctx: Ctx, c: Any) -> TableRow:
    sources = ctx.res.chain(c) or (ctx.book.dataset_sources[Layer.BOX12],)
    real = any(ctx.book.get_source_type(s) != SourceType.UPSTREAM_DATASET for s in sources)
    claim = ctx.book.claim(
        None, CT.FACT, "ORDER_CHAIN",
        f"Order chain ({c.status.value.lower()}; stages "
        f"{'/'.join(s.value for s in c.stages)}) first announced {c.first_announced}.",
        basis=NA, upstream=(up(Layer.BOX12, "ORDER_CHAIN", c.chain_id),), sources=sources,
        source_text=c.project, period_end=c.last_updated if c.last_updated <= ctx.as_of
        else None)
    confirmed = pending = None
    if c.confirmed_gross_value_crore is not None and c.counted_in_confirmed and real:
        confirmed = ctx.book.claim(
            None, CT.FACT, "ORDER_CHAIN_CONFIRMED_VALUE",
            f"Confirmed order value {number(c.confirmed_gross_value_crore)} crore "
            f"({c.value_tax_basis.value.lower()}).", basis=NA,
            upstream=(up(Layer.BOX12, "ORDER_CHAIN", c.chain_id),), sources=sources,
            value=c.confirmed_gross_value_crore, unit="INR CRORE",
            period_end=c.confirmed_on, derived_from=(claim,))
    if c.pending_l1_value_crore is not None and real:
        pending = ctx.book.claim(
            None, CT.FACT, "ORDER_CHAIN_PENDING_L1_VALUE",
            f"L1 bid value {number(c.pending_l1_value_crore)} crore (pending; not an award).",
            basis=NA, upstream=(up(Layer.BOX12, "ORDER_CHAIN", c.chain_id),), sources=sources,
            value=c.pending_l1_value_crore, unit="INR CRORE", derived_from=(claim,))
    return row(str(c.chain_id), c.status.value, RowKind.FACT, [
        text("STATUS", c.status.value, origin="REPORT"), text("CLIENT", c.client),
        text("FIRST", str(c.first_announced), origin="REPORT"),
        cell("CONFIRMED", ctx.book, confirmed, note="NOT_CONFIRMED_OR_NOT_STATED"),
        cell("PENDING_L1", ctx.book, pending, note="NO_PENDING_L1_VALUE")], claim_id=claim)


# --- 12. shareholding -----------------------------------------------------------------------------


def shareholding(ctx: Ctx) -> None:
    sec = S.SHAREHOLDING
    own = ctx.box13.ownership_risks
    if not own.series:
        ctx.drafts[sec].missing.add(InputFamily.SHAREHOLDING_DATA)
        ctx.limit(sec, "SHAREHOLDING", "DATA_UNAVAILABLE", "no shareholding series upstream")
        return
    ends = sorted({p.period_end for s in own.series for p in s.points
                   if p.period_end <= ctx.as_of})
    core_ends = ends[-12:]
    for code, chosen, appendix in (("SHAREHOLDING", core_ends, False),
                                   ("SHAREHOLDING_HISTORY", ends, True)):
        rows: list[TableRow] = []
        for s in own.series:
            label = s.holder or str(s.category)
            points = {p.period_end: p for p in s.points}
            cells = [cell(f"P_{e.isoformat().replace('-', '_')}", ctx.book,
                          ownership_point(ctx, str(s.category), points[e]) if e in points
                          else None, note="NOT_FILED") for e in chosen]
            if not appendix:
                for key, change in (("QOQ", s.change_qoq_pp), ("Q4", s.change_4q_pp),
                                    ("Q12", s.change_12q_pp)):
                    cid = None
                    if change is not None and s.points:
                        last = s.points[-1]
                        src = ownership_point(ctx, str(s.category), last)
                        cid = ctx.book.claim(
                            None, CT.CALCULATION, f"HOLDING_CHANGE_{key}_{s.category}"[:80],
                            f"{human(str(s.category)).capitalize()} holding change "
                            f"({key.lower()}): {number(change)} percentage points to "
                            f"{last.period_end} (Box 13).", basis=NA,
                            upstream=tuple(up(Layer.BOX12 if p.layer.value == "BOX12"
                                              else Layer.BOX13, "SHAREHOLDING_FILING"
                                              if p.layer.value == "BOX12" else "REQUEST_INPUT",
                                              p.record_id) for p in s.points[-13:]),
                            sources=ctx.book.get(src).source_ids, value=change,
                            unit="PERCENTAGE_POINTS", period_end=last.period_end,
                            calculation_ids=tuple(p.record_id for p in s.points[-13:]),
                            formula_ids=("BOX13/OWNERSHIP_CHANGE_PP",), derived_from=(src,))
                    cells.append(cell(key, ctx.book, cid, note="INSUFFICIENT_HISTORY"))
            rows.append(row(label, label, RowKind.HISTORICAL, cells, unit="PERCENT"))
        columns = [col(f"P_{e.isoformat().replace('-', '_')}", str(e), ColumnKind.HISTORICAL,
                       period_end=e) for e in chosen]
        if not appendix:
            columns += [col("QOQ", "Change QoQ (pp)"), col("Q4", "Change 4Q (pp)"),
                        col("Q12", "Change 12Q (pp)")]
        ctx_table(ctx, sec, code, "Shareholding pattern" + (" (full history)" if appendix
                                                             else " (12 quarters)"), NA,
                  columns, rows, appendix=appendix)
    if own.unparsed_categories:
        ctx.limit(sec, "SHAREHOLDING_CATEGORIES", "DATA_UNAVAILABLE",
                  f"categories not parsed from exchange filings: "
                  f"{', '.join(own.unparsed_categories[:10])}")
    if ctx.box12 is not None:
        for a in sorted(ctx.box12.corporate_actions, key=lambda x: str(x.action_id)):
            day = a.ex_date or a.record_date or a.announcement_date
            if a.kind.value not in ("BONUS", "SPLIT", "RIGHTS", "BUYBACK") or (
                    day is not None and day > ctx.as_of):
                continue
            sources = ctx.res.refs(a.sources)
            if sources:
                ctx.add(sec, ctx.book.claim(
                    None, CT.FACT, f"CORPORATE_ACTION_{a.kind.value}"[:80],
                    f"Corporate action ({human(a.kind.value)}) dated {day or 'not stated'} "
                    "(changes share counts; not promoter selling).", basis=NA,
                    upstream=(up(Layer.BOX12, "CORPORATE_ACTION", a.action_id),),
                    sources=sources, source_text=a.subject, period_end=day))


# --- 13. credit -----------------------------------------------------------------------------------


def credit(ctx: Ctx) -> None:
    sec = S.CREDIT_LIQUIDITY
    c = ctx.box13.liquidity_credit_risks
    for m in (*c.metrics, *c.agency_adjusted_debt, *c.maturities):
        ctx.forensic(m, sec)
    rows: list[TableRow] = []
    for r in sorted(c.ratings, key=lambda x: (x.agency, x.instrument or "", x.action_date)):
        if r.action_date > ctx.as_of:
            continue
        if r.record_id in ctx.explicit:
            sources: tuple[UUID, ...] = (ctx.cited(ctx.explicit[r.record_id][1].source),)
        else:
            sources = tuple(ctx.box12_label(r.source_id)) or (
                ctx.res.label(r.source_id, Layer.BOX13, "RATING"),)
        claim = ctx.book.claim(
            None, CT.FACT, "CREDIT_RATING",
            f"Rating action by {r.agency} on {r.action_date}: {r.action.value.lower()}, "
            f"outlook {r.outlook.value.lower()} (instrument "
            f"{r.instrument or 'not stated'}).", basis=NA,
            upstream=(up(Layer.BOX13, "RATING", r.observation_id),), sources=sources,
            source_text=r.rating_text, period=str(r.action_date), period_end=r.action_date)
        ctx.add(sec, claim)
        instrument = r.instrument or "instrument not stated"
        rows.append(row(str(r.observation_id), f"{r.agency} - {instrument}", RowKind.FACT, [
            text("DATE", str(r.action_date), origin="REPORT"), text("RATING", r.rating_text),
            text("OUTLOOK", r.outlook.value, origin="REPORT"),
            text("ACTION", r.action.value, origin="REPORT")], claim_id=claim))
    ctx_table(ctx, sec, "RATINGS", "Credit ratings by agency and instrument (never compared "
              "across agencies or instruments)", NA,
              [col("DATE", "Date", ColumnKind.TEXT), col("RATING", "Rating", ColumnKind.TEXT),
               col("OUTLOOK", "Outlook", ColumnKind.TEXT),
               col("ACTION", "Action", ColumnKind.TEXT)], rows)
    if not rows:
        ctx.drafts[sec].missing.add(InputFamily.RATINGS)
        ctx.limit(sec, "CREDIT_RATINGS", "DATA_UNAVAILABLE", "no rating by instrument supplied")
    for given in ctx.box13.request.ratings:
        if given.sensitivities and given.action_date <= ctx.as_of:
            ctx.add(sec, ctx.book.claim(
                None, CT.FACT, "RATING_SENSITIVITIES",
                f"Rating sensitivities stated by {given.agency} ({given.action_date}).",
                basis=NA, upstream=(up(Layer.BOX13, "REQUEST_INPUT", fid("rating", given)),),
                sources=(ctx.cited(given.source),), source_text=given.sensitivities,
                period_end=given.action_date))


# --- 14 / 15 / 16. forecasts, valuation, scenarios ------------------------------------------------

FORECAST_LINES = ("REVENUE", "EBITDA", "OPERATING_PROFIT", "EBIT", "PBT", "PAT",
                  "PAT_ATTRIBUTABLE", "EPS", "CAPEX", "FCF_ESTIMATE", "GROSS_DEBT")


def forecast_claim(ctx: Ctx, v: ForecastValue) -> UUID | None:
    if v.status != "AVAILABLE" or v.value is None or ctx.box10 is None:
        return None
    if v.kind == "ACTUAL":
        if v.actual is not None and v.actual.canonical_value_id in ctx.res.values and \
                ctx.res.values[v.actual.canonical_value_id].value == v.value:
            return ctx.fact(ctx.res.values[v.actual.canonical_value_id])
        ids = [c.canonical_value_id for c in v.actual_components] + (
            [v.actual.canonical_value_id] if v.actual else [])
        s_, u_, e_ = ctx.res.records(ids)
        if not s_:
            return None
        return ctx.book.claim(
            None, CT.CALCULATION if v.formula_id else CT.FACT, f"ACTUAL_{v.line.value}",
            f"Historical anchor {human(v.line.value)}: {number(v.value)} {v.currency.value} "
            f"{v.scale.value} ({v.period.label}; Box 10 actual).",
            basis=basis_of(ctx.box10.base.reporting_basis), value=v.value,
            upstream=(up(Layer.BOX10, "FORECAST_VALUE", v.value_id), *u_), sources=s_,
            unit=f"{v.currency.value} {v.scale.value}", period=v.period.label,
            period_end=v.period.period_end, calculation_ids=(v.value_id,),
            formula_ids=(v.formula_id,) if v.formula_id else ())
    assert v.scenario is not None
    sources = (ctx.book.dataset_sources[Layer.BOX10], *ctx.res.anchored(v.value_id))
    return ctx.book.claim(
        None, CT.SCENARIO, f"FORECAST_{v.line.value}",
        f"{v.scenario.value} scenario forecast {human(v.line.value)}: {number(v.value)} "
        f"{v.currency.value} {v.scale.value} for {v.period.label} (Box 10 {v.formula_id}).",
        basis=basis_of(ctx.box10.base.reporting_basis), value=v.value,
        upstream=(up(Layer.BOX10, "FORECAST_VALUE", v.value_id),), sources=sources,
        unit=f"{v.currency.value} {v.scale.value}" if v.unit == "MONEY" else
        f"{v.currency.value} per share", period=v.period.label, period_end=v.period.period_end,
        scenario=Scenario(v.scenario.value), calculation_ids=(v.value_id,),
        formula_ids=(v.formula_id,) if v.formula_id else ("BOX10",))


def assumption_claim(ctx: Ctx, a: Any) -> UUID:
    assert ctx.box10 is not None
    sources: list[UUID] = [ctx.book.dataset_sources[Layer.BOX10]]
    sources += ctx.res.records(a.canonical_value_ids)[0]
    if a.reference:
        sources.append(ctx.book.source(Layer.BOX10, SourceType.ANALYST_INPUT,
                                       f"ASSUMPTION/{a.source.value}", reference=a.reference))
    if a.guidance is not None and ctx.box8 is not None:
        statements = {s.statement_id: s for s in ctx.box8.statements}
        sources += [ctx.res.box8(statements[s].provenance) for s in a.guidance.statement_ids
                    if s in statements]
    return ctx.book.claim(
        None, CT.SCENARIO, f"ASSUMPTION_{a.driver.value}",
        f"{a.scenario.value} assumption {human(a.driver.value)} = {number(a.value)} "
        f"{a.unit.value.lower()} for FY{a.fiscal_year} ({a.source.value.lower()}, "
        f"{a.method.value.lower()}; Box 10).", basis=basis_of(ctx.box10.base.reporting_basis),
        value=a.value, unit=a.unit.value, period=f"FY{a.fiscal_year}",
        scenario=Scenario(a.scenario.value),
        upstream=(up(Layer.BOX10, "ASSUMPTION", a.assumption_id),), sources=sources,
        calculation_ids=(a.assumption_id,), formula_ids=(a.method.value,),
        limitations=a.warnings[:10])


def forecasts(ctx: Ctx) -> dict[Scenario, tuple[list[UUID], list[UUID]]]:
    sec = S.FORECASTS
    f = ctx.box10
    out: dict[Scenario, tuple[list[UUID], list[UUID]]] = {}
    if f is None:
        ctx.drafts[sec].missing.add(InputFamily.BOX10_FORECAST)
        ctx.limit(sec, "FORECAST", "LAYER_NOT_SUPPLIED", "Box 10 forecast")
        return out
    if f.status.value == "UNAVAILABLE":
        ctx.drafts[sec].status = SectionStatus.UNAVAILABLE
        ctx.drafts[sec].reason = "Box 10 produced no forecast"
    elif f.status.value == "PARTIAL":
        ctx.drafts[sec].status = SectionStatus.PARTIAL
        ctx.drafts[sec].reason = "Box 10 forecast is PARTIAL"
    for i in f.issues:
        if i.severity.value == "ERROR":
            ctx.limit(sec, f"FORECAST_{i.code}"[:80], "DATA_UNAVAILABLE",
                      f"Box 10 {i.code}" + (f" ({i.subject})" if i.subject else ""),
                      upstream=(up(Layer.BOX10, "DATASET", f.forecast_id),))
    years = list(f.forecast_years)
    anchors = sorted({v.period.period_end for v in f.actuals if v.period.period_end},
                     reverse=True)[:2][::-1]
    for scenario in f.scenarios:
        sc = Scenario(scenario.value)
        forecast_ids: list[UUID] = []
        rows: list[TableRow] = []
        for line in FORECAST_LINES:
            cells: list[TableCell] = []
            for end in anchors:
                actual = next((v for v in f.actuals if v.line.value == line
                               and v.period.period_end == end), None)
                cells.append(cell(f"A_{end.year}", ctx.book, forecast_claim(ctx, actual)
                                  if actual else None, note="NOT_AVAILABLE"))
            for y in years:
                v = next((x for x in f.values if x.line.value == line and x.scenario == scenario
                          and x.period.period_end == y.period_end), None)
                cid = forecast_claim(ctx, v) if v else None
                if cid is not None:
                    forecast_ids.append(cid)
                cells.append(cell(f"F_{y.fiscal_year or y.label}"[:80].upper().replace(
                    " ", "_"), ctx.book, cid, note=v.reason or "UNAVAILABLE" if v else
                    "NOT_FORECAST"))
            if any(x.status.value == "AVAILABLE" for x in cells):
                rows.append(row(line, human(line).capitalize(), RowKind.FORECAST, cells))
        columns = [col(f"A_{e.year}", f"FY ending {e} (historical)", ColumnKind.HISTORICAL,
                       period_end=e) for e in anchors]
        columns += [col(f"F_{y.fiscal_year or y.label}"[:80].upper().replace(" ", "_"),
                        f"{y.label} ({sc.value} forecast)", ColumnKind.FORECAST,
                        period=y.label, period_end=y.period_end, scenario=sc) for y in years]
        ctx_table(ctx, sec, f"FORECAST_{sc.value}", f"{sc.value} scenario forecast (Box 10; "
                  "historical anchors labelled)", basis_of(f.base.reporting_basis), columns,
                  rows, scenario=sc, footnotes=(
                      "Forecast columns are scenario outputs, not reported results.",))
        drivers: list[UUID] = []
        arows: list[TableRow] = []
        for driver in sorted({a.driver.value for a in f.assumptions if a.scenario == scenario}):
            acells: list[TableCell] = []
            for y in years:
                a = next((x for x in f.assumptions if x.driver.value == driver
                          and x.scenario == scenario and x.fiscal_year == y.fiscal_year), None)
                cid = assumption_claim(ctx, a) if a else None
                if cid is not None:
                    drivers.append(cid)
                acells.append(cell(f"F_{y.fiscal_year or y.label}"[:80].upper().replace(
                    " ", "_"), ctx.book, cid, note="NOT_ASSUMED"))
            arows.append(row(driver, human(driver).capitalize(), RowKind.ASSUMPTION, acells))
        ctx_table(ctx, sec, f"ASSUMPTIONS_{sc.value}", f"{sc.value} forecast assumptions (Box 10 "
                  "assumption IDs)", basis_of(f.base.reporting_basis), columns[len(anchors):],
                  arows, scenario=sc)
        out[sc] = (drivers, forecast_ids)
    return out


VALUATION_ROWS = ("ENTERPRISE_VALUE", "GROSS_DEBT", "CASH", "MINORITY_INTEREST",
                  "EQUITY_VALUE", "SHARE_COUNT", "FAIR_VALUE_PER_SHARE", "WACC",
                  "TERMINAL_VALUE", "PRESENT_VALUE_TERMINAL", "SUM_PRESENT_VALUE")


def valuation(ctx: Ctx) -> ValuationView:
    sec = S.VALUATION
    v = ctx.box11
    market = market_context(ctx)
    if v is None:
        ctx.drafts[sec].missing.add(InputFamily.BOX11_VALUATION)
        ctx.limit(sec, "VALUATION", "LAYER_NOT_SUPPLIED", "Box 11 valuation")
        return ValuationView(methods=(), market_context_claim_ids=tuple(market))
    if v.status.value == "UNAVAILABLE":
        ctx.drafts[sec].status = SectionStatus.UNAVAILABLE
        ctx.drafts[sec].reason = "Box 11 produced no method result (inputs missing)"
    elif v.status.value == "PARTIAL":
        ctx.drafts[sec].status = SectionStatus.PARTIAL
        ctx.drafts[sec].reason = "some Box 11 methods are unavailable"
    values = {x.value_id: x for x in v.values}
    shared = [x for x in v.values if x.scenario is None and x.status == "AVAILABLE"]
    methods: list[ValuationMethodView] = []
    rows: list[TableRow] = []
    for r in v.results:
        sc = Scenario(r.scenario.value)
        result_ids: list[UUID] = []
        named: dict[str, UUID] = {}
        for vid in r.value_ids:
            x = values.get(vid)
            if x is None or x.status != "AVAILABLE" or x.value is None:
                continue
            cid = valuation_claim(ctx, x, sc, r.method.value)
            result_ids.append(cid)
            named.setdefault(x.name, cid)
        for x in shared:
            cid = valuation_claim(ctx, x, sc, r.method.value)
            named.setdefault(x.name, cid)
        assumptions = [valuation_assumption(ctx, a, sc) for a in v.assumptions
                       if a.scenario is None or a.scenario == r.scenario]
        limits: list[UUID] = []
        if r.status.value != "COMPLETE":
            limits.append(ctx.limit(None, f"VALUATION_{r.method.value}"[:80],
                                    "DATA_UNAVAILABLE",
                                    f"{r.scenario.value} {r.method.value} is "
                                    f"{r.status.value.lower()} ({r.reason}); missing: "
                                    f"{', '.join(r.missing_inputs[:10]) or 'not stated'}",
                                    upstream=(up(Layer.BOX11, "DATASET", v.valuation_id),)))
        ctx.add(sec, *(named[n] for n in VALUATION_ROWS if n in named), *limits)
        methods.append(ValuationMethodView(
            scenario=sc, method=r.method.value, status=r.status.value, reason=r.reason,
            missing_inputs=r.missing_inputs, assumption_claim_ids=tuple(assumptions),
            result_claim_ids=tuple(dict.fromkeys(result_ids + [
                named[n] for n in VALUATION_ROWS if n in named])),
            limitation_claim_ids=tuple(limits)))
        rows.append(row(f"{r.method.value}|{sc.value}", f"{r.method.value} - {sc.value}",
                        RowKind.VALUATION, [
                            text("STATUS", r.status.value, origin="REPORT"),
                            *(cell(n, ctx.book, named.get(n), note=r.reason or "UNAVAILABLE")
                              for n in ("ENTERPRISE_VALUE", "EQUITY_VALUE",
                                        "FAIR_VALUE_PER_SHARE"))]))
    ctx_table(ctx, sec, "VALUATION_METHODS", "Valuation by method and scenario (never blended)",
              basis_of(v.reporting_basis), [
                  col("STATUS", "Status", ColumnKind.TEXT),
                  col("ENTERPRISE_VALUE", "Enterprise value", ColumnKind.FORECAST),
                  col("EQUITY_VALUE", "Equity value", ColumnKind.FORECAST),
                  col("FAIR_VALUE_PER_SHARE", "Per-share value", ColumnKind.FORECAST)], rows,
              footnotes=("Each method and scenario is shown separately: no blended value, "
                         "midpoint or target is derived.",))
    arows = [row(f"{a.assumption_id}|{sc.value}", f"{a.kind.value} ({sc.value})",
                 RowKind.ASSUMPTION, [cell("VALUE", ctx.book, valuation_assumption(ctx, a, sc))])
             for a in v.assumptions for sc in (
                 [Scenario(a.scenario.value)] if a.scenario else
                 [Scenario(x.value) for x in v.scenarios])]
    ctx_table(ctx, sec, "VALUATION_ASSUMPTIONS", "Valuation assumptions (explicit, cited)",
              basis_of(v.reporting_basis), [col("VALUE", "Value", ColumnKind.FORECAST)], arows,
              appendix=True)
    return ValuationView(methods=tuple(methods), market_context_claim_ids=tuple(market))


def valuation_claim(ctx: Ctx, x: Any, scenario: Scenario, method: str) -> UUID:
    assert ctx.box11 is not None and x.value is not None
    sources = (ctx.book.dataset_sources[Layer.BOX11], *ctx.res.anchored(x.value_id))
    return ctx.book.claim(
        None, CT.SCENARIO, f"VALUATION_{x.name}"[:80],
        f"{scenario.value} {method} {human(x.name)}: {number(x.value)} "
        f"{(x.currency.value + ' ') if x.currency else ''}{x.unit.value.lower()} (Box 11 "
        f"{x.formula_id or 'input'}).", basis=basis_of(ctx.box11.reporting_basis),
        value=x.value, unit=x.unit.value, scenario=scenario,
        upstream=(up(Layer.BOX11, "VALUATION_VALUE", x.value_id),), sources=sources,
        calculation_ids=(x.value_id,), formula_ids=(x.formula_id or "BOX11/INPUT",),
        metric=method)


def valuation_assumption(ctx: Ctx, a: Any, scenario: Scenario) -> UUID:
    assert ctx.box11 is not None
    sources = [ctx.book.dataset_sources[Layer.BOX11]]
    if a.reference:
        sources.append(ctx.book.source(Layer.BOX11, SourceType.ANALYST_INPUT,
                                       "EXPLICIT_ASSUMPTION", reference=a.reference))
    return ctx.book.claim(
        None, CT.SCENARIO, f"VALUATION_ASSUMPTION_{a.kind.value}"[:80],
        f"{scenario.value} valuation assumption {human(a.kind.value)} = {number(a.value)} "
        f"{a.unit.value.lower()} (explicit input to Box 11).",
        basis=basis_of(ctx.box11.reporting_basis), value=a.value, unit=a.unit.value,
        scenario=scenario, upstream=(up(Layer.BOX11, "ASSUMPTION", a.assumption_id),),
        sources=sources)


def market_context(ctx: Ctx) -> list[UUID]:
    sec = S.VALUATION
    b12 = ctx.box12
    out: list[UUID] = []
    if b12 is None:
        ctx.drafts[sec].missing.add(InputFamily.MARKET_DATA)
        return out
    dataset = up(Layer.BOX12, "DATASET", b12.dataset_id)
    history = b12.company_history
    history_sources = tuple(s for s in (ctx.res.payload(p) for p in (history.payload_ids
                                                                       if history else ()))
                            if s)
    snap = b12.snapshot
    if snap is not None and snap.market_date is not None and snap.market_date <= ctx.as_of:
        src = ctx.res.payload(snap.payload_id)
        if src is not None:
            out.append(ctx.book.claim(
                None, CT.FACT, "MARKET_PRICE",
                f"Reference price {number(snap.last_price)} INR on {snap.market_date} "
                f"({snap.freshness.value.lower()}; {snap.source.value}). Context only.",
                basis=NA, upstream=(up(Layer.BOX12, "SNAPSHOT", snap.snapshot_id),),
                sources=(src,), value=snap.last_price, unit="INR", period_end=snap.market_date))
    for st in b12.statistics:
        if st.status.value == "AVAILABLE" and st.value is not None and history_sources:
            out.append(ctx.book.claim(
                None, CT.CALCULATION, f"MARKET_{st.kind.value}"[:80],
                f"{human(st.kind.value).capitalize()}: {number(st.value)} {st.unit.lower()} "
                f"({st.window_start} to {st.window_end}; Box 12). Context only.", basis=NA,
                upstream=(dataset,), sources=history_sources, value=st.value, unit=st.unit,
                period_end=st.window_end, calculation_ids=(b12.dataset_id,),
                formula_ids=(st.method[:1000],)))
    for r in b12.period_returns:
        if r.status.value == "AVAILABLE" and r.absolute_return_pct is not None and \
                history_sources:
            out.append(ctx.book.claim(
                None, CT.CALCULATION, f"MARKET_RETURN_{r.period.value}"[:80],
                f"Price return {r.period.value.lower()}: {number(r.absolute_return_pct)} % "
                f"({r.start_date} to {r.end_date}; price only). Context only.", basis=NA,
                upstream=(dataset,), sources=history_sources, value=r.absolute_return_pct,
                unit="PERCENT", period_end=r.end_date, calculation_ids=(b12.dataset_id,),
                formula_ids=(r.method[:1000],)))
    for b in b12.betas:
        if b.status.value == "AVAILABLE" and b.value is not None and history_sources:
            out.append(ctx.book.claim(
                None, CT.CALCULATION, "MARKET_BETA",
                f"Beta vs {b.benchmark.symbol}: {number(b.value)} ({b.observations} "
                f"observations; {b.methodology.frequency.lower()} returns). Context only.",
                basis=NA,
                upstream=(dataset,), sources=history_sources, value=b.value,
                period_end=b.end_date, calculation_ids=(b12.dataset_id,),
                formula_ids=(f"BOX12/BETA_{b.methodology.frequency}_"
                             f"{b.methodology.lookback_days}D",)))
    if not out:
        ctx.drafts[sec].missing.add(InputFamily.MARKET_DATA)
        ctx.limit(sec, "MARKET_DATA", "PROVIDER_UNAVAILABLE",
                  "no current quote or price history (market-data provider unavailable)",
                  upstream=(dataset,))
    ctx.add(sec, *out)
    return out


def scenarios(ctx: Ctx, forecast: dict[Scenario, tuple[list[UUID], list[UUID]]],
              view: ValuationView) -> InvestmentCase:
    sec = S.SCENARIO_ANALYSIS
    if not forecast:
        ctx.drafts[sec].missing.add(InputFamily.BOX10_FORECAST)
        ctx.limit(sec, "SCENARIOS", "DATA_UNAVAILABLE", "no Box 10 scenarios")
        return InvestmentCase(cases=())
    risks = tuple(ctx.box13.red_flags)
    names: dict[Scenario, Literal["BULL_CASE", "BASE_CASE", "BEAR_CASE"]] = {
        Scenario.BULL: "BULL_CASE", Scenario.BASE: "BASE_CASE", Scenario.BEAR: "BEAR_CASE"}
    cases: list[ScenarioCase] = []
    for sc in (Scenario.BULL, Scenario.BASE, Scenario.BEAR):
        if sc not in forecast:
            continue
        drivers, values = forecast[sc]
        valuations = [i for m in view.methods if m.scenario == sc and m.status != "UNAVAILABLE"
                      for i in m.result_claim_ids if ctx.book.get(i).topic in (
                          "VALUATION_ENTERPRISE_VALUE", "VALUATION_EQUITY_VALUE",
                          "VALUATION_FAIR_VALUE_PER_SHARE")]
        headline = [i for i in values if ctx.book.get(i).topic in ("FORECAST_REVENUE",
                                                                    "FORECAST_PAT")][:6]
        key_drivers = [i for i in drivers if ctx.book.get(i).topic in (
            "ASSUMPTION_REVENUE_GROWTH", "ASSUMPTION_OPERATING_MARGIN",
            "ASSUMPTION_EBITDA_MARGIN")][:9]
        ctx.add(sec, *headline, *key_drivers, *valuations[:6])
        status = SectionStatus.COMPLETE if headline and valuations else SectionStatus.PARTIAL
        cases.append(ScenarioCase(
            scenario=sc, case=names[sc], status=status,
            thesis=f"{sc.value} scenario as Box 10 defines it: the {sc.value.lower()} historical "
                   "driver path (assumption claims) carried through the forecast and Box 11 "
                   "valuation. A scenario, not a recommendation.",
            driver_claim_ids=tuple(key_drivers or drivers[:9]),
            forecast_claim_ids=tuple(headline or values[:6]),
            valuation_claim_ids=tuple(valuations[:12]), risk_ids=risks,
            conditions_required=(
                f"Realised drivers track the {sc.value} assumption path (see driver claims).",
                "The Box 13 company risks listed do not escalate beyond their current grade."),
            invalidation_conditions=(
                f"Realised drivers move outside the {sc.value} assumption path.",
                "A new RED company risk or a source correction changes the historical "
                "anchors.")))
    if any(c.status != SectionStatus.COMPLETE for c in cases):
        ctx.drafts[sec].status = SectionStatus.PARTIAL
        ctx.drafts[sec].reason = "a scenario has no Box 11 valuation output"
    return InvestmentCase(cases=tuple(cases))


# --- 17. risks, data quality, reconciliation, disagreements ---------------------------------------


def risks(ctx: Ctx) -> RiskPresentation:
    sec = S.RISKS
    d13 = ctx.box13
    red = [i for i in d13.red_flags if d13.signal(i).severity == Severity.RED]
    yellow = [i for i in d13.red_flags if d13.signal(i).severity == Severity.YELLOW]
    for i in (*red, *yellow, *d13.positive_controls):
        ctx.add(sec, ctx.signal_claims[i])
    gated = SignalStatus.UNSUPPORTED_SECTOR_RULE.value  # not applicable: never a data gap
    rows = [row(str(r.risk_id), r.code, RowKind.CALCULATION if r.status in ("EVALUATED", gated)
                else RowKind.DATA_LIMITATION, [
                    text("KIND", r.kind.value, origin="REPORT"),
                    text("STATUS", r.status, origin="REPORT"),
                    text("SEVERITY", r.severity, origin="REPORT"),
                    text("BASIS", ctx.book.get(r.claim_id).basis.value, origin="REPORT"),
                    cell("VALUE", ctx.book, r.claim_id, note=r.reason or "NO_VALUE",
                         not_applicable=r.status == gated)],
                claim_id=r.claim_id) for r in ctx.risk_entries]
    ctx_table(ctx, sec, "RISK_REGISTER", "Risk register (every Box 13 signal)", NA, [
        col("KIND", "Kind", ColumnKind.TEXT), col("STATUS", "Status", ColumnKind.TEXT),
        col("SEVERITY", "Severity", ColumnKind.TEXT), col("BASIS", "Basis", ColumnKind.TEXT),
        col("VALUE", "Value")], rows, appendix=True)
    limitations: list[LimitationEntry] = []
    for item in d13.data_quality_summary.items:
        cid = ctx.book.claim(
            None, CT.DATA_LIMITATION, f"DQ_{item.kind}"[:80],
            limitation_text("SOURCE_CONFLICT_UNRESOLVED" if item.kind == "INPUT_CONFLICT"
                            else "PROVIDER_UNAVAILABLE" if item.kind == "BLOCKED_SOURCE"
                            else "DATA_UNAVAILABLE", f"{human(item.kind)} ({item.layer.value})"),
            basis=NA, upstream=(up(Layer.BOX13, "DATA_QUALITY_ITEM", item.item_id),),
            limitations=(item.kind,), source_text=item.detail)
        limitations.append(LimitationEntry(entry_id=item.item_id, kind=item.kind,
                                           layer=item.layer.value, claim_id=cid,
                                           affects=item.affects))
        target = sec if item.kind != "RULE_INPUTS_UNAVAILABLE" else None
        if target is not None:
            ctx.add(target, cid)
        ctx.add(S.DATA_QUALITY_RECONCILIATION, cid)
    for r in ctx.risk_entries:
        claim = ctx.book.get(r.claim_id)
        if claim.claim_type == CT.DATA_LIMITATION:
            ctx.add(S.DATA_QUALITY_RECONCILIATION, r.claim_id)
            limitations.append(LimitationEntry(
                entry_id=uuid5(NS, f"signal-limitation|{r.risk_id}"), kind=r.reason or r.status,
                layer="BOX13", claim_id=r.claim_id, affects=(r.code,)))
    diligence: list[DiligenceEntry] = []
    drows: list[TableRow] = []
    for oi in d13.open_diligence_items:
        cid = ctx.book.claim(
            None, CT.DATA_LIMITATION, "OPEN_DILIGENCE",
            f"Open diligence item ({oi.importance.value.lower()} importance): not answered "
            "upstream.", basis=NA, upstream=(up(Layer.BOX13, "DILIGENCE_ITEM", oi.item_id),),
            limitations=("OPEN_DILIGENCE",), risk_ids=oi.related_risk_ids,
            source_text=f"{oi.question} | {oi.reason}")
        diligence.append(DiligenceEntry(item_id=oi.item_id, importance=oi.importance.value,
                                        claim_id=cid, needed_source=oi.needed_source,
                                        related_risk_ids=oi.related_risk_ids))
        if oi.importance.value in ("HIGH", "MEDIUM"):
            ctx.add(sec, cid)
        drows.append(row(str(oi.item_id), oi.importance.value, RowKind.DATA_LIMITATION, [
            text("QUESTION", oi.question), text("SOURCE", oi.needed_source)],
            claim_id=cid))
    ctx_table(ctx, sec, "OPEN_DILIGENCE", "Open diligence items (all)", NA, [
        col("QUESTION", "Question", ColumnKind.TEXT),
        col("SOURCE", "Source that would answer it", ColumnKind.TEXT)], drows, appendix=True)
    return RiskPresentation(risk_register=tuple(ctx.risk_entries), red=tuple(red),
                            yellow=tuple(yellow), positive_controls=d13.positive_controls,
                            data_limitations=tuple(limitations),
                            open_diligence=tuple(diligence))


def reconciliation(ctx: Ctx) -> list[ReconciliationEntry]:
    sec = S.DATA_QUALITY_RECONCILIATION
    out: list[ReconciliationEntry] = []
    rows: list[TableRow] = []
    comparisons = ctx.box13.standalone_consolidated_reconciliation
    latest = max((c.period_end for c in comparisons), default=None)
    for c in comparisons:
        ups = (up(Layer.BOX13, "COMPARISON", c.comparison_id),)
        sources, u_, e_ = ctx.res.records(c.record_ids)
        comparable = c.status.value not in ("UNAVAILABLE", "NOT_COMPARABLE")

        stand = recon_value(ctx, c, c.standalone_value, ClaimBasis.STANDALONE, sources, u_)
        cons = recon_value(ctx, c, c.consolidated_value, ClaimBasis.CONSOLIDATED, sources, u_)
        diff = None
        if comparable and c.absolute_difference is not None and sources:
            diff = ctx.book.claim(
                None, CT.CALCULATION, f"RECON_DIFFERENCE_{c.metric}"[:80],
                f"{c.metric} consolidated minus standalone {number(c.absolute_difference)} for "
                f"{c.period}: {c.status.value.lower()} (Box 13).", basis=NA,
                upstream=(*ups, *u_), sources=sources, value=c.absolute_difference,
                period=c.period, period_end=c.period_end, calculation_ids=(c.comparison_id,),
                formula_ids=("BOX13/BASIS_COMPARISON",),
                derived_from=tuple(x for x in (stand, cons) if x))
        if comparable:
            claim = diff or ctx.book.claim(
                None, CT.CALCULATION, f"RECON_STATUS_{c.metric}"[:80],
                f"{c.metric} {c.period}: {c.status.value.lower()} (Box 13).", basis=NA,
                upstream=ups, calculation_ids=(c.comparison_id,),
                formula_ids=("BOX13/BASIS_COMPARISON",), source_text=c.reason)
            classification = "COMPARISON"
        else:
            code = "SOURCE_CONFLICT_UNRESOLVED" if "INPUT_CONFLICT" in c.reason else \
                "NOT_COMPARABLE_ACROSS_BASES" if c.status.value == "NOT_COMPARABLE" else \
                "DATA_UNAVAILABLE"
            claim = ctx.book.claim(
                None, CT.DATA_LIMITATION, f"RECON_{c.metric}"[:80],
                limitation_text(code, f"{c.metric} {c.period}"), basis=NA, upstream=ups,
                limitations=(code,), source_text=c.reason)
            classification = "DATA_LIMITATION"
        out.append(ReconciliationEntry(
            comparison_id=c.comparison_id, metric=c.metric, period=c.period,
            status=c.status.value, standalone_claim_id=stand, consolidated_claim_id=cons,
            difference_claim_id=diff, claim_id=claim,
            classification=cast(Literal["COMPARISON", "DATA_LIMITATION"], classification)))
        if c.period_end == latest:
            ctx.add(sec, claim)
        rows.append(row(str(c.comparison_id), f"{c.metric} {c.period}", RowKind.CALCULATION
                        if comparable else RowKind.DATA_LIMITATION, [
                            text("STATUS", c.status.value, origin="REPORT"),
                            cell("STANDALONE", ctx.book, stand, note="UNAVAILABLE"),
                            cell("CONSOLIDATED", ctx.book, cons, note="UNAVAILABLE"),
                            cell("DIFFERENCE", ctx.book, diff, note="NOT_COMPARABLE")],
                        claim_id=claim))
    ctx_table(ctx, sec, "STANDALONE_CONSOLIDATED", "Standalone / consolidated reconciliation "
              "(Box 13)", NA, [col("STATUS", "Status", ColumnKind.TEXT),
                               col("STANDALONE", "Standalone", basis=ClaimBasis.STANDALONE),
                               col("CONSOLIDATED", "Consolidated",
                                   basis=ClaimBasis.CONSOLIDATED),
                               col("DIFFERENCE", "Consolidated - standalone")], rows,
              footnotes=("An unavailable or conflicted comparison is a data limitation, never "
                         "a company risk.",))
    return out


def recon_value(ctx: Ctx, c: Any, v: Decimal | None, basis: ClaimBasis,
                sources: tuple[UUID, ...], ups: tuple[UpstreamRecord, ...]) -> UUID | None:
    if v is None or not sources:
        return None
    return ctx.book.claim(
        None, CT.CALCULATION, f"RECON_{basis.value}_{c.metric}"[:80],
        f"{c.metric} ({basis.value.lower()}) {number(v)} for {c.period} (Box 13 basis "
        "comparison).", basis=basis, upstream=(up(Layer.BOX13, "COMPARISON", c.comparison_id),
                                               *ups), sources=sources, value=v,
        period=c.period, period_end=c.period_end, calculation_ids=(c.comparison_id,),
        formula_ids=("BOX13/BASIS_COMPARISON",))


def disagreements(ctx: Ctx) -> list[DisagreementView]:
    sec = S.DATA_QUALITY_RECONCILIATION
    out: list[DisagreementView] = []
    for e in ctx.box13.source_disagreements.entries:
        code = "SOURCE_CONFLICT_UNRESOLVED" if e.status == "OPEN" else "RESOLVED_BY_AUTHORITY"
        cid = ctx.book.claim(
            None, CT.DATA_LIMITATION, f"DISAGREEMENT_{e.group.value}"[:80],
            ("Source disagreement remains open: " if e.status == "OPEN" else
             "Source disagreement resolved by the upstream authority policy: ") +
            f"{e.subject[:200]} ({e.period}, {e.basis}).", basis=NA,
            upstream=(up(Layer.BOX13, "DISAGREEMENT", e.entry_id),), limitations=(code,),
            source_text=e.reason)
        out.append(DisagreementView(
            entry_id=e.entry_id, origin=e.origin.value, topic=e.subject, period=e.period,
            basis=e.basis, values=tuple(DisagreementValueView(
                source=v.source, authority=v.authority, value=v.value, record_id=v.record_id)
                for v in e.values), status=e.status, canonical_selection=e.canonical_source,
            reason=e.reason, claim_id=cid))
        if e.status == "OPEN":
            ctx.add(sec, cid)
    if ctx.box9 is not None:
        for c in ctx.box9.conflicts:
            cid = ctx.book.claim(
                None, CT.DATA_LIMITATION, "DISAGREEMENT_INDUSTRY",
                f"Source disagreement remains open on {human(c.topic)} (Box 9).", basis=NA,
                upstream=(up(Layer.BOX9, "CONFLICT", c.conflict_id),),
                limitations=("SOURCE_CONFLICT_UNRESOLVED",),
                source_text=f"{c.claim_a} | {c.claim_b}")
            out.append(DisagreementView(
                entry_id=c.conflict_id, origin="BOX9", topic=c.topic, period=str(
                    c.date_a or c.date_b or "-"), basis="NOT_APPLICABLE", values=(
                    DisagreementValueView(source=c.company_a, authority=c.nature_a.value,
                                          value=None, record_id=c.record_a),
                    DisagreementValueView(source=c.company_b, authority=c.nature_b.value,
                                          value=None, record_id=c.record_b)),
                status="OPEN", canonical_selection=None, reason="Box 9 leaves it unresolved",
                claim_id=cid))
            ctx.add(sec, cid)
    rows = [row(str(d.entry_id), d.topic, RowKind.DATA_LIMITATION, [
        text("STATUS", d.status, origin="REPORT"), text("ORIGIN", d.origin, origin="REPORT"),
        text("VALUES", " | ".join(f"{v.source}: {number(v.value)}" for v in d.values)),
        text("SELECTED", d.canonical_selection)], claim_id=d.claim_id) for d in out]
    ctx_table(ctx, sec, "SOURCE_DISAGREEMENTS", "Source disagreements (every value kept)", NA, [
        col("STATUS", "Status", ColumnKind.TEXT), col("ORIGIN", "Origin", ColumnKind.TEXT),
        col("VALUES", "Values by source", ColumnKind.TEXT),
        col("SELECTED", "Canonical selection", ColumnKind.TEXT)],
        rows[: ctx.limits.max_appendix_rows], appendix=True, upstream=len(rows))
    return out


# --- 18 / 19. what changes the view, monitoring ---------------------------------------------------


def thresholds(s: RiskSignal) -> tuple[ThresholdRef, ...]:
    """The rule's own thresholds, copied from the Box 13 registry output."""
    return tuple(ThresholdRef(severity=t.severity.value, comparison=t.comparison, value=t.value,
                              rule_id=s.rule_id) for t in s.threshold)


def what_changes(ctx: Ctx, tracker: list[GuidanceTrackerEntry]) -> list[ViewChangeEntry]:
    d13 = ctx.box13
    out: list[ViewChangeEntry] = []
    for i in d13.red_flags:
        s = d13.signal(i)
        out.append(ViewChangeEntry(
            entry_id=uuid5(NS, f"view|weak|{i}"), direction=ViewDirection.STRENGTHENS,
            condition=f"{s.metric} moves into the GREEN band of rule {s.rule_id}",
            subject=s.metric, thresholds=thresholds(s),
            related_claim_ids=(ctx.signal_claims[i],), related_risk_ids=(i,),
            source_required="Next annual report / results with the rule's inputs",
            status="MONITORING"))
    for i in d13.positive_controls:
        s = d13.signal(i)
        out.append(ViewChangeEntry(
            entry_id=uuid5(NS, f"view|control|{i}"), direction=ViewDirection.WEAKENS,
            condition=f"{s.metric} leaves the band that makes {s.code} a positive control",
            subject=s.metric, thresholds=thresholds(s),
            related_claim_ids=(ctx.signal_claims[i],), related_risk_ids=(i,),
            source_required="Next annual report / results with the rule's inputs",
            status="MONITORING"))
    for s in d13.signals:
        if s.signal_class == SignalClass.COMPANY_RISK and s.severity == Severity.GREEN:
            if not thresholds(s):
                continue
            out.append(ViewChangeEntry(
                entry_id=uuid5(NS, f"view|green|{s.risk_id}"), direction=ViewDirection.WEAKENS,
                condition=f"{s.metric} crosses the YELLOW threshold of rule {s.rule_id}",
                subject=s.metric, thresholds=thresholds(s),
                related_claim_ids=(ctx.signal_claims[s.risk_id],),
                related_risk_ids=(s.risk_id,),
                source_required="Next annual report / results with the rule's inputs",
                status="MONITORING"))
    for g in tracker:
        if g.result == "NOT_YET_DUE":
            out.append(ViewChangeEntry(
                entry_id=uuid5(NS, f"view|guidance|{g.guidance_id}"),
                direction=ViewDirection.RESOLVES_UNCERTAINTY,
                condition=f"Reported outcome for management guidance on {human(g.metric)} "
                          f"({g.deadline or 'period not stated'})", subject=g.metric[:80],
                thresholds=(), related_claim_ids=(g.statement_claim_id,), related_risk_ids=(),
                source_required="Results for the guided period", status="OPEN"))
    for item in d13.open_diligence_items:
        if item.importance.value in ("HIGH", "MEDIUM"):
            out.append(ViewChangeEntry(
                entry_id=uuid5(NS, f"view|diligence|{item.item_id}"),
                direction=ViewDirection.RESOLVES_UNCERTAINTY,
                condition="An open diligence question is answered from its source",
                subject="OPEN_DILIGENCE", thresholds=(), related_claim_ids=(),
                related_risk_ids=item.related_risk_ids,
                source_required=item.needed_source, status="OPEN"))
    for e in d13.source_disagreements.entries:
        if e.status == "OPEN":
            out.append(ViewChangeEntry(
                entry_id=uuid5(NS, f"view|conflict|{e.entry_id}"),
                direction=ViewDirection.RESOLVES_UNCERTAINTY,
                condition="A filed source settles an open source disagreement",
                subject="SOURCE_DISAGREEMENT", thresholds=(), related_claim_ids=(),
                related_risk_ids=(), source_required="The filed document for the period",
                status="OPEN"))
    for entry in out:
        ctx.item(S.WHAT_CHANGES_THE_VIEW, entry.entry_id)
    return out


Trigger = Literal["NEXT_RESULTS", "NEXT_ANNUAL_REPORT", "NEXT_SHAREHOLDING_FILING",
                  "NEXT_RATING_ACTION", "ON_EXCHANGE_FILING", "ON_DATA_RESOLUTION"]
MONITOR_CODES: tuple[tuple[str, str, Trigger], ...] = (
    ("CFO_CONVERSION", "CFO_TO_PAT", "NEXT_ANNUAL_REPORT"),
    ("DEBTOR_DAYS", "RECEIVABLE_DAYS", "NEXT_ANNUAL_REPORT"),
    ("MARGIN", "OPERATING_MARGIN", "NEXT_RESULTS"),
    ("DEBT", "GROSS_DEBT", "NEXT_ANNUAL_REPORT"),
    ("CAPEX", "CAPEX", "NEXT_ANNUAL_REPORT"),
)


def monitoring(ctx: Ctx, tracker: list[GuidanceTrackerEntry], view: OrderBookView) -> list[
        MonitoringItem]:
    d13 = ctx.box13
    out: list[MonitoringItem] = []
    flags_by_metric: dict[str, list[UUID]] = {}
    for i in d13.red_flags:
        flags_by_metric.setdefault(d13.signal(i).metric, []).append(i)

    def add(topic: str, subject: str, reason: str, latest: Iterable[UUID],
            trigger: Trigger, source: str, risks: Iterable[UUID] = (),
            limits: tuple[ThresholdRef, ...] = ()) -> None:
        item = MonitoringItem(
            item_id=uuid5(NS, f"monitor|{ctx.box4.company_key}|{topic}|{subject}"),
            topic=topic, subject=subject[:80], reason=reason, latest_claim_ids=tuple(latest),
            thresholds=limits, source_to_check=source, trigger=trigger,
            related_risk_ids=tuple(risks))
        out.append(item)
        ctx.item(S.MONITORING, item.item_id)

    latest_quarter = periods(ctx, ctx.analytical, "QUARTER")
    add("NEXT_RESULTS", "QUARTERLY_RESULTS", "Next quarterly results after the latest "
        f"reported quarter ({latest_quarter[-1] if latest_quarter else 'none on file'})", (),
        "NEXT_RESULTS", "Exchange results filing")
    years = periods(ctx, ctx.analytical, "FY")
    for topic, code, trigger in MONITOR_CODES:
        latest: list[UUID] = []
        if years:
            m = ctx.b5.get((code, ctx.analytical.value, "FY", years[-1])) or ctx.b5.get(
                (code, ctx.analytical.value, "POINT_IN_TIME", years[-1]))
            if m is not None and m.status == CalculationStatus.UNSUPPORTED_SECTOR:
                ctx.not_applicable(S.MONITORING, topic)  # Box 5: not this sector's metric
                continue
            claim = ctx.metric(m) if m else None
            if claim is not None:
                latest.append(claim)
        flags = flags_by_metric.get(code, [])
        add(topic, code, f"Track {human(code)} against its history and Box 13 rules", latest,
            trigger, "Annual report / results", flags,
            tuple(t for f in flags for t in thresholds(d13.signal(f))))
    for i in d13.red_flags:
        s = d13.signal(i)
        add("RISK_FLAG", s.code, f"{s.severity.value if s.severity else ''} company risk "
            f"{s.code}: re-test when its inputs are next reported",
            (ctx.signal_claims[i],), "NEXT_ANNUAL_REPORT", "The rule's input documents", (i,),
            thresholds(s))
    open_guidance = [g for g in tracker if g.result == "NOT_YET_DUE"]
    if open_guidance:
        add("GUIDANCE_DELIVERY", "GUIDANCE", f"{len(open_guidance)} guidance items not yet due",
            tuple(g.statement_claim_id for g in open_guidance[:20]), "NEXT_RESULTS",
            "Results for the guided period")
    if view.applicable:
        add("ORDER_INFLOW", "ORDER_BOOK", "Order inflow, cancellations and L1 conversion (kept "
            "by stage)", tuple(b.count_claim_id for b in view.buckets if b.count_claim_id),
            "ON_EXCHANGE_FILING", "Exchange order filings (LOA / cancellation)")
    if d13.liquidity_credit_risks.ratings:
        add("RATING", "CREDIT_RATING", "Rating actions by agency and instrument", (),
            "NEXT_RATING_ACTION", "Rating-agency press releases")
    if d13.ownership_risks.series:
        add("PROMOTER_HOLDING", "PROMOTER_HOLDING", "Promoter holding and pledge each quarter",
            (), "NEXT_SHAREHOLDING_FILING", "Exchange shareholding-pattern filing")
    litigation = [e for e in d13.exchange_event_risks.entries
                  if e.event_type in ("LITIGATION", "REGULATORY")]
    if litigation:
        add("LITIGATION", "LITIGATION", "Litigation / regulatory filings (direction must be "
            "cited before it is graded)", (), "ON_EXCHANGE_FILING", "Exchange filings")
    for kind, n in d13.data_quality_summary.counts:
        if kind in ("BLOCKED_SOURCE", "MISSING_REQUIRED_DOCUMENT", "INCOMPLETE_ORDER_LINKAGE",
                    "INPUT_CONFLICT", "STALE_DATA", "INCOMPLETE_COVERAGE_WINDOW"):
            add("DATA_GAP", kind, f"Data gap {human(kind)} ({n} items)", (),
                "ON_DATA_RESOLUTION", "The blocked / missing source")
    return out


__all__ = [
    "Ctx",
    "Draft",
    "build_signals",
    "business",
    "cash_balance",
    "concalls",
    "credit",
    "disagreements",
    "events",
    "forecasts",
    "forensics",
    "historical",
    "industry",
    "management",
    "monitoring",
    "order_book",
    "overview",
    "reconciliation",
    "risks",
    "scenarios",
    "shareholding",
    "valuation",
    "what_changes",
]
