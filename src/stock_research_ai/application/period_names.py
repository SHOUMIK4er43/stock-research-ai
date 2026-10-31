"""One reader name per period, shared by every output (Summary, Detailed Research, PDF).

Screener prints 'Mar 2026' for a fiscal year, the March quarter and a balance-sheet date
alike; a filing header can come through garbled. Every period is named from its type, its
end date and the company's fiscal-year end instead: FY2026, Q2 FY2027, H1 FY2027, 9M FY2027,
TTM to 30-Sep-2026, or a balance sheet at 31-Mar-2026.
"""

from datetime import date
from types import SimpleNamespace
from typing import Any

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def day_name(day: date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def period_name(period: Any, fy_end_month: int | None) -> str:
    """One unambiguous reader name per period, whatever the source printed ('Mar 2026',
    'year ended March 31 2026 March 31 2026'): FY2026 (the fiscal year named by the year it
    ends), Q2 FY2027, H1 FY2027, 9M FY2027, TTM to 30-Sep-2026, or a balance-sheet date
    31-Mar-2026. Without an end date the source label is kept as it is."""
    end, kind = getattr(period, "period_end", None), period.period_type.value
    label = str(period.label)
    if end is None:
        return label
    if kind == "POINT_IN_TIME":
        return day_name(end)
    if kind == "TTM":
        return f"TTM to {day_name(end)}"
    month = fy_end_month or 3
    fiscal_year = end.year if end.month <= month else end.year + 1
    if kind == "FY":
        return f"FY{fiscal_year}" if end.month == month else f"year ended {day_name(end)}"
    elapsed = (end.month - month - 1) % 12 + 1  # months of the fiscal year to the end date
    if elapsed % 3:
        return f"period ended {day_name(end)}"
    if kind == "QUARTER":
        return f"Q{elapsed // 3} FY{fiscal_year}"
    prefix = {"HALF_YEAR": "H", "NINE_MONTH": "9M"}.get(kind)
    if prefix == "H" and elapsed in (6, 12):
        return f"H{elapsed // 6} FY{fiscal_year}"
    if prefix == "9M" and elapsed == 9:
        return f"9M FY{fiscal_year}"
    return f"{label} (to {day_name(end)})"


SUMMARY_KINDS = {"annual": "FY", "cash_flow": "FY", "quarterly": "QUARTER",
                 "balance_sheet": "POINT_IN_TIME"}


def summary_period_name(block: str, label: str, end: date | None,
                        fy_end_month: int | None, *, ttm_end: date | None = None) -> str:
    """The Summary's name for one Screener column, from its block and end date: a fiscal
    year, a quarter, a balance-sheet date or a TTM to its last quarter. Without a known
    fiscal-year end a quarter is named by its end date, never guessed into a fiscal year."""
    if label.strip().upper() == "TTM":
        return f"TTM to {day_name(ttm_end)}" if ttm_end else "TTM (end date not stated)"
    kind = SUMMARY_KINDS.get(block)
    if end is None or kind is None:
        return label
    if kind == "POINT_IN_TIME":
        return f"balance sheet at {day_name(end)}"
    if fy_end_month is None:
        return (f"year ended {day_name(end)}" if kind == "FY"
                else f"quarter ended {day_name(end)}")
    return period_name(SimpleNamespace(period_type=SimpleNamespace(value=kind),
                                       period_end=end, label=label), fy_end_month)


__all__ = ["day_name", "period_name", "summary_period_name"]
