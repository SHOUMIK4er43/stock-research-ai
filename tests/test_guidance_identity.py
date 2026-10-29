"""V1 RC1 (Tech Mahindra): one real guidance target is one target.

TECHM's '15 % EBIT margin for FY27' was stated in January and reiterated in October; the
report listed it twice as ISSUED because the word 'standalone' appeared somewhere else in the
October call, which tagged every October guidance sentence STANDALONE and split the chain. The
reliability line read '0 completed of 0 issued' while the target was open.
"""

from test_management_intelligence import MANAGEMENT, WELCOME, call, guidance, run

from stock_research_ai.domain.management_intelligence import Lifecycle

TARGET = "We expect EBIT margin for FY27 of 15%."
AGAIN = "We remain on track to achieve our FY27 EBIT margin target of 15%."
STRAY = ("Our software products business, which we also report on a standalone basis "
         "internally, had a seasonal quarter.")


def docs(second: str) -> dict[str, list[list[str]]]:
    return {
        "/q3.pdf": call("Q3 FY26 Earnings Conference Call", "January 16, 2026", [
            WELCOME, f"Anil Sharma: Good afternoon everyone and thank you for joining. Our "
            f"consolidated revenue grew this quarter. {TARGET} We remain committed to our "
            "targets."], MANAGEMENT),
        "/q2.pdf": call("Q2 FY27 Earnings Conference Call", "October 15, 2026", [
            WELCOME, f"Anil Sharma: Good afternoon everyone and thank you for joining. {second}"],
            MANAGEMENT),
    }


async def test_incidental_basis_words_never_split_one_target(tmp_path):
    data, _b, _c = await run(tmp_path, docs(f"{STRAY} {AGAIN}"))
    first, again = guidance(data, "EBIT margin for FY27 of 15%"), guidance(data, "on track")
    assert again.lifecycle == Lifecycle.REITERATED
    assert again.previous_guidance_id == first.guidance_id
    # the January call reads CONSOLIDATED, the October one STANDALONE - both only from words
    # elsewhere in each call: the chain holds and the basis is flagged unconfirmed
    assert (first.basis_method, again.basis_method) == ("EVENT_MANAGEMENT_TEXT",) * 2
    assert first.reporting_basis != again.reporting_basis
    assert "BASIS_UNCONFIRMED" in again.warnings and "BASIS_CHANGED" not in again.warnings


async def test_an_explicit_basis_change_still_splits(tmp_path):
    data, _b, _c = await run(tmp_path, docs(
        "On a standalone basis we expect EBIT margin for FY27 of 15%."))
    explicit = guidance(data, "On a standalone basis")
    assert explicit.basis_method == "STATEMENT_TEXT"
    assert explicit.lifecycle == Lifecycle.ISSUED and "BASIS_CHANGED" in explicit.warnings


async def test_report_tracker_collapses_a_reiterated_target(tmp_path):
    data, _b, _c = await run(tmp_path, docs(f"{STRAY} {AGAIN}"))
    chained = [g for g in data.guidance if g.previous_guidance_id]
    assert chained
    superseded = {g.previous_guidance_id for g in data.guidance}
    heads = [g for g in data.guidance if g.guidance_id not in superseded]
    # the report's tracker lists chain heads only: one row for the reiterated target
    assert len(heads) == len(data.guidance) - len(chained) == 1
