"""Operating KPIs management states on calls, as structured, cited evidence.

A transcript number becomes a KPI only when its meaning is explicit in the sentence: a
registered metric phrase (DSO, deal wins / TCV, free cash flow and its conversion, EBIT margin,
constant-currency growth, headcount) followed closely by a number WITH its unit. Nothing is
inferred: a missing unit means no KPI; a missing currency is stated as not stated; the period is
the period words the sentence itself uses ('for the quarter', 'Q2', 'FY26'), else 'not stated'.
Only MANAGEMENT statements count (never an analyst's question), and forward-looking statements
are left to Box 8's guidance tracker. Each KPI keeps its statement - speaker, call, page,
evidence IDs - as provenance; it is a management statement, not a reconciled figure.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from stock_research_ai.application.semantic_guards import mask

_NUMBER = r"(-?\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)"
_MONEY = (r"(?P<currency>US\s?\$|USD\s?|\$|₹\s?|Rs\.?\s?|INR\s?)?" + _NUMBER.replace(
    "(", "(?P<value>", 1) + r"\s*(?P<scale>million|billion|mn|bn|crores?|cr)\b")
PERIOD = re.compile(
    r"\b(?:Q[1-4](?:\s?FY\s?'?\d{2,4})?|FY\s?'?\d{2,4}|H[12](?:\s?FY\s?'?\d{2,4})?|"
    r"(?:for|in|during|over) the (?:quarter|year|half|full year|first half|fiscal year)|"
    r"this quarter|this year|last quarter|full[- ]year|last[- ]12[- ]months?|LTM|"
    r"trailing (?:12|twelve) months)\b", re.I)
#: (metric, reader label, pattern with a 'value' group, unit when the pattern fixes it)
PATTERNS: tuple[tuple[str, str, re.Pattern[str], str | None], ...] = (
    ("DSO", "Days sales outstanding (DSO)", re.compile(
        r"\bDSOs?\b[^.%]{0,60}?\b(?P<value>\d{2,3}(?:\.\d+)?)\s*days\b", re.I), "DAYS"),
    ("DEAL_WINS_TCV", "Deal wins (TCV)", re.compile(
        r"\b(?:TCV|deal wins?|new deal (?:wins|TCV)|net new (?:deal )?(?:TCV|wins))\b"
        r"[^.]{0,60}?" + _MONEY, re.I), None),
    ("FREE_CASH_FLOW", "Free cash flow", re.compile(
        r"\bfree cash flows?\b[^.]{0,60}?" + _MONEY, re.I), None),
    ("FCF_TO_PAT", "Free cash flow / PAT (cash conversion)", re.compile(
        r"\bfree cash flows?\b[^.]{0,120}?\b(?P<value>\d{2,3}(?:\.\d+)?)\s*%\s+of\s+(?:our\s+)?"
        r"(?:reported\s+)?(?:PAT|net profit|profit after tax)\b", re.I), "PERCENT"),
    ("EBIT_MARGIN", "EBIT margin (company-reported)", re.compile(
        r"\bEBIT\s+(?:margins?|percent(?:age)?)\b[^.%]{0,40}?\b(?P<value>\d{1,2}(?:\.\d+)?)\s*%",
        re.I), "PERCENT"),
    ("CC_REVENUE_GROWTH", "Revenue growth in constant currency", re.compile(
        r"\bconstant[- ]currency\b[^.%]{0,60}?(?P<value>-?\d{1,2}(?:\.\d+)?)\s*%|"
        r"(?P<value2>-?\d{1,2}(?:\.\d+)?)\s*%[^.%]{0,30}?\bin constant[- ]currency\b", re.I),
     "PERCENT"),
    ("HEADCOUNT", "Headcount", re.compile(
        r"\bheadcount\b[^.]{0,40}?\b(?P<value>\d{1,3}(?:,\d{3})+|\d{4,7})\b", re.I), "COUNT"),
)
#: a bound or an approximation, not a stated figure ('over $1 billion', 'close to 48%')
QUALIFIED = re.compile(r"\b(?:over|above|more than|at least|exceed\w*|crossed|surpass\w*|"
                       r"nearly|close to|around|about|approximately|almost|upwards of|under|"
                       r"below|less than)\s*$", re.I)
_SCALES = {"million": "MILLION", "mn": "MILLION", "billion": "BILLION", "bn": "BILLION",
           "crore": "CRORE", "crores": "CRORE", "cr": "CRORE"}


@dataclass(frozen=True)
class StatedKpi:
    metric: str
    label: str
    value: Decimal
    unit: str  # DAYS / PERCENT / COUNT / USD MILLION / INR CRORE / 'MILLION (currency ...)'
    period_text: str | None  # the sentence's own period words, verbatim; None = not stated
    statement: Any  # the Box 8 ManagementStatement (speaker, call, page, evidence)


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _money_unit(m: re.Match[str]) -> str:
    currency = (m.group("currency") or "").replace(" ", "").upper()
    scale = _SCALES[m.group("scale").lower()]
    code = {"US$": "USD", "USD": "USD", "$": "USD", "₹": "INR", "RS.": "INR", "RS": "INR",
            "INR": "INR"}.get(currency)
    return f"{code} {scale}" if code else f"{scale} (currency not stated)"


def _normalized(k: StatedKpi) -> tuple[Decimal, str]:
    """A value in its unit family's base (billions as millions) for duplicate detection."""
    scale = next((s for s in ("BILLION", "MILLION", "CRORE") if s in k.unit), k.unit)
    value = k.value * 1000 if scale == "BILLION" else k.value
    family = {"BILLION": "MILLION"}.get(scale, scale)  # the currency is not part of the key
    return value, family


def stated_kpis(box8: Any) -> list[StatedKpi]:
    """Every explicit KPI in a MANAGEMENT, not forward-looking statement (one per metric per
    statement, the first match)."""
    if box8 is None:
        return []
    out: list[StatedKpi] = []
    seen: dict[tuple[Any, str, Decimal, str], int] = {}
    for s in box8.statements:
        if s.speaker_role.value != "MANAGEMENT" or s.forward_looking:
            continue
        meaning = mask(s.quote)
        period = PERIOD.search(meaning)
        for metric, label, pattern, unit in PATTERNS:
            m = pattern.search(meaning)
            if m is None:
                continue
            group = "value" if m.groupdict().get("value") else "value2"
            raw = m.groupdict().get(group)
            value = _decimal(raw) if raw else None
            start = m.start("currency") if "currency" in m.groupdict() and m.group(
                "currency") else m.start(group)
            if value is None or QUALIFIED.search(meaning[max(0, start - 30):start]):
                continue
            kpi = StatedKpi(metric, label, value, unit or _money_unit(m),
                            s.quote[period.start():period.end()] if period else None, s)
            # one figure restated in a call ('US$1.023 billion' ... '1,023 million'): kept
            # once, preferring the statement that names its currency
            key = (s.event_id, metric, *_normalized(kpi))
            if key not in seen:
                seen[key] = len(out)
                out.append(kpi)
            elif "not stated" in out[seen[key]].unit and "not stated" not in kpi.unit:
                out[seen[key]] = kpi
    return out


__all__ = ["PATTERNS", "StatedKpi", "stated_kpis"]
