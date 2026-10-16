"""V1 business-model applicability, shared by extraction and financial reconciliation.

Specific structured industry labels are evidence of a business model. Statement
signatures corroborate them or reveal conflicts; one financing/deposit row is never
proof of a banking licence. Unresolved financial businesses use restricted analytics.
This is deliberately not a general sector taxonomy or a regulatory classification.
"""

import re
from collections.abc import Sequence

from stock_research_ai.domain.financial_canonical import Industry

CLASSIFICATION_VERSION = "financial-business/1"
FINANCIAL_INDUSTRIES = frozenset({Industry.BANK, Industry.NBFC, Industry.INSURANCE})
RESTRICTED_INDUSTRIES = FINANCIAL_INDUSTRIES | {Industry.UNKNOWN}


def _key(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


_NAMES = {
    Industry.BANK: {
        "banks", "banking", "commercial bank", "commercial banks", "private sector bank",
        "private sector banks", "public sector bank", "public sector banks", "banks private",
        "banks public", "small finance banks", "small finance bank",
    },
    Industry.NBFC: {
        "nbfc", "nbfcs", "non banking financial company", "non banking financial companies",
        "non banking finance company", "non banking finance companies", "housing finance",
        "housing finance company", "housing finance companies", "hfc", "hfcs",
        "finance housing", "finance nbfc", "finance investment and credit companies",
    },
    Industry.INSURANCE: {
        "insurance", "life insurance", "general insurance", "non life insurance",
        "insurance life", "insurance non life", "insurance general", "insurance companies",
        "life insurer", "general insurer", "reinsurance",
    },
}
_FINANCIAL = re.compile(
    r"\b(?:bank\w*|nbfcs?|hfcs?|financ\w*|insur\w*|reinsurance|lending|loans?|"
    r"microfinance|mortgage|investment|investments|asset management|stock brok\w*|"
    r"holding compan\w*|capital markets|wealth management|mutual funds?|fintech|credit)\b"
)
_FINANCIAL_ROWS = {
    "financing profit", "financing income", "interest income", "interest earned",
    "net interest income", "deposits", "advances", "loan book", "aum",
    "assets under management", "nim", "net interest margin", "gnpa", "nnpa",
    "gross npa", "net npa", "gross npa percent", "net npa percent",
    "gross npa ratio", "net npa ratio", "solvency ratio", "premium income",
    "gross written premium", "net premium", "net premiums", "claims", "claims incurred",
    "loans", "loans and advances",
    "gross premium", "gross premiums", "net premium income", "net premiums earned",
}
_PREMIUM = {"premium income", "gross written premium", "net premium", "net premiums",
            "gross premium", "gross premiums", "net premium income", "net premiums earned"}
_CLAIMS = {"claims", "claims incurred", "benefits paid", "solvency ratio"}


def has_business_evidence(
    classifications: Sequence[str], description: str, labels: Sequence[str]
) -> bool:
    """A price/EPS/book-value-only snapshot carries no business-model evidence.

    Older explicit caller-declared calculations may still use such minimal snapshots;
    any financial hint, even ambiguous, must instead impose the acquired restriction.
    """
    return bool(classifications or description.strip() or {_key(n) for n in labels} & (
        _FINANCIAL_ROWS | {"sales", "revenue", "operating profit", "ebitda", "inventory"}
    ))


def classify_business(
    classifications: Sequence[str] = (), *, description: str = "", labels: Sequence[str] = ()
) -> Industry:
    """Exact structured labels, corroborated descriptions, then conservative signatures.

    Descriptions alone cannot grant sector-specific treatment. In particular, software
    suppliers mentioning banking customers are not banks. Contradictory evidence never
    chooses a winner. Financing profit / ordinary deposits do not distinguish an NBFC
    (including a deposit-taking HFC) from a bank.
    """
    names = {_key(n) for n in classifications}
    rows = {_key(n) for n in labels}
    text = _key(description)
    found = {industry for industry, aliases in _NAMES.items() if names & aliases}
    insurance_structure = bool(rows & _PREMIUM and rows & _CLAIMS)
    if insurance_structure:
        found.add(Industry.INSURANCE)
    # Specific self-descriptions require financial statement corroboration. An explicit
    # non-bank description also prevents "banking" within that phrase implying BANK.
    nbfc = bool(re.search(r"\b(?:non banking financ\w*|housing finance|nbfc|hfc)\b", text))
    if nbfc and rows & _FINANCIAL_ROWS:
        found.add(Industry.NBFC)
    if (not nbfc and re.search(r"\b(?:commercial bank|banking business|banking services)\b", text)
            and "deposits" in rows and rows & {"advances", "interest income", "interest earned"}):
        found.add(Industry.BANK)
    if re.search(r"\b(?:insurer|insurance company|life insurance|general insurance)\b", text):
        if rows & _PREMIUM:
            found.add(Industry.INSURANCE)
    if len(found) > 1:
        return Industry.UNKNOWN
    if found:
        result = next(iter(found))
        if result != Industry.INSURANCE and rows & _PREMIUM:
            return Industry.UNKNOWN
        return result
    if any(_FINANCIAL.search(n) for n in names) or rows & _FINANCIAL_ROWS:
        return Industry.UNKNOWN
    # An industrial statement signature may corroborate a manufacturing/services label,
    # but an explicit financial self-description must never fall through to GENERAL.
    if (nbfc or re.search(r"\b(?:insurance company|insurer|financial services|"
                         r"financing business|lending business|investment company|finance company|"
                         r"engaged in (?:the business of )?(?:financing|lending)|"
                         r"(?:provides|providing|offers|offering) (?:loans|financing|credit)|"
                         r"(?:finance|financing|lending|banking|insurance) "
                         r"(?:activities|operations))\b",
                         text)):
        return Industry.UNKNOWN
    if rows & {"sales", "revenue", "revenue from operations"} and rows & {
        "operating profit", "ebitda", "inventory", "inventories", "cost of goods sold"
    }:
        return Industry.GENERAL
    if names & {"manufacturing", "information technology", "it software", "it services",
                "technology", "software", "services"}:
        return Industry.GENERAL
    return Industry.UNKNOWN


def reported_metric_applicable(industry: Industry, metric: str) -> bool:
    """Summary's reported facts: do not turn invalid ratios into grounded claims."""
    if metric in {"roce", "operating_profit", "source_free_cashflow"}:
        return industry == Industry.GENERAL
    if metric in {"gross_npa_percent", "net_npa_percent", "financing_profit", "deposits"}:
        return industry in {Industry.BANK, Industry.NBFC}
    return True


def declaration_compatible(industry: Industry | None, declaration: str) -> bool:
    """An older caller declaration cannot widen an acquired business restriction.

    Legacy snapshots without classification retain the explicit caller-evidence contract.
    Fresh snapshots always carry the acquired classification, including UNKNOWN.
    """
    if industry is None or declaration in {"UNKNOWN", "OTHER_FINANCIAL"}:
        return True
    return (industry == Industry.GENERAL and declaration == "NON_FINANCIAL") or (
        industry == Industry.BANK and declaration == "BANK"
    )
