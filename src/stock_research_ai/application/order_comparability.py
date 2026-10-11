"""Box 12 comparability contract for every order-book calculation.

Two economic quantities are combined (added, subtracted, divided) only when they agree on
every dimension the calculation names; otherwise the result is UNAVAILABLE with the reasons,
never arithmetic. Box 12 has no FX engine and no tax or basis conversion, so a mismatch is
never converted away.

Dimensions:
  CURRENCY  identical currency code
  BASIS     identical, known financial basis (CONSOLIDATED or STANDALONE); a disclosed order
            carries no statement basis (NOT_APPLICABLE) and is neutral; UNKNOWN / BOTH never
            match anything
  TAX       one known tax basis across all quantities. UNKNOWN never matches a known basis.
            UNKNOWN matches UNKNOWN only between disclosed order values (`unknown_matches_
            unknown`: arithmetic within one order chain - the same filer about the same
            contract - or a sum reported as 'disclosed orders only'), and the match is
            flagged; never against revenue or a reported order book. The caller's explicit
            `allow_tax_inclusive_order_values` policy lets a mismatch through, flagged
  PERIOD    an explicit rule per calculation (`same_period` / `as_of_equals`): never the
            closest period
  UNIT      identical unit / scale
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from stock_research_ai.domain.market_intelligence import FinancialBasis, TaxBasis


class Dimension(StrEnum):
    CURRENCY = "CURRENCY"
    BASIS = "BASIS"
    TAX = "TAX"
    UNIT = "UNIT"


ALL = (Dimension.CURRENCY, Dimension.BASIS, Dimension.TAX, Dimension.UNIT)
#: Arithmetic between two disclosed order values (no statement basis is involved).
ORDER_VALUES = (Dimension.CURRENCY, Dimension.TAX, Dimension.UNIT)

TAX_POLICY_NOTE = "TAX_INCLUSIVE_OR_UNKNOWN_VALUES_PERMITTED_BY_POLICY"
TAX_UNKNOWN_NOTE = "TAX_BASIS_UNKNOWN_ON_BOTH_SIDES"
_STATEMENT_BASES = frozenset({FinancialBasis.CONSOLIDATED, FinancialBasis.STANDALONE})


@dataclass(frozen=True)
class Quantity:
    """One economic quantity with everything needed to decide comparability."""

    label: str
    value: Decimal | None
    currency: str | None
    tax: TaxBasis
    basis: FinancialBasis = FinancialBasis.NOT_APPLICABLE
    unit: str = "CRORE"


@dataclass(frozen=True)
class Comparability:
    reasons: tuple[str, ...]  # empty = comparable
    notes: tuple[str, ...] = ()  # flags carried onto an available result

    @property
    def ok(self) -> bool:
        return not self.reasons

    @property
    def reason(self) -> str | None:
        return self.reasons[0] if self.reasons else None


def check_comparable(
    quantities: Sequence[Quantity],
    dimensions: Sequence[Dimension] = ALL,
    *,
    allow_tax_policy: bool = False,
    unknown_matches_unknown: bool = False,
) -> Comparability:
    """Every quantity against every other on the named dimensions. Reasons are stable codes
    in dimension order; a quantity without a value is reported as VALUE_NOT_DISCLOSED."""
    reasons: list[str] = []
    notes: list[str] = []
    if any(q.value is None for q in quantities):
        reasons.append("VALUE_NOT_DISCLOSED")
    if len(quantities) < 2:  # nothing to compare against (an empty or one-term sum)
        return Comparability(reasons=tuple(reasons))
    if Dimension.CURRENCY in dimensions:
        currencies = {q.currency for q in quantities}
        if None in currencies:
            reasons.append("CURRENCY_NOT_STATED")
        elif len(currencies) > 1:
            reasons.append("CURRENCY_MISMATCH")
    if Dimension.BASIS in dimensions:
        stated = {q.basis for q in quantities if q.basis != FinancialBasis.NOT_APPLICABLE}
        if stated - _STATEMENT_BASES:
            reasons.append("BASIS_UNKNOWN")
        elif len(stated) > 1:
            reasons.append("BASIS_MISMATCH")
    if Dimension.TAX in dimensions:
        taxes = {q.tax for q in quantities}
        if len(taxes) == 1 and TaxBasis.UNKNOWN not in taxes:
            pass
        elif taxes == {TaxBasis.UNKNOWN} and unknown_matches_unknown:
            notes.append(TAX_UNKNOWN_NOTE)
        elif allow_tax_policy:
            notes.append(TAX_POLICY_NOTE)
        else:
            reasons.append("TAX_BASIS_NOT_COMPARABLE")
    if Dimension.UNIT in dimensions and len({q.unit for q in quantities}) > 1:
        reasons.append("UNIT_MISMATCH")
    return Comparability(reasons=tuple(reasons), notes=tuple(notes))


def same_period(a: tuple[date, date], b: tuple[date, date]) -> str | None:
    """Flow quantities (inflow, revenue) combine only over the identical start and end."""
    return None if a == b else "PERIOD_MISMATCH"


def as_of_equals(as_of: date, required: date) -> str | None:
    """A stock quantity (an order book) pairs only with the exact required date."""
    return None if as_of == required else "PERIOD_MISMATCH"
