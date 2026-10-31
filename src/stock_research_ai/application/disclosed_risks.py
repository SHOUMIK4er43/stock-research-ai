"""Management-disclosed risks and headwinds: what management itself says may hurt the business.

The registered Box 13 rules are not the only route into the Negatives view. A management
statement that explicitly discusses a material uncertainty (a client's decision, currency, a
large deal's ramp, pricing deflation from AI, wage increases ...) is listed - verbatim, cited,
labelled MANAGEMENT-DISCLOSED RISK / HEADWIND - and never graded: no severity is invented, and
a registered rule's RED / YELLOW stays the only grade.

A statement qualifies only when (1) a MANAGEMENT speaker said it (never an analyst's question,
a moderator or an unknown speaker), (2) it carries an explicit risk cue, and (3) one registered
category matches its meaning (keywords are read after semantic_guards.mask, so 'chip prices'
or 'technical debt' never qualify by accident).
"""

import re
from dataclasses import dataclass
from typing import Any

from stock_research_ai.application.semantic_guards import mask

#: an explicit statement of risk / adverse pressure (not tone, not a positive outlook)
RISK_CUE = re.compile(
    r"\b(?:risks?|headwinds?|uncertain\w*|pressure[sd]?|soft(?:ness|er|ening)?|weak(?:ness|er)?"
    r"|slow(?:down|er|ing)?|delay(?:s|ed)?|deferr\w*|decline[sd]?|cuts?|impact(?:ed)?"
    r"|challeng\w*|volatil\w*|cautious|caution|deflation\w*|cannibali\w*|ramp[- ]downs?)\b",
    re.I)
#: a denial just before the cue ('it won't have a margin headwind', 'we do not expect ...
#: furloughs') is not a disclosed risk
NEGATION = re.compile(r"\b(?:no|not|never|nor|without|unlikely|n't|won't|don't|do not|does not|"
                      r"did not|isn't|aren't|cannot)\b[^.;]{0,60}$", re.I)
#: (code, reader label, pattern) - each a business risk a company's management can disclose
CATEGORIES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("CLIENT_CONCENTRATION", "Client concentration / a large client's decision", re.compile(
        r"\b(?:top|largest|large|one|a|single|key|specific) (?:\d+ )?(?:clients?|customers?|"
        r"accounts?)\b|\bclient (?:concentration|decisions?|specific)\b", re.I)),
    ("CURRENCY", "Currency / FX", re.compile(
        r"\b(?:currenc(?:y|ies)|cross[- ]currency|fx|forex|exchange rates?|rupee|"
        r"dollar|euro|pound)\b", re.I)),
    ("LARGE_DEAL_EXECUTION", "Large-deal execution / transitions", re.compile(
        r"\b(?:large deals?|mega deals?|deal ramp\w*|ramp[- ]ups?|transition(?:s| costs?)?)\b",
        re.I)),
    ("MARGIN_PRESSURE", "Margin pressure", re.compile(
        r"\bmargins?\b.{0,60}\b(?:pressure|headwinds?|declin\w*|dilut\w*|compress\w*|"
        r"impact\w*|lower)\b|\b(?:pressure|headwinds?|dilut\w*|compress\w*)\b.{0,60}"
        r"\bmargins?\b", re.I)),
    ("AI_DEFLATION", "AI-driven pricing / effort deflation", re.compile(
        r"\b(?:ai|genai|automation|productivity)\b.{0,80}\b(?:deflation\w*|pass(?:ed|ing)? on|"
        r"cannibali\w*|pricing|fewer (?:people|hours|tickets)|lower revenue)\b|\b(?:deflation\w*|"
        r"cannibali\w*)\b.{0,80}\b(?:ai|genai|automation|productivity)\b", re.I)),
    ("WAGE_INFLATION", "Wage inflation", re.compile(
        r"\b(?:wage (?:hikes?|increases?|inflation)|salary (?:hikes?|increases?)|"
        r"compensation (?:increases?|revisions?))\b", re.I)),
    ("DISCRETIONARY_SPENDING", "Discretionary spending", re.compile(
        r"\bdiscretionary\b|\bclient (?:budgets?|spend(?:ing)?)\b|\bit spend(?:ing)?\b", re.I)),
    ("INSOURCING", "Client insourcing / captives", re.compile(
        r"\binsourc\w*|\bcaptives?\b|\bglobal capability cent(?:er|re)s?\b|\bgccs?\b", re.I)),
    ("SEASONALITY", "Seasonality / furloughs", re.compile(
        r"\bseasonal\w*|\bfurlough\w*|\bholiday season\b", re.I)),
    ("REGULATORY", "Regulatory / visa / tariff", re.compile(
        r"\bregulat\w*|\bvisas?\b|\bimmigration\b|\btariffs?\b|\bsanctions?\b", re.I)),
    ("GEOGRAPHIC", "Geographic exposure", re.compile(
        r"\b(?:europe|european|americas?|north america|apac|middle east|japan|germany|"
        r"france)\b|(?-i:\b(?:US|USA|UK)\b)|\bu\.s\.", re.I)),
)
MAX_PER_CATEGORY = 3  # the latest statements per category; the transcripts hold the rest


@dataclass(frozen=True)
class DisclosedRisk:
    category: str
    label: str
    statement: Any  # the Box 8 ManagementStatement


def disclosed_risks(box8: Any) -> list[DisclosedRisk]:
    """Management statements that disclose a registered risk category, latest first per
    category (bounded), one category per statement (the first registered match)."""
    if box8 is None:
        return []
    dates = {e.event_id: e.call_date for e in box8.events}
    found: dict[str, list[DisclosedRisk]] = {}
    for s in box8.statements:
        if s.speaker_role.value != "MANAGEMENT":
            continue
        meaning = mask(s.quote)
        if not any(not NEGATION.search(meaning[:cue.start()])
                   for cue in RISK_CUE.finditer(meaning)):
            continue  # no risk cue, or every cue is denied
        for code, label, pattern in CATEGORIES:
            if pattern.search(meaning):
                found.setdefault(code, []).append(DisclosedRisk(code, label, s))
                break
    out: list[DisclosedRisk] = []
    for code, _label, _pattern in CATEGORIES:
        items = sorted(found.get(code, ()), key=lambda d: (
            str(dates.get(d.statement.event_id) or ""), str(d.statement.statement_id)),
            reverse=True)
        out += items[:MAX_PER_CATEGORY]
    return out


__all__ = ["CATEGORIES", "DisclosedRisk", "disclosed_risks"]
