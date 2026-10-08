"""DETAILED_RESEARCH_V1 Box 9: competitive mentions and market-position claims.

Text comes only from validated layers: Box 8 management statements (company assertions),
Box 8 analyst / journalist question turns, and Box 7 business evidence (company documents).
Company names resolve only through exact names / registered aliases of known companies;
an alias shared by several companies stays AMBIGUOUS and generic competition language stays
UNRESOLVED. A management claim ('we gained share', '20 % market share') stays a claim:
UNVERIFIED unless another source corroborates it; differing numbers are CONFLICTED and
preserved as conflicts.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid5

from stock_research_ai.application.management_statements import sentences
from stock_research_ai.application.peer_registry import (
    ALIASES,
    AMBIGUOUS_ALIASES,
    COMPETITION,
    CONTEXT_RULES,
    FIRST_PERSON,
    NS,
    SHARE_CATEGORY,
    SHARE_NUMERIC,
    SHARE_QUALITATIVE,
    SHARE_TOLERANCE_PP,
)
from stock_research_ai.application.peer_selection import CompanyLayers
from stock_research_ai.domain.business_intelligence import SourceKind
from stock_research_ai.domain.evidence import EvidenceDocumentType, SourceAuthority
from stock_research_ai.domain.industry_intelligence import (
    ClaimStatus,
    CompetitiveMention,
    ContextType,
    EvidenceRef,
    IndustryEvidenceConflict,
    MarketPositionClaim,
    Nature,
    Resolution,
    SourceLayer,
)
from stock_research_ai.domain.management_intelligence import Provenance, Role

_DISCLOSURES = {
    EvidenceDocumentType.ANNUAL_REPORT,
    EvidenceDocumentType.QUARTERLY_RESULT,
    EvidenceDocumentType.REGULATION_30_FILING,
    EvidenceDocumentType.EXCHANGE_ANNOUNCEMENT,
}


@dataclass(frozen=True)
class TextUnit:
    """One sentence-sized piece of upstream text with its full lineage."""

    company_key: str
    text: str
    layer: SourceLayer
    record_id: UUID
    dataset_id: UUID
    nature: Nature
    location: Provenance | None
    source_url: str | None
    locator: str | None
    authority: SourceAuthority | None
    document_type: EvidenceDocumentType | None
    event_date: date | None
    period_text: str | None
    speaker_role: str | None
    topics: tuple[str, ...] = ()
    event_id: UUID | None = None

    def ref(self) -> EvidenceRef:
        return EvidenceRef(
            company_key=self.company_key,
            layer=self.layer,
            record_id=self.record_id,
            dataset_id=self.dataset_id,
            location=self.location,
            source_url=self.source_url,
            locator=self.locator,
        )


def text_units(company: CompanyLayers, max_chars: int) -> list[TextUnit]:
    out: list[TextUnit] = []
    box8 = company.box8
    if box8 is not None:
        events = {e.event_id: e for e in box8.events}
        for s in box8.statements:
            e = events[s.event_id]
            out.append(
                TextUnit(
                    company.company_key,
                    s.quote[:max_chars],
                    SourceLayer.BOX8_STATEMENT,
                    s.statement_id,
                    box8.dataset_id,
                    Nature.COMPANY_ASSERTION,
                    s.provenance,
                    s.provenance.source_url,
                    s.provenance.locator,
                    s.provenance.authority,
                    s.provenance.document_type,
                    e.call_date,
                    e.reporting_period.raw_text if e.reporting_period else None,
                    s.speaker_role.value,
                    s.topics,
                    s.event_id,
                )
            )
        people = {p.participant_id: p for p in box8.participants}
        turns = {t.turn_id: t for t in box8.turns}
        for q in box8.qa_pairs:
            for tid in q.question_turn_ids:
                t = turns[tid]
                role = people[t.participant_id].role
                if role not in (Role.ANALYST, Role.MEDIA):
                    continue
                e = events[t.event_id]
                for index, (a, b) in enumerate(sentences(t.text)):
                    sentence = t.text[a:b].strip()
                    if len(sentence.split()) < 4:
                        continue
                    out.append(
                        TextUnit(
                            company.company_key,
                            sentence[:max_chars],
                            SourceLayer.BOX8_TURN,
                            t.turn_id,
                            box8.dataset_id,
                            Nature.ANALYST_QUESTION,
                            t.provenance,
                            t.provenance.source_url,
                            f"{t.provenance.locator}/q{index}",
                            t.provenance.authority,
                            t.provenance.document_type,
                            e.call_date,
                            e.reporting_period.raw_text if e.reporting_period else None,
                            role.value,
                            q.topics,
                            t.event_id,
                        )
                    )
    box7 = company.box7
    if box7 is not None:
        for ev in box7.business_evidence:
            src = ev.source
            nature = (
                Nature.AGGREGATOR_DATA
                if src.source_kind == SourceKind.BOX1B_PROFILE
                else Nature.COMPANY_DISCLOSURE
                if src.document_type in _DISCLOSURES
                else Nature.COMPANY_ASSERTION
            )
            for index, (a, b) in enumerate(sentences(ev.normalized_text)):
                sentence = ev.normalized_text[a:b].strip()
                if len(sentence.split()) < 4:
                    continue
                out.append(
                    TextUnit(
                        company.company_key,
                        sentence[:max_chars],
                        SourceLayer.BOX7_EVIDENCE,
                        ev.evidence_id,
                        box7.dataset_id,
                        nature,
                        None,
                        src.source_url,
                        f"{src.locator}/s{index}",
                        src.authority,
                        src.document_type,
                        None,
                        None,
                        None,
                        (ev.evidence_kind.value,),
                    )
                )
    return out


# --- identity -------------------------------------------------------------------------------------


@dataclass
class Identity:
    """Alias -> company key, built from the known companies (target + peer candidates)."""

    resolved: dict[str, str] = field(default_factory=dict)
    ambiguous: dict[str, tuple[str, ...]] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)

    def patterns(self) -> list[tuple[str, re.Pattern[str]]]:
        forms = sorted({*self.resolved, *self.ambiguous}, key=lambda f: (-len(f), f))
        out = []
        for form in forms:
            flags = 0 if len(form) <= 4 else re.I
            out.append((form, re.compile(rf"(?<![\w&]){re.escape(form)}(?![\w&])", flags)))
        return out


def identity(companies: dict[str, tuple[str | None, str | None]]) -> Identity:
    """companies: company key -> (Screener display name, symbol)."""
    out = Identity()
    by_symbol = {symbol: key for key, (_name, symbol) in companies.items() if symbol}
    for key, (name, symbol) in sorted(companies.items()):
        forms = set(ALIASES.get(symbol or "", ()))
        if name:
            forms.add(name)
            out.names[key] = name
        for form in forms:
            if form in AMBIGUOUS_ALIASES:
                continue
            if form in out.resolved and out.resolved[form] != key:
                out.ambiguous[form] = tuple(sorted({out.resolved.pop(form), key}))
            elif form not in out.ambiguous:
                out.resolved[form] = key
    for form, symbols in AMBIGUOUS_ALIASES.items():
        out.ambiguous[form] = tuple(by_symbol.get(s, s) for s in symbols)
    return out


def _context(text: str) -> ContextType:
    for kind, pattern in CONTEXT_RULES:
        if pattern.search(text):
            return kind
    return ContextType.GENERAL_COMPETITION if COMPETITION.search(text) else ContextType.OTHER


def competitive_mentions(
    units: list[TextUnit], ident: Identity, self_keys: dict[str, set[str]]
) -> list[CompetitiveMention]:
    """self_keys: source company -> its own company keys (never a competitor of itself)."""
    patterns = ident.patterns()
    out: list[CompetitiveMention] = []
    for u in units:
        if u.layer == SourceLayer.BOX7_EVIDENCE and not COMPETITION.search(u.text):
            continue  # company documents: only explicit competition context
        spans: list[tuple[int, int]] = []
        found: list[tuple[str, str | None, tuple[str, ...]]] = []
        for form, pattern in patterns:
            for m in pattern.finditer(u.text):
                if any(not (m.end() <= a or m.start() >= b) for a, b in spans):
                    continue
                spans.append(m.span())
                if form in ident.resolved:
                    resolved = ident.resolved[form]
                    if resolved in self_keys.get(u.company_key, set()):
                        continue
                    found.append((form, resolved, ()))
                else:  # an alias shared by several companies
                    found.append((form, None, ident.ambiguous[form]))
        if not found and COMPETITION.search(u.text) and u.nature != Nature.AGGREGATOR_DATA:
            found.append(("", None, ()))
        seen: set[tuple[str | None, str]] = set()
        for form, key, options in found:
            if (key, form) in seen:
                continue
            seen.add((key, form))
            resolution = (
                Resolution.RESOLVED
                if key
                else Resolution.AMBIGUOUS
                if options
                else Resolution.UNRESOLVED
            )
            out.append(
                CompetitiveMention(
                    mention_id=uuid5(
                        NS, f"mention|{u.layer.value}|{u.record_id}|{u.locator}|{form}"
                    ),
                    source_company_key=u.company_key,
                    mentioned_company_key=key,
                    raw_mention=form or None,
                    candidates=options,
                    resolution=resolution,
                    context_type=_context(u.text),
                    topics=u.topics,
                    nature=u.nature,
                    event_date=u.event_date,
                    speaker_role=u.speaker_role,
                    quote=u.text,
                    source=u.ref(),
                    confidence="HIGH" if key else "MEDIUM" if options else "LOW",
                )
            )
    return out


# --- market position ------------------------------------------------------------------------------


def _category(text: str) -> str:
    m = SHARE_CATEGORY.search(text)
    return " ".join(m.group("cat").lower().split()) if m else "UNSPECIFIED"


def market_claims(
    units: list[TextUnit], ident: Identity, self_keys: dict[str, set[str]]
) -> tuple[list[MarketPositionClaim], list[IndustryEvidenceConflict]]:
    raw: list[MarketPositionClaim] = []
    patterns = ident.patterns()
    for u in units:
        if u.nature == Nature.ANALYST_QUESTION:
            continue  # a question is not a claim
        numeric = SHARE_NUMERIC.search(u.text)
        qualitative = SHARE_QUALITATIVE.search(u.text)
        if not numeric and not qualitative:
            continue
        named = {ident.resolved[f] for f, p in patterns if f in ident.resolved and p.search(u.text)}
        mine = self_keys.get(u.company_key, set())
        others = named - mine
        if FIRST_PERSON.search(u.text) and u.nature != Nature.AGGREGATOR_DATA:
            subject = u.company_key
        elif len(others) == 1:
            subject = others.pop()
        elif named & mine:
            subject = u.company_key  # the company names itself
        else:
            continue  # nobody identifiable claims a position (e.g. a client case study)
        share = Decimal(numeric.group("v1") or numeric.group("v2")) if numeric else None
        raw.append(
            MarketPositionClaim(
                claim_id=uuid5(NS, f"claim|{u.layer.value}|{u.record_id}|{u.locator}"),
                company_key=subject,
                source_company_key=u.company_key,
                category=_category(u.text),
                stated_share=share,
                unit="PERCENT" if share is not None else None,
                qualitative=share is None,
                period_text=u.period_text,
                event_date=u.event_date,
                nature=u.nature,
                authority=u.authority,
                status=ClaimStatus.UNVERIFIED,
                quote=u.text,
                source=u.ref(),
                confidence="HIGH" if share is not None else "MEDIUM",
            )
        )
    tolerance = Decimal(SHARE_TOLERANCE_PP)
    conflicts: list[IndustryEvidenceConflict] = []
    out = []
    for c in raw:
        same = [
            o
            for o in raw
            if o.claim_id != c.claim_id
            and o.company_key == c.company_key
            and o.category == c.category
        ]
        independent = [
            o
            for o in same
            if o.source_company_key != c.source_company_key
            or (Nature.COMPANY_DISCLOSURE in (o.nature, c.nature) and o.nature != c.nature)
        ]
        corroborating, conflicting = [], []
        if c.stated_share is not None:
            for o in independent:
                if o.stated_share is None:
                    continue
                if abs(o.stated_share - c.stated_share) <= tolerance:
                    corroborating.append(o.claim_id)
            for o in same:
                if (
                    o.stated_share is not None
                    and o.period_text == c.period_text
                    and abs(o.stated_share - c.stated_share) > tolerance
                ):
                    conflicting.append(o.claim_id)
        if conflicting:
            status = ClaimStatus.CONFLICTED
        elif corroborating:
            status = ClaimStatus.CORROBORATED
        elif c.nature == Nature.COMPANY_ASSERTION:
            status = ClaimStatus.UNVERIFIED  # management opinion, uncorroborated
        else:
            status = ClaimStatus.SINGLE_SOURCE
        out.append(
            c.model_copy(
                update={
                    "status": status,
                    "corroborating_ids": tuple(sorted(corroborating, key=str)),
                    "conflicting_ids": tuple(sorted(conflicting, key=str)),
                }
            )
        )
        for other in conflicting:
            a, b = sorted([c.claim_id, other], key=str)
            if a != c.claim_id:
                continue
            o = next(x for x in raw if x.claim_id == b)
            conflicts.append(
                IndustryEvidenceConflict(
                    conflict_id=uuid5(NS, f"conflict|MARKET_SHARE|{a}|{b}"),
                    topic="MARKET_SHARE",
                    record_a=a,
                    record_b=b,
                    company_a=c.source_company_key,
                    company_b=o.source_company_key,
                    claim_a=f"{c.company_key} {c.category}: {c.stated_share}%",
                    claim_b=f"{o.company_key} {o.category}: {o.stated_share}%",
                    nature_a=c.nature,
                    nature_b=o.nature,
                    date_a=c.event_date,
                    date_b=o.event_date,
                )
            )
    return out, conflicts
