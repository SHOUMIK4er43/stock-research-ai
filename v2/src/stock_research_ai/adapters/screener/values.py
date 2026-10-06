"""Conservative source number and period interpretation; never infer fiscal calendars."""

import re
from datetime import date
from decimal import Decimal

from stock_research_ai.domain.financial import (
    DateResolution,
    FinancialPeriod,
    FinancialUnit,
    ParsedValue,
    PeriodFrequency,
    PeriodKind,
    UnitKind,
    ValueStatus,
)

UNKNOWN = FinancialUnit(kind=UnitKind.UNKNOWN)


def parse_value(text: str, unit: FinancialUnit = UNKNOWN) -> ParsedValue:
    value = " ".join(text.split()).strip()
    missing = {
        "": ValueStatus.BLANK,
        "-": ValueStatus.NOT_REPORTED,
        "--": ValueStatus.NOT_REPORTED,
        "—": ValueStatus.NOT_REPORTED,
        "not reported": ValueStatus.NOT_REPORTED,
        "not applicable": ValueStatus.NOT_APPLICABLE,
        "unavailable": ValueStatus.UNAVAILABLE,
    }
    status = missing.get(value.lower())
    if status is not None:
        return ParsedValue(
            original=text, value=None, status=status, unit=unit, reason=value or "blank"
        )
    if "%" in value:
        unit = FinancialUnit(kind=UnitKind.PERCENT)
    elif "₹" in value or re.search(r"\bRs\.?", value):
        unit = FinancialUnit(kind=UnitKind.MONEY, currency="INR")
    if re.search(r"\b(?:Cr\.?|Crores?)\b", value, re.I):
        unit = FinancialUnit(kind=unit.kind, currency=unit.currency, scale=Decimal("10000000"))
    number = re.sub(r"₹|\bRs\.?|%|\b(?:Cr\.?|Crores?)\b\.?", "", value, flags=re.I).strip()
    number = number.replace("−", "-")
    if number.startswith("(") and number.endswith(")"):
        number = "-" + number[1:-1]
    # Validate grouping before removing commas (both Indian and international).
    valid = re.fullmatch(
        r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d+)?", number
    )
    if not valid:
        return ParsedValue(
            original=text,
            value=None,
            status=ValueStatus.PARSE_ERROR,
            unit=unit,
            reason="Unrecognized numeric representation",
        )
    return ParsedValue(
        original=text,
        value=Decimal(number.replace(",", "")),
        status=ValueStatus.AVAILABLE,
        unit=unit,
    )


def parse_period(
    label: str, date_key: str | None, frequency: PeriodFrequency, instant: bool = False
) -> FinancialPeriod:
    end = None
    if date_key:
        try:
            end = date.fromisoformat(date_key)
        except ValueError:
            pass
    if label.strip().upper() == "TTM":
        frequency = PeriodFrequency.TTM
    # Month labels do not establish a day or a fiscal calendar. Explicit data-date-key
    # supplies the end only; even annual/quarterly duration starts remain unknown.
    return FinancialPeriod(
        kind=PeriodKind.INSTANT if instant else PeriodKind.DURATION,
        fiscal_label=label or "Unlabelled period",
        frequency=frequency,
        original_date_key=date_key,
        end_date=end,
        date_resolution=(
            DateResolution.EXACT
            if instant and end
            else DateResolution.PARTIAL
            if end or re.fullmatch(r"[A-Za-z]{3} \d{4}", label)
            else DateResolution.UNRESOLVED
        ),
    )
