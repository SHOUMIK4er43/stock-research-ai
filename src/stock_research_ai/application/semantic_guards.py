"""Context guards for keyword classifiers: a keyword is not evidence of a financial concept.

Transcript classifiers (statement types, topics, industry demand / pricing signals) match
registered words. Some of those words also have a non-financial sense in ordinary business
language: 'technical debt' is not borrowing, 'chip prices are increasing' is an input cost and
not the company's pricing power, and 'deployed at Target Group' names a client, not a target.

`mask` blanks every such phrase - character for character, so positions are kept - before a
classifier reads a sentence. Quotes are never changed: only what a keyword is taken to mean.
"""

import re

#: phrases whose financial keyword is used in a non-financial sense
NON_FINANCIAL_SENSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # software / design debt is not borrowing
    ("TECHNICAL_DEBT", re.compile(r"\b(?:technical|tech|technology|design|code|architectural)"
                                  r"[- ]debts?\b", re.I)),
    # prices of inputs, commodities and securities are not the company's pricing
    ("INPUT_OR_MARKET_PRICE", re.compile(
        r"\b(?:chip|memory|semiconductor|component|hardware|commodit(?:y|ies)|raw[- ]material"
        r"|input|fuel|oil|crude|gas|steel|metal|cotton|coal|energy|power|electricity|freight"
        r"|share|stock|bond)s?(?:\s+(?:and|or|&)\s+\w+)?\s+pric(?:e|es|ing)\b", re.I)),
    # 'Target' as a proper noun (a client / company name) - capitalised mid-sentence or
    # followed by another capitalised word - and market targeting are not financial targets
    ("TARGET_AS_NAME", re.compile(r"(?<=[a-z,;:] )Target\b|\bTarget(?=\s+[A-Z][a-z])")),
    ("TARGET_MARKET", re.compile(
        r"\btarget(?:ed|ing)?\s+(?:audiences?|markets?|segments?|clients?|customers?"
        r"|accounts?|industr(?:y|ies)|verticals?|geograph(?:y|ies)|personas?)\b", re.I)),
)


def mask(text: str) -> str:
    """`text` with each non-financial-sense phrase blanked to underscores of equal length."""
    for _code, pattern in NON_FINANCIAL_SENSES:
        text = pattern.sub(lambda m: "_" * len(m.group(0)), text)
    return text


__all__ = ["NON_FINANCIAL_SENSES", "mask"]
