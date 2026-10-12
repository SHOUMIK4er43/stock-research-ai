"""DETAILED_RESEARCH_V1 Box 13: one deterministic policy for explicit, cited inputs.

Every explicit input family of a DueDiligenceRequest is normalized once, before any rule reads
it, so that no rule can pick an observation by input order:

  1. exact duplicates (numerically equal content, source included) are one record;
  2. records are grouped by a semantic key - what fact the record states (period / as-of,
     basis, category, agency, instrument, counterparty, ...), never its monetary value;
  3. records of one key that state the same content (everything but the source) are one
     observation, cited by its best-authority source;
  4. records of one key that state different content are a disagreement. The documented
     authority ranking (`AUTHORITY_RANK`: lower wins a disagreement on a filed fact) selects a
     canonical record only when exactly one content holds the best rank and it is not an
     analyst input; the losing records are preserved. Otherwise the disagreement stays OPEN
     and the key is withheld: a rule that needs it is UNAVAILABLE (INPUT_CONFLICT), never
     graded from one arbitrary value.

Families whose rows are separate line items (contingent categories without a description,
preferential issues, penalties) are keyed by their full content: exact duplicates collapse and
nothing else is merged. A different period, basis, currency or share-count basis is a
different key: never a conflict, never merged. Records are ordered by their canonical content,
so the normalized request - and everything built from it - is independent of input order.

The same `resolve` decides Box 12 / explicit observations of one fact (promoter holding).
"""

import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel

from stock_research_ai.application.red_flags import (
    NS,
    Ctx,
    DueDiligenceInputError,
    Ev,
    Trace,
    basis_of,
    unavailable,
)
from stock_research_ai.domain.due_diligence import (
    AUTHORITY_RANK,
    DisagreementGroup,
    DueDiligenceRequest,
    SignalBasis,
    SourceAuthority,
)

Key = tuple[object, ...]
G = DisagreementGroup


def fid(family: str, item: object) -> UUID:
    """The record ID of an explicit input: uuid5 over its content (equal content, equal ID)."""
    dump = getattr(item, "model_dump_json", None)
    return uuid5(NS, f"input|{family}|{dump() if dump else item}")


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()


def _plain(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value else "0"
    if isinstance(value, (date, UUID)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def canonical(item: BaseModel, *, exclude: frozenset[str] = frozenset()) -> str:
    """Content as text with numerically equal values written alike (10 == 10.0)."""
    return json.dumps(_plain(item.model_dump(exclude=set(exclude))), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)


# --- the authority decision -------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One record stating a fact: its authority rank and the content it states."""

    rank: int
    content: str
    order: str  # full canonical text: the deterministic tie-break inside one content
    item: Any


@dataclass(frozen=True)
class Resolution:
    status: Literal["SINGLE", "AGREE", "RESOLVED", "OPEN"]
    winner: Candidate | None  # None exactly when OPEN
    candidates: tuple[Candidate, ...]  # every record, sorted (losers preserved)


ANALYST_RANK = AUTHORITY_RANK[SourceAuthority.ANALYST_INPUT]


def resolve(candidates: Iterable[Candidate]) -> Resolution:
    rows = tuple(sorted(candidates, key=lambda c: (c.rank, c.content, c.order)))
    contents: dict[str, int] = {}
    for c in rows:
        contents[c.content] = min(contents.get(c.content, c.rank), c.rank)
    if len(contents) == 1:
        return Resolution("SINGLE" if len(rows) == 1 else "AGREE", rows[0], rows)
    best = min(contents.values())
    leaders = [k for k, r in contents.items() if r == best]
    if len(leaders) == 1 and best < ANALYST_RANK:
        return Resolution("RESOLVED", next(c for c in rows if c.content == leaders[0]), rows)
    return Resolution("OPEN", None, rows)


# --- explicit input families ------------------------------------------------------------------


@dataclass(frozen=True)
class Family:
    field: str  # DueDiligenceRequest field
    record: str  # the fid family of its record IDs
    subject: str
    group: DisagreementGroup
    key: Callable[[Any], Key]  # None-free semantic key; never a monetary value
    value: Callable[[Any], Decimal | None]  # shown in a disagreement
    period: Callable[[Any], str]
    basis: Callable[[Any], str] = lambda _: "NOT_APPLICABLE"
    describe: Callable[[Any], str] = lambda _: ""


def _line(item: BaseModel) -> Key:
    """A separate line item: identified by its full content (only exact duplicates merge)."""
    return ("#LINE", canonical(item))


def _contingent_key(c: Any) -> Key:
    if c.category.value == "TOTAL":  # a note states one total per date and basis
        return (c.as_of, c.basis, "TOTAL", c.currency.value)
    if c.description:
        return (c.as_of, c.basis, c.category.value, c.currency.value, norm(c.description))
    return _line(c)


FAMILIES: tuple[Family, ...] = (
    Family("share_counts", "shares", "SHARE_COUNT", G.SHAREHOLDING,
           lambda r: (r.as_of, r.bonus_split_adjusted), lambda r: r.shares,
           lambda r: str(r.as_of),
           describe=lambda r: "bonus/split adjusted" if r.bonus_split_adjusted else "unadjusted"),
    Family("retained_earnings", "retained_earnings", "RETAINED_EARNINGS", G.FINANCIAL,
           lambda r: (r.as_of, r.basis, r.currency.value), lambda r: r.value_crore,
           lambda r: str(r.as_of), lambda r: r.basis),
    Family("pledges", "pledge", "PROMOTER_PLEDGE_PCT", G.SHAREHOLDING,
           lambda r: (r.period_end,), lambda r: r.pledged_pct_of_promoter_holding,
           lambda r: str(r.period_end)),
    Family("holdings", "holding", "HOLDING_PCT", G.SHAREHOLDING,
           lambda r: (r.period_end, r.category.value, norm(r.holder)), lambda r: r.pct,
           lambda r: str(r.period_end),
           describe=lambda r: r.category.value + (f" {r.holder}" if r.holder else "")),
    Family("shareholder_counts", "shareholders", "SHAREHOLDER_COUNT", G.SHAREHOLDING,
           lambda r: (r.period_end,), lambda r: Decimal(r.count), lambda r: str(r.period_end)),
    Family("preferential_issues", "preferential", "PREFERENTIAL_ISSUE", G.GOVERNANCE, _line,
           lambda r: r.issue_price, lambda r: str(r.issue_date)),
    Family("related_party", "related_party", "RELATED_PARTY", G.GOVERNANCE,
           lambda r: (r.fiscal_year, r.basis, r.kind.value, norm(r.counterparty),
                      r.currency.value),
           lambda r: r.amount_crore, lambda r: f"FY{r.fiscal_year}", lambda r: r.basis,
           lambda r: f"{r.kind.value} {r.counterparty} ({r.relation.value})"),
    Family("auditor_reports", "auditor_report", "AUDITOR_REPORT", G.GOVERNANCE,
           lambda r: (r.fiscal_year, r.basis), lambda r: None, lambda r: f"FY{r.fiscal_year}",
           lambda r: r.basis, lambda r: f"{r.opinion.value} opinion"),
    Family("auditor_changes", "auditor_change", "AUDITOR_CHANGE", G.GOVERNANCE,
           lambda r: (r.change_date, norm(r.outgoing), norm(r.incoming)), lambda r: None,
           lambda r: str(r.change_date), describe=lambda r: r.reason),
    Family("management_changes", "management_change", "MANAGEMENT_CHANGE", G.GOVERNANCE,
           lambda r: (r.change_date, r.role.value, r.kind, norm(r.person)), lambda r: None,
           lambda r: str(r.change_date),
           describe=lambda r: f"{r.role.value} {r.kind} planned={r.planned_succession}"),
    Family("contingent_liabilities", "contingent", "CONTINGENT_LIABILITY", G.FINANCIAL,
           _contingent_key, lambda r: r.amount_crore, lambda r: str(r.as_of),
           lambda r: r.basis, lambda r: f"{r.category.value} {r.description or ''}".strip()),
    Family("cwip_ageing", "cwip", "CWIP_AGEING", G.FINANCIAL,
           lambda r: (r.as_of, r.basis, r.bucket.value, r.status, r.currency.value),
           lambda r: r.amount_crore, lambda r: str(r.as_of), lambda r: r.basis,
           lambda r: f"{r.bucket.value} {r.status}"),
    Family("ratings", "rating", "CREDIT_RATING", G.RATING,
           lambda r: (r.action_date, norm(r.agency), norm(r.instrument)), lambda r: None,
           lambda r: str(r.action_date),
           describe=lambda r: f"{r.agency} {r.instrument}: {r.rating} ({r.outlook.value})"),
    Family("agency_adjusted_debt", "agency_debt", "AGENCY_ADJUSTED_DEBT", G.FINANCIAL,
           lambda r: (r.as_of, norm(r.agency), norm(r.definition), r.currency.value),
           lambda r: r.value_crore, lambda r: str(r.as_of), describe=lambda r: r.agency),
    Family("debt_maturities", "maturity", "DEBT_MATURITY", G.FINANCIAL,
           lambda r: (r.as_of, r.basis, r.bucket, r.currency.value), lambda r: r.amount_crore,
           lambda r: str(r.as_of), lambda r: r.basis, lambda r: r.bucket),
    Family("executive_pay", "executive_pay", "EXECUTIVE_PAY", G.GOVERNANCE,
           lambda r: (r.fiscal_year, norm(r.scope), r.currency.value), lambda r: r.amount_crore,
           lambda r: f"FY{r.fiscal_year}", describe=lambda r: r.scope),
    Family("board", "board", "BOARD", G.GOVERNANCE, lambda r: (r.as_of,),
           lambda r: Decimal(r.independent_directors), lambda r: str(r.as_of),
           describe=lambda r: f"{r.independent_directors} of {r.directors} independent"),
    Family("penalties", "penalty", "PENALTY", G.GOVERNANCE, _line,
           lambda r: r.amount_inr, lambda r: str(r.penalty_date)),
    Family("complaints", "complaints", "SHAREHOLDER_COMPLAINTS", G.GOVERNANCE,
           lambda r: (r.fiscal_year,), lambda r: Decimal(r.received),
           lambda r: f"FY{r.fiscal_year}"),
    Family("working_capital", "working_capital", "WORKING_CAPITAL_DISCLOSURE", G.FINANCIAL,
           lambda r: (r.as_of, r.basis, r.item.value, r.currency.value),
           lambda r: r.amount_crore, lambda r: str(r.as_of), lambda r: r.basis,
           lambda r: r.item.value),
    Family("order_book_reports", "order_book", "ORDER_BOOK", G.ORDER_BOOK,
           lambda r: (r.as_of, r.basis, r.currency.value), lambda r: r.value_crore,
           lambda r: str(r.as_of), lambda r: r.basis,
           lambda r: f"{r.definition} ({r.tax_basis})"),
    Family("reconciliation_explanations", "explanation", "RECONCILIATION_EXPLANATION",
           G.FINANCIAL, lambda r: (r.metric, r.fiscal_year), lambda r: None,
           lambda r: f"FY{r.fiscal_year}", describe=lambda r: f"{r.metric} {r.kind}"),
    Family("reconciliation_reviews", "review", "RECONCILIATION_REVIEW", G.FINANCIAL,
           lambda r: (r.fiscal_year,), lambda r: Decimal(len(r.sources_checked)),
           lambda r: f"FY{r.fiscal_year}"),
    Family("event_assessments", "event_assessment", "EVENT_DIRECTION", G.EVENT,
           lambda r: (r.event_id,), lambda r: None, lambda r: str(r.event_id),
           describe=lambda r: r.direction.value),
)
FAMILY = {f.field: f for f in FAMILIES}


@dataclass(frozen=True)
class InputConflict:
    """Records of one semantic key that state different content."""

    family: Family
    key: Key
    resolution: Resolution

    @property
    def open(self) -> bool:
        return self.resolution.status == "OPEN"

    @property
    def records(self) -> tuple[Any, ...]:
        return tuple(c.item for c in self.resolution.candidates)

    def record_ids(self) -> tuple[UUID, ...]:
        return tuple(sorted({fid(self.family.record, r) for r in self.records}, key=str))

    @property
    def first(self) -> Any:
        return self.resolution.candidates[0].item


@dataclass
class NormalizedInputs:
    echo: DueDiligenceRequest  # sorted, exact duplicates removed: echoed and hashed
    request: DueDiligenceRequest  # one record per key; OPEN keys withheld
    conflicts: list[InputConflict] = field(default_factory=list)
    duplicates: dict[str, int] = field(default_factory=dict)  # exact duplicates removed

    def open(self, family: str) -> list[InputConflict]:
        return [c for c in self.conflicts if c.family.field == family and c.open]

    def open_where(self, family: str, keep: Callable[[Any], bool]) -> list[InputConflict]:
        return [c for c in self.open(family) if any(keep(r) for r in c.records)]


def _rank(item: Any) -> int:
    source = getattr(item, "source", None)
    return AUTHORITY_RANK[source.authority] if source is not None else ANALYST_RANK


def normalize(request: DueDiligenceRequest) -> NormalizedInputs:
    """O(n log n) per family: an index by key, never a pairwise comparison."""
    echo_update: dict[str, tuple[Any, ...]] = {}
    resolved_update: dict[str, tuple[Any, ...]] = {}
    conflicts: list[InputConflict] = []
    duplicates: dict[str, int] = {}
    for family in FAMILIES:
        items: Sequence[Any] = getattr(request, family.field)
        exact: dict[str, Any] = {}
        for item in items:
            text = canonical(item)
            kept = exact.get(text)
            if kept is None or item.model_dump_json() < kept.model_dump_json():
                exact[text] = item
        if len(exact) != len(items):
            duplicates[family.field] = len(items) - len(exact)
        unique = [exact[k] for k in sorted(exact)]
        echo_update[family.field] = tuple(unique)
        groups: dict[str, list[Candidate]] = {}
        for text, item in zip(sorted(exact), unique, strict=True):
            key = json.dumps(_plain(list(family.key(item))), sort_keys=True)
            groups.setdefault(key, []).append(Candidate(
                _rank(item), canonical(item, exclude=frozenset({"source"})), text, item))
        kept_items: list[Any] = []
        for key_text in sorted(groups):
            resolution = resolve(groups[key_text])
            if resolution.status in ("RESOLVED", "OPEN"):
                conflicts.append(InputConflict(family, tuple(json.loads(key_text)), resolution))
            if resolution.winner is not None:
                kept_items.append(resolution.winner.item)
        resolved_update[family.field] = tuple(sorted(kept_items, key=canonical))
    return NormalizedInputs(
        echo=request.model_copy(update=echo_update),
        request=request.model_copy(update=resolved_update),
        conflicts=conflicts, duplicates=duplicates)


# --- upstream record identity ---------------------------------------------------------------


def unique_by_id[T: BaseModel](items: Sequence[T], attr: str, what: str) -> tuple[T, ...]:
    """Records that carry their own ID: the same ID twice with identical content is one record;
    the same ID with different content is an integrity conflict (never an arbitrary pick)."""
    seen: dict[object, T] = {}
    out: list[T] = []
    for item in items:
        key = getattr(item, attr)
        prior = seen.get(key)
        if prior is None:
            seen[key] = item
            out.append(item)
        elif canonical(prior) != canonical(item):
            raise DueDiligenceInputError(
                "RECORD_ID_CONFLICT", f"{what} {key} appears twice with different content")
    return tuple(out)


# --- rule plumbing ----------------------------------------------------------------------------


def conflicted(ctx: Ctx, family: str, keep: Callable[[Any], bool] = lambda _: True) -> list[
        InputConflict]:
    """OPEN conflicts of a family with at least one record a rule would read."""
    return ctx.inputs.open_where(family, keep) if ctx.inputs is not None else []


def conflict_ev(ctx: Ctx, basis: SignalBasis, conflicts: Sequence[InputConflict],
                trace: Trace | None = None, what: str = "") -> Ev:
    """UNAVAILABLE (INPUT_CONFLICT), citing every disagreeing record: never a graded value."""
    trace = trace or Trace()
    subjects: list[str] = []
    for c in conflicts:
        f = c.family
        subjects.append(f"{f.subject} {f.period(c.first)}")
        for r in c.records:
            ctx.explicit(trace, f"conflicting {f.subject} {f.period(r)} {f.describe(r)}".strip(),
                         f.value(r), "VALUE", fid(f.record, r), r.source,
                         basis=basis_of(f.basis(r)))
    listed = ", ".join(sorted(set(subjects)))
    return unavailable(basis, "INPUT_CONFLICT", trace=trace,
                       explanation="sources disagree for the same fact"
                       + (f" ({listed[:600]})" if listed else "")
                       + f"{what}: no value is selected and no severity is graded")
