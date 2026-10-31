"""V1 RC1 (Tech Mahindra): expected group differences are not suspicious findings.

TECHM's consolidated revenue, profit, equity and cash exceed the parent's by a stable share
every year - the ordinary footprint of a group with subsidiaries - yet each difference was
'MATERIAL_NOT_REVIEWED' with its own diligence question. A stable, same-direction difference
is now STRUCTURAL_PATTERN (both bases kept); a reversal, a sign change or a material shift
(TECHM's gross debt: 51 % -> 31 %) stays flagged.
"""

from datetime import date
from decimal import Decimal as D
from uuid import uuid4

from stock_research_ai.application.due_diligence import _structural
from stock_research_ai.domain.due_diligence import BasisComparison, ReconciliationStatus

NR = ReconciliationStatus.MATERIAL_NOT_REVIEWED


def comp(metric: str, year: int, cons: str, stand: str) -> BasisComparison:
    cv, sv = D(cons), D(stand)
    return BasisComparison(
        comparison_id=uuid4(), metric=metric, period=f"FY{year}", period_end=date(year, 3, 31),
        standalone_value=sv, consolidated_value=cv, absolute_difference=cv - sv,
        percentage_difference=(abs(cv - sv) * 100 / abs(cv)).quantize(D("0.000001")),
        explanation=None, explanation_record_id=None, status=NR,
        reason="SOURCES_NOT_YET_CHECKED", record_ids=())


def question(c: BasisComparison):
    return (f"Why does {c.metric} differ between standalone and consolidated in {c.period}?",
            "why", "notes", ())


def test_a_stable_group_footprint_is_a_structural_pattern_not_a_question():
    rows = [comp("REVENUE", 2026, "56815", "48927"), comp("REVENUE", 2025, "52988", "44617")]
    out, extra = _structural(rows, [question(c) for c in rows])
    assert {c.status for c in out} == {ReconciliationStatus.STRUCTURAL_PATTERN}
    assert extra == [] and all(c.standalone_value and c.consolidated_value for c in out)


def test_a_material_shift_a_reversal_or_a_sign_change_stays_flagged():
    shift = [comp("GROSS_DEBT", 2026, "2186", "1505"), comp("GROSS_DEBT", 2025, "2025", "982")]
    reversal = [comp("CASH", 2026, "1000", "1500"), comp("CASH", 2025, "1000", "1480")]
    flip = [comp("PAT", 2026, "-200", "300"), comp("PAT", 2025, "-180", "290")]
    for rows in (shift, reversal, flip):
        out, extra = _structural(rows, [question(c) for c in rows])
        assert {c.status for c in out} == {NR} and len(extra) == 2
