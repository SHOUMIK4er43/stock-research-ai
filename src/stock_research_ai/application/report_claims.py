"""Box 14 claim book: deduplicated claims, the source register, typed tables and the
provenance resolver that walks every claim back to its documents.

One underlying fact is ONE claim (stable uuid5 over its key, never over generated_at); several
sections may reference it. One document / record is ONE source however often it is cited.
Nothing here computes an economic number: values are copied from upstream records.
"""

import hashlib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from pydantic import BaseModel

from stock_research_ai.domain.business_intelligence import (
    BusinessIntelligenceDataset,
    SourceLocation,
)
from stock_research_ai.domain.document_intelligence import EvidenceBundle
from stock_research_ai.domain.final_research_report import (
    CellStatus,
    ClaimBasis,
    ClaimType,
    ColumnKind,
    Layer,
    Materiality,
    Polarity,
    ReportClaim,
    ReportLimits,
    ReportSource,
    ReportTable,
    RowKind,
    Scenario,
    SectionId,
    SourceType,
    TableCell,
    TableColumn,
    TableRow,
    UpstreamRecord,
)
from stock_research_ai.domain.financial_analytics import CalculatedMetric, FinancialAnalyticsDataset
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalFinancialValue,
    Confidence,
    Currency,
    ReportingBasis,
    SourceFinancialObservation,
)
from stock_research_ai.domain.forecasting import ForecastDataset
from stock_research_ai.domain.fundamental_valuation import ValuationDataset
from stock_research_ai.domain.management_intelligence import (
    ManagementIntelligenceDataset,
)
from stock_research_ai.domain.management_intelligence import (
    Provenance as Box8Provenance,
)
from stock_research_ai.domain.market_intelligence import (
    CorporateEvent,
    EventSourceRef,
    MarketIntelligenceDataset,
    OrderChain,
)

NS = UUID("0f7c2b9e-54a1-5d3c-8e6f-14b1a2c3d4e5")


class ReportCompositionError(ValueError):
    """The inputs cannot be composed into one trustworthy report (fails closed)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def url_key(url: str | None) -> str | None:
    """A citation URL without its fragment (a table-cell locator is not another document)."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path or '/'}{query}"[:1000]


def basis_of(value: ReportingBasis | str | None) -> ClaimBasis:
    text = getattr(value, "value", value)
    if text == "CONSOLIDATED":
        return ClaimBasis.CONSOLIDATED
    if text == "STANDALONE":
        return ClaimBasis.STANDALONE
    return ClaimBasis.NOT_APPLICABLE


STATEMENT_PLACES = Decimal("0.000001")


def number(value: Decimal | None) -> str:
    """The upstream value as written in a claim's statement: every digit up to six decimals;
    longer arithmetic carries (Box 10 / Box 11) are rounded half-even to six places in the
    TEXT only - the claim's value keeps every digit and Box 15 formats it."""
    if value is None:
        return "unavailable"
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -6:
        value = value.quantize(STATEMENT_PLACES, rounding=ROUND_HALF_EVEN)
    return format(value, "f")


def _day(value: datetime | date | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value


# --- source register -------------------------------------------------------------------------


@dataclass
class _Source:
    key: str
    source_type: SourceType
    title: str
    url: str | None
    authority: set[str] = field(default_factory=set)
    document_ids: set[UUID] = field(default_factory=set)
    shas: set[str] = field(default_factory=set)
    document_date: date | None = None
    retrieval_date: date | None = None
    layers: set[Layer] = field(default_factory=set)
    status: str = "AVAILABLE"
    limitations: set[str] = field(default_factory=set)


_STATUS_RANK = {"AVAILABLE": 0}  # any non-AVAILABLE status is kept (worst wins, by name)


@dataclass
class _Claim:
    claim: ReportClaim
    sections: set[SectionId] = field(default_factory=set)


class Book:
    """Collects claims, sources and tables in a deterministic order."""

    def __init__(self, company_key: str, as_of: date, limits: ReportLimits) -> None:
        self.company_key = company_key
        self.as_of = as_of
        self.limits = limits
        self._sources: dict[str, _Source] = {}
        self._doc_url: dict[UUID, str] = {}
        self._claims: dict[UUID, _Claim] = {}
        self._tables: list[ReportTable] = []
        self._types: dict[UUID, SourceType] = {}
        self.dataset_sources: dict[Layer, UUID] = {}

    # --- sources
    def alias(self, document_id: UUID | None, url: str | None) -> None:
        """Teach the register that a document ID and a URL are the same document."""
        key = url_key(url)
        if document_id is not None and key is not None:
            self._doc_url.setdefault(document_id, key)

    def source(
        self,
        layer: Layer,
        source_type: SourceType,
        authority: str,
        *,
        url: str | None = None,
        document_id: UUID | None = None,
        sha: str | None = None,
        reference: str | None = None,
        title: str | None = None,
        document_date: date | datetime | None = None,
        retrieval_date: date | datetime | None = None,
        status: str = "AVAILABLE",
        limitation: str | None = None,
    ) -> UUID:
        key = url_key(url) or (self._doc_url.get(document_id) if document_id else None)
        if key is None:
            if document_id is not None:
                key = f"document:{document_id}"
            elif sha is not None:
                key = f"sha256:{sha}"
            elif reference:
                key = f"reference:{reference.strip()[:900]}"
            else:
                raise ReportCompositionError("SOURCE_WITHOUT_IDENTITY",
                                             f"a {layer.value} source has no URL / ID / reference")
        entry = self._sources.get(key)
        if entry is None:
            if len(self._sources) >= self.limits.max_sources:
                raise ReportCompositionError("RESOURCE_LIMIT_EXCEEDED", "more than "
                                             f"{self.limits.max_sources} distinct sources")
            entry = _Source(key=key, source_type=source_type,
                            title=(title or reference or key)[:1000],
                            url=key if key.startswith("http") else None)
            self._sources[key] = entry
        elif _TYPE_RANK[source_type] < _TYPE_RANK[entry.source_type]:
            entry.source_type = source_type  # a filed document outranks a dataset mention
        entry.authority.add(f"{layer.value}:{authority}"[:1000])
        entry.layers.add(layer)
        if document_id is not None:
            entry.document_ids.add(document_id)
        if sha is not None:
            entry.shas.add(sha)
        for attr, raw in (("document_date", document_date), ("retrieval_date", retrieval_date)):
            day = _day(raw)
            current = getattr(entry, attr)
            if day is not None and (current is None or day < current):
                setattr(entry, attr, day)
        if status != "AVAILABLE":
            entry.status = max(entry.status, status) if entry.status != "AVAILABLE" else status
        if limitation:
            entry.limitations.add(limitation)
        sid = uuid5(NS, f"source|{self.company_key}|{key}")
        self._types[sid] = entry.source_type
        return sid

    def get_source_type(self, source_id: UUID) -> SourceType:
        return self._types[source_id]

    def dataset_source(self, layer: Layer, dataset_id: UUID | None, sha: str, title: str,
                       status: str) -> UUID:
        sid = self.source(layer, SourceType.UPSTREAM_DATASET, f"DATASET/{status}",
                          reference=f"{layer.value} dataset {dataset_id or sha}", title=title,
                          document_id=None, sha=sha)
        self.dataset_sources[layer] = sid
        return sid

    # --- claims
    def claim(
        self,
        section: SectionId | None,
        claim_type: ClaimType,
        topic: str,
        statement: str,
        *,
        basis: ClaimBasis,
        upstream: Iterable[UpstreamRecord],
        sources: Iterable[UUID] = (),
        value: Decimal | None = None,
        metric: str | None = None,
        unit: str | None = None,
        currency: Currency | None = None,
        period: str | None = None,
        period_end: date | None = None,
        scenario: Scenario | None = None,
        source_text: str | None = None,
        evidence_ids: Iterable[UUID] = (),
        calculation_ids: Iterable[UUID] = (),
        formula_ids: Iterable[str] = (),
        risk_ids: Iterable[UUID] = (),
        derived_from: Iterable[UUID] = (),
        confidence: Confidence | None = None,
        polarity: Polarity = Polarity.NEUTRAL,
        limitations: Iterable[str] = (),
    ) -> UUID:
        ups = tuple(sorted(set(upstream), key=lambda u: (u.layer.value, u.kind,
                                                          str(u.record_id))))
        key = "|".join((
            claim_type.value, topic, metric or "-", period or "-", str(period_end or "-"),
            basis.value, scenario.value if scenario else "-", number(value),
            ",".join(f"{u.layer.value}:{u.kind}:{u.record_id}" for u in ups),
            ",".join(sorted(limitations)) if claim_type == ClaimType.DATA_LIMITATION else "-",
            # equal content only: the same record read two ways is two claims, never merged
            hashlib.sha256(f"{statement}\x00{source_text or ''}".encode()).hexdigest(),
        ))
        claim_id = uuid5(NS, f"claim|{self.company_key}|{key}")
        found = self._claims.get(claim_id)
        if found is None:
            if len(self._claims) >= self.limits.max_claims:
                raise ReportCompositionError("RESOURCE_LIMIT_EXCEEDED",
                                             f"more than {self.limits.max_claims} claims")
            claim = ReportClaim(
                claim_id=claim_id, claim_type=claim_type, topic=topic,
                statement=statement[:4000], source_text=source_text[:4000] if source_text
                else None, metric=metric, value=value, unit=unit, currency=currency,
                period=period[:1000] if period else None, period_end=period_end, basis=basis,
                scenario=scenario, source_ids=tuple(sorted(set(sources), key=str))[:500],
                upstream=ups[:500], evidence_ids=tuple(sorted(set(evidence_ids), key=str))[:500],
                calculation_ids=tuple(sorted(set(calculation_ids), key=str))[:500],
                formula_ids=tuple(sorted(set(formula_ids)))[:50],
                risk_ids=tuple(sorted(set(risk_ids), key=str))[:500],
                derived_from=tuple(sorted(set(derived_from), key=str))[:500],
                confidence=confidence, polarity=polarity,
                limitations=tuple(sorted(set(limitations)))[:50])
            found = _Claim(claim)
            self._claims[claim_id] = found
        if section is not None:
            found.sections.add(section)
        return claim_id

    def attach(self, claim_ids: Iterable[UUID], section: SectionId) -> None:
        for i in claim_ids:
            self._claims[i].sections.add(section)

    def get(self, claim_id: UUID) -> ReportClaim:
        return self._claims[claim_id].claim

    def limitation(self, section: SectionId | None, topic: str, statement: str,
                   code: str, *, upstream: Iterable[UpstreamRecord], basis: ClaimBasis =
                   ClaimBasis.NOT_APPLICABLE, sources: Iterable[UUID] = (),
                   period: str | None = None, period_end: date | None = None) -> UUID:
        return self.claim(section, ClaimType.DATA_LIMITATION, topic, statement, basis=basis,
                          upstream=upstream, sources=sources, limitations=(code,),
                          period=period, period_end=period_end)

    # --- tables
    def table(
        self,
        section: SectionId,
        code: str,
        title: str,
        basis: ClaimBasis,
        columns: list[TableColumn],
        rows: list[TableRow],
        *,
        period: str | None = None,
        scenario: Scenario | None = None,
        footnotes: Iterable[str] = (),
        appendix: bool = False,
        truncated: bool = False,
    ) -> UUID:
        if len(self._tables) >= self.limits.max_tables:
            raise ReportCompositionError("RESOURCE_LIMIT_EXCEEDED",
                                         f"more than {self.limits.max_tables} tables")
        if len(rows) > self.limits.max_rows:
            raise ReportCompositionError("RESOURCE_LIMIT_EXCEEDED",
                                         f"table {code} has more than {self.limits.max_rows} rows")
        table_id = uuid5(NS, f"table|{self.company_key}|{section.value}|{code}")
        claim_ids: list[UUID] = []
        for row in rows:
            for i in (row.claim_id, *(c.claim_id for c in row.cells)):
                if i is not None and i not in claim_ids:
                    claim_ids.append(i)
        target = SectionId.APPENDICES if appendix else section
        self.attach(claim_ids, target)
        source_ids = sorted({s for i in claim_ids for s in self.get(i).source_ids}, key=str)
        self._tables.append(ReportTable(
            table_id=table_id, section_id=target, code=code, title=title, basis=basis,
            period=period, scenario=scenario, columns=tuple(columns), rows=tuple(rows),
            footnotes=tuple(footnotes), source_ids=tuple(source_ids), claim_ids=tuple(claim_ids),
            appendix=appendix, truncated=truncated))
        return table_id

    # --- assembly
    def finish(self, materiality: dict[UUID, Materiality]) -> tuple[
            tuple[ReportClaim, ...], tuple[ReportSource, ...], tuple[ReportTable, ...]]:
        claims: list[ReportClaim] = []
        usage: dict[UUID, tuple[set[SectionId], set[ClaimBasis], int]] = {}
        for cid, entry in self._claims.items():
            sections = tuple(s for s in SectionId if s in entry.sections)
            claim = entry.claim.model_copy(update={
                "section_ids": sections,
                "materiality": materiality.get(cid, Materiality.LOW)})
            claims.append(claim)
            for s in claim.source_ids:
                secs, bases, n = usage.setdefault(s, (set(), set(), 0))
                secs.update(sections)
                bases.add(claim.basis)
                usage[s] = (secs, bases, n + 1)
        sources: list[ReportSource] = []
        for key, src in self._sources.items():
            sid = uuid5(NS, f"source|{self.company_key}|{key}")
            secs, bases, n = usage.get(sid, (set(), set(), 0))
            sources.append(ReportSource(
                source_id=sid, key=key, source_type=src.source_type,
                authority=tuple(sorted(src.authority))[:50], title=src.title,
                document_ids=tuple(sorted(src.document_ids, key=str))[:50],
                artifact_sha256=tuple(sorted(src.shas))[:50], url=src.url,
                document_date=src.document_date, retrieval_date=src.retrieval_date,
                layers=tuple(x for x in Layer if x in src.layers),
                section_ids=tuple(x for x in SectionId if x in secs),
                bases=tuple(x for x in ClaimBasis if x in bases), status=src.status,
                limitations=tuple(sorted(src.limitations)), claim_count=n))
        sources.sort(key=lambda s: (s.source_type.value, s.key))
        claims.sort(key=lambda c: (c.topic, str(c.claim_id)))
        return tuple(claims), tuple(sources), tuple(self._tables)

    @property
    def claim_ids(self) -> set[UUID]:
        return set(self._claims)

    @property
    def tables(self) -> tuple[ReportTable, ...]:
        return tuple(self._tables)

    @property
    def source_count(self) -> int:
        return len(self._sources)


_TYPE_RANK = {t: i for i, t in enumerate((
    SourceType.FILING, SourceType.EXCHANGE_RECORD, SourceType.RATING_AGENCY,
    SourceType.COMPANY_DOCUMENT, SourceType.PROVIDER_RECORD, SourceType.ANALYST_INPUT,
    SourceType.UPSTREAM_DATASET))}


# --- table helpers ---------------------------------------------------------------------------


def col(key: str, label: str, kind: ColumnKind = ColumnKind.VALUE, *, period: str | None = None,
        period_end: date | None = None, scenario: Scenario | None = None,
        basis: ClaimBasis | None = None) -> TableColumn:
    return TableColumn(key=key, label=label, kind=kind, period=period, period_end=period_end,
                       scenario=scenario, basis=basis)


def cell(column: str, book: Book, claim_id: UUID | None, *, note: str = "UNAVAILABLE",
         not_applicable: bool = False) -> TableCell:
    """A numeric cell whose value is the claim's value (never a separate copy)."""
    if claim_id is not None:
        value = book.get(claim_id).value
        if value is not None:
            return TableCell(column=column, status=CellStatus.AVAILABLE, value=value,
                             claim_id=claim_id)
    status = CellStatus.NOT_APPLICABLE if not_applicable else CellStatus.UNAVAILABLE
    return TableCell(column=column, status=status, claim_id=claim_id, note=note)


def text(column: str, value: str | None, *, origin: str = "UPSTREAM",
         claim_id: UUID | None = None) -> TableCell:
    if value is None or not value.strip():
        return TableCell(column=column, status=CellStatus.UNAVAILABLE, note="NOT_STATED",
                         claim_id=claim_id)
    return TableCell(column=column, status=CellStatus.TEXT, text=value.strip()[:1000],
                     text_origin="UPSTREAM" if origin == "UPSTREAM" else "REPORT",
                     claim_id=claim_id)


def row(key: str, label: str, kind: RowKind, cells: list[TableCell], *, unit: str | None = None,
        claim_id: UUID | None = None) -> TableRow:
    return TableRow(key=key[:1000], label=label[:1000], kind=kind, unit=unit,
                    cells=tuple(cells), claim_id=claim_id)


# --- provenance resolver -----------------------------------------------------------------------

Prov = tuple[tuple[UUID, ...], tuple[UpstreamRecord, ...], tuple[UUID, ...]]


def up(layer: Layer, kind: str, record_id: UUID) -> UpstreamRecord:
    return UpstreamRecord(layer=layer, kind=kind, record_id=record_id)


class Resolver:
    """Maps upstream records to registered sources (documents, filings, provider records)."""

    def __init__(self, book: Book, box4: CanonicalFinancialDataset,
                 box5: FinancialAnalyticsDataset, *, bundle: EvidenceBundle | None,
                 box7: BusinessIntelligenceDataset | None,
                 box8: ManagementIntelligenceDataset | None,
                 box10: ForecastDataset | None, box11: ValuationDataset | None,
                 box12: MarketIntelligenceDataset | None) -> None:
        self.book = book
        self.observations: dict[UUID, SourceFinancialObservation] = {
            o.observation_id: o for o in box4.observations}
        self.values: dict[UUID, CanonicalFinancialValue] = {
            v.canonical_value_id: v for v in box4.canonical_values}
        self.metrics: dict[UUID, CalculatedMetric] = {m.metric_id: m for m in _metrics(box5)}
        self.bundle_docs: dict[UUID, tuple[str, str, str, datetime]] = {}
        self.evidence_doc: dict[UUID, UUID] = {}
        if bundle is not None:
            for x in bundle.extractions:
                self.bundle_docs[x.document_id] = (x.document_type.value, x.authority.value,
                                                   x.artifact_sha256, x.extracted_at)
                for item in x.evidence:
                    self.evidence_doc[item.evidence_id] = x.document_id
        self.box7_obs: dict[UUID, SourceLocation] = {}
        if box7 is not None:
            for s in box7.segment_observations:
                self.box7_obs[s.observation_id] = s.source
            for g in box7.geography_observations:
                self.box7_obs[g.observation_id] = g.source
            for k in box7.operating_kpi_observations:
                self.box7_obs[k.kpi_observation_id] = k.source
        self.forecast_values = {v.value_id: v for v in (*box10.actuals, *box10.year_to_date_actuals,
                                                        *box10.values)} \
            if box10 else {}
        self.assumptions = {a.assumption_id: a for a in box10.assumptions} if box10 else {}
        self.valuation_values = {v.value_id: v for v in box11.values} if box11 else {}
        self.events: dict[UUID, CorporateEvent] = {e.event_id: e for e in box12.events} \
            if box12 else {}
        self.orders = {o.order_id: o for o in box12.orders} if box12 else {}
        self.payloads = {p.payload_id: p for p in box12.payloads} if box12 else {}
        self._lineage: dict[UUID, frozenset[UUID]] = {}
        # teach the register which document IDs and URLs name the same document
        for o in box4.observations:
            book.alias(o.source.document_id, o.source.source_url)
        if box8 is not None:
            for st in box8.statements:
                book.alias(st.provenance.document_id, st.provenance.source_url)
            for e in box8.events:
                book.alias(e.document_id, e.source_url)
        for loc in self.box7_obs.values():
            book.alias(loc.document_id, loc.source_url)

    # --- Box 4 / Box 5
    def observation(self, observation_id: UUID) -> UUID | None:
        o = self.observations.get(observation_id)
        if o is None:
            return None
        src = o.source
        kind = SourceType.FILING if src.source_type.value == "FILING" else \
            SourceType.PROVIDER_RECORD
        title = None
        if src.document_id is not None and src.document_id in self.bundle_docs:
            title = f"{self.bundle_docs[src.document_id][0].replace('_', ' ').title()}"
        return self.book.source(
            Layer.BOX4, kind, f"{src.source_type.value}/{o.authority.value}",
            url=src.source_url, document_id=src.document_id, sha=src.artifact_sha256,
            title=title or ("Screener company page" if kind == SourceType.PROVIDER_RECORD
                            else "Filed financial statement"),
            retrieval_date=src.retrieved_or_extracted_at)

    def canonical(self, value_id: UUID) -> Prov:
        v = self.values.get(value_id)
        if v is None:
            return (), (), ()
        observations = (v.selected_observation_id, *v.supporting_observation_ids)
        sources = tuple(s for s in (self.observation(o) for o in observations) if s)
        return sources, (up(Layer.BOX4, "CANONICAL_VALUE", value_id),), observations

    def metric(self, metric_id: UUID) -> Prov:
        m = self.metrics.get(metric_id)
        if m is None:
            return (), (), ()
        observations = tuple(p.selected_observation_id for p in m.provenance)
        sources = tuple(s for s in (self.observation(o) for o in observations) if s)
        ups = (up(Layer.BOX5, "METRIC", metric_id),
               *(up(Layer.BOX4, "CANONICAL_VALUE", p.canonical_value_id) for p in m.provenance))
        return sources, ups, observations

    def record(self, record_id: UUID) -> Prov:
        """A Box 4 canonical value, Box 4 observation or Box 5 metric, whichever it is."""
        if record_id in self.values:
            return self.canonical(record_id)
        if record_id in self.metrics:
            return self.metric(record_id)
        if record_id in self.observations:
            s = self.observation(record_id)
            return ((s,) if s else ()), (up(Layer.BOX4, "OBSERVATION", record_id),), (
                record_id,)
        return (), (), ()

    def records(self, record_ids: Iterable[UUID]) -> Prov:
        sources: list[UUID] = []
        ups: list[UpstreamRecord] = []
        evidence: list[UUID] = []
        for r in record_ids:
            s, u, e = self.record(r)
            sources += s
            ups += u
            evidence += e
        return tuple(sources), tuple(ups), tuple(evidence)

    # --- Box 3 / Box 13 labels
    def evidence(self, evidence_id: UUID) -> UUID | None:
        doc = self.evidence_doc.get(evidence_id)
        if doc is None:
            return None
        kind, authority, sha, extracted = self.bundle_docs[doc]
        return self.book.source(Layer.BOX3, _doc_type(kind, authority), f"{kind}/{authority}",
                                document_id=doc, sha=sha, title=kind.replace("_", " ").title(),
                                retrieval_date=extracted)

    def label(self, label: str, layer: Layer, authority: str) -> UUID:
        """A Box 13 / Box 12 source label: a URL (fragment = locator) or a document reference."""
        key = url_key(label.split("#", 1)[0]) if "://" in label else None
        kind = _url_type(key) if key else SourceType.ANALYST_INPUT
        if key is None and re.match(r"^BOX\d+:", label):
            kind = SourceType.UPSTREAM_DATASET  # an upstream box's own record reference
        if key is None and authority in ("EXCHANGE_FILING", "COMPANY_DOCUMENT",
                                         "RATING_AGENCY"):
            kind = {"EXCHANGE_FILING": SourceType.FILING,
                    "COMPANY_DOCUMENT": SourceType.COMPANY_DOCUMENT,
                    "RATING_AGENCY": SourceType.RATING_AGENCY}[authority]
        return self.book.source(layer, kind, authority, url=key, reference=label,
                                title=None if key else label)

    # --- Box 7 / Box 8
    def location(self, loc: SourceLocation, layer: Layer = Layer.BOX7) -> UUID:
        return self.book.source(layer, _doc_type(loc.document_type.value if loc.document_type
                                                 else "OTHER", loc.authority.value),
                                f"{loc.source_kind.value}/{loc.authority.value}",
                                url=loc.source_url, document_id=loc.document_id,
                                sha=loc.artifact_sha256)

    def box8(self, p: Box8Provenance, call_date: date | None = None) -> UUID:
        return self.book.source(Layer.BOX8, _doc_type(p.document_type.value, p.authority.value),
                                f"{p.document_type.value}/{p.authority.value}",
                                url=p.source_url, document_id=p.document_id,
                                sha=p.artifact_sha256,
                                title=p.document_type.value.replace("_", " ").title(),
                                document_date=call_date)

    # --- Box 10 / Box 11 lineage: forecast values back to the Box 4 values they rest on
    def lineage(self, value_id: UUID, depth: int = 0) -> frozenset[UUID]:
        if value_id in self._lineage:
            return self._lineage[value_id]
        found: set[UUID] = set()
        if depth > 200:
            return frozenset()
        v = self.forecast_values.get(value_id)
        if v is not None:
            if v.actual is not None:
                found.add(v.actual.canonical_value_id)
            found.update(c.canonical_value_id for c in v.actual_components)
            for i in v.inputs:
                if i.kind == "ASSUMPTION" and i.ref_id in self.assumptions:
                    found.update(self.assumptions[i.ref_id].canonical_value_ids)
                elif i.ref_id != value_id:
                    found.update(self.lineage(i.ref_id, depth + 1))
        a = self.assumptions.get(value_id)
        if a is not None:
            found.update(a.canonical_value_ids)
        w = self.valuation_values.get(value_id)
        if w is not None:
            found.update(b.canonical_value_id for b in w.box4_inputs)
            for j in w.inputs:
                if j.ref_id is not None and j.ref_id != value_id:
                    found.update(self.lineage(j.ref_id, depth + 1))
        result = frozenset(found)
        self._lineage[value_id] = result
        return result

    def anchored(self, value_id: UUID, limit: int = 20) -> tuple[UUID, ...]:
        """The document sources of the Box 4 values a forecast / valuation value rests on."""
        out: list[UUID] = []
        for cv in sorted(self.lineage(value_id), key=str)[:limit]:
            out += self.canonical(cv)[0]
        return tuple(dict.fromkeys(out))

    # --- Box 12
    def event_sources(self, event: CorporateEvent) -> tuple[UUID, ...]:
        out = [self._ref(r, event.event_date) for r in event.sources]
        if event.source_url:
            out.append(self.book.source(Layer.BOX12, SourceType.FILING,
                                        f"EVENT_ATTACHMENT/{event.authority.value}",
                                        url=event.source_url, title=event.event_type.value
                                        .replace("_", " ").title() + " filing",
                                        document_date=event.event_date))
        return tuple(dict.fromkeys(out))

    def _ref(self, r: EventSourceRef, day: date | None = None) -> UUID:
        payload = self.payloads.get(r.payload_id)
        url = r.source_url or (payload.url if payload else None)
        status = payload.status.value if payload else "AVAILABLE"
        return self.book.source(
            Layer.BOX12, SourceType.EXCHANGE_RECORD if r.provider.value in ("NSE", "BSE")
            else SourceType.PROVIDER_RECORD, f"{r.provider.value}/{r.authority.value}",
            url=url, reference=f"{r.provider.value} {r.locator}" if url is None else None,
            title=f"{r.provider.value} record", document_date=r.published_at or day,
            retrieval_date=payload.retrieved_at if payload else None,
            status="AVAILABLE" if status == "OK" else status)

    def refs(self, refs: Iterable[EventSourceRef]) -> tuple[UUID, ...]:
        return tuple(dict.fromkeys(self._ref(r) for r in refs))

    def payload(self, payload_id: UUID) -> UUID | None:
        p = self.payloads.get(payload_id)
        if p is None:
            return None
        return self.book.source(
            Layer.BOX12, SourceType.EXCHANGE_RECORD if p.provider.value in ("NSE", "BSE")
            else SourceType.PROVIDER_RECORD, f"{p.provider.value}/{p.authority.value}",
            url=p.url, sha=p.content_sha256, title=f"{p.provider.value} {p.kind.value}",
            retrieval_date=p.retrieved_at,
            status="AVAILABLE" if p.status.value == "OK" else p.status.value,
            limitation=None if p.status.value == "OK" else f"SOURCE_{p.status.value}")

    def chain(self, chain: OrderChain) -> tuple[UUID, ...]:
        out: list[UUID] = []
        for oid in chain.order_ids:
            o = self.orders.get(oid)
            if o is None:
                continue
            e = self.events.get(o.event_id)
            if e is not None:
                out += self.event_sources(e)
            if o.source_url:
                out.append(self.book.source(Layer.BOX12, SourceType.FILING,
                                            f"ORDER_DOCUMENT/{o.stage.value}", url=o.source_url,
                                            sha=o.document_sha256, title="Order filing",
                                            document_date=o.announcement_date))
        return tuple(dict.fromkeys(out))


def _doc_type(document_type: str, authority: str) -> SourceType:
    if authority == "THIRD_PARTY" or "RATING" in document_type:
        return SourceType.RATING_AGENCY
    if authority == "PRIMARY_REGULATORY":
        return SourceType.FILING
    if authority == "PRIMARY_COMPANY":
        return SourceType.COMPANY_DOCUMENT
    if authority == "SECONDARY_AGGREGATOR":
        return SourceType.PROVIDER_RECORD
    return SourceType.FILING if document_type != "OTHER" else SourceType.PROVIDER_RECORD


def _url_type(key: str) -> SourceType:
    host = urlsplit(key).netloc
    if host.endswith("screener.in") or host.endswith("zerodha.com") or host.endswith(
            "kite.trade"):
        return SourceType.PROVIDER_RECORD
    if host.endswith("careratings.com") or host.endswith("crisil.com") or host.endswith(
            "icra.in") or host.endswith("indiaratings.co.in"):
        return SourceType.RATING_AGENCY
    if "nseindia.com" in host or "bseindia.com" in host:
        return SourceType.FILING if key.lower().endswith(".pdf") else SourceType.EXCHANGE_RECORD
    return SourceType.FILING if key.lower().endswith(".pdf") else SourceType.COMPANY_DOCUMENT


def _metrics(box5: FinancialAnalyticsDataset) -> Iterator[CalculatedMetric]:
    for name in ("building_blocks", "growth", "profitability", "returns", "dupont", "leverage",
                 "liquidity", "working_capital", "cash_flow_quality", "capital_allocation",
                 "accruals", "banking"):
        yield from getattr(box5, name)


# --- the reference validator -------------------------------------------------------------------


def id_index(model: BaseModel | None) -> set[UUID]:
    """Every UUID that appears anywhere in a dataset (records, IDs, references)."""
    found: set[UUID] = set()
    if model is None:
        return found
    stack: list[Any] = [model.model_dump(mode="python")]
    while stack:
        item = stack.pop()
        if isinstance(item, UUID):
            found.add(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend(item)
    return found


def verify_references(claims: Iterable[ReportClaim], index: dict[Layer, set[UUID]],
                      risk_ids: set[UUID]) -> None:
    """Every upstream record, evidence and calculation ID a claim cites exists upstream."""
    everything = set().union(*index.values()) if index else set()
    for c in claims:
        for u in c.upstream:
            if u.record_id not in index.get(u.layer, set()):
                raise ReportCompositionError(
                    "BROKEN_REFERENCE", f"claim {c.topic} cites {u.layer.value} {u.kind} "
                    f"{u.record_id}, which is not in the supplied {u.layer.value} dataset")
        for e in (*c.evidence_ids, *c.calculation_ids):
            if e not in everything:
                raise ReportCompositionError("BROKEN_REFERENCE", f"claim {c.topic} cites an "
                                             f"unknown evidence / calculation ID {e}")
        if not set(c.risk_ids) <= risk_ids:
            raise ReportCompositionError("BROKEN_REFERENCE", f"claim {c.topic} cites an unknown "
                                         "risk ID")


__all__ = [
    "Book",
    "NS",
    "ReportCompositionError",
    "Resolver",
    "basis_of",
    "cell",
    "col",
    "id_index",
    "number",
    "row",
    "text",
    "up",
    "url_key",
    "verify_references",
]
