"""Conservative analyst input budget (Part 6D): deterministic, priority-ordered,
recency-capped evidence selection. Every kept reference is whole and untouched;
every dropped reference is dropped whole - never a partial/sliced JSON structure.
"""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_analysis import world as evidence_world

from stock_research_ai.application import analyst_model_input as pin
from stock_research_ai.application.analysis import build_source_references
from stock_research_ai.domain.analysis import EvidenceReference

world = evidence_world


async def test_byte_budget_cannot_be_exceeded(world, monkeypatch):
    *_, bundle, evidence = world
    monkeypatch.setattr(pin, "MAX_INPUT_BYTES", 20_000)
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    assert 0 < len(selected) < len(exhaustive)
    total = sum(len(pin._json(pin._content(r, evidence)).encode("utf-8")) for r in selected)
    assert total <= pin.MAX_INPUT_BYTES


async def test_item_budget_cannot_be_exceeded(world, monkeypatch):
    *_, bundle, evidence = world
    monkeypatch.setattr(pin, "MAX_ITEMS", 3)
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    assert len(selected) <= 3


async def test_selected_items_are_never_partial_or_modified(world, monkeypatch):
    *_, bundle, evidence = world
    monkeypatch.setattr(pin, "MAX_INPUT_BYTES", 5_000)
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    assert selected
    exhaustive_set = set(exhaustive)
    for ref in selected:
        # Every kept reference is one of the original, untouched candidates - never a
        # synthesized, truncated, or otherwise modified reference/content pair.
        assert ref in exhaustive_set
        raw = pin._content(ref, evidence)
        assert json.loads(pin._json(raw)) == raw


async def test_selection_is_deterministic(world):
    *_, bundle, evidence = world
    exhaustive = build_source_references(bundle.request, evidence)
    first = pin.select_bounded_sources(exhaustive, evidence)
    second = pin.select_bounded_sources(exhaustive, evidence)
    assert first == second


async def test_recent_periods_preferred_over_older_periods(world):
    source, *_, bundle, evidence = world
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    selected_keys = {r.item_key for r in selected if r.kind == "FINANCIAL"}

    by_id = {str(o.id): o for o in source.profit_loss}
    distinct_periods = sorted(
        {(o.period.end_date, o.period.fiscal_label) for o in source.profit_loss},
        key=lambda p: (p[0] is None, p[0]),
        reverse=True,
    )
    if len(distinct_periods) <= pin._ANNUAL_PERIOD_CAP:
        pytest.skip("fixture does not have enough distinct annual periods to prove recency")
    newest = distinct_periods[0]
    oldest = distinct_periods[-1]

    def period_of(o) -> tuple:
        return (o.period.end_date, o.period.fiscal_label)

    newest_ids = {k for k, o in by_id.items() if period_of(o) == newest}
    oldest_ids = {k for k, o in by_id.items() if period_of(o) == oldest}
    assert newest_ids & selected_keys
    assert not (oldest_ids & selected_keys)


async def test_calculations_and_valuations_retain_priority_under_tight_budget(world, monkeypatch):
    *_, calculation, valuation, _, bundle, evidence = world
    # Tight enough to force real exclusions, generous enough for company + all
    # calc/valuation results (tier 1/2) to still fit comfortably ahead of history.
    monkeypatch.setattr(pin, "MAX_INPUT_BYTES", 15_000)
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    assert len(selected) < len(exhaustive)
    selected_calc_valuation = {
        r.item_key for r in selected if r.kind in ("FINANCIAL_CALCULATION", "VALUATION")
    }
    all_calc_valuation = {
        r.item_key for r in exhaustive if r.kind in ("FINANCIAL_CALCULATION", "VALUATION")
    }
    assert all_calc_valuation
    assert selected_calc_valuation == all_calc_valuation


async def test_shareholding_history_obeys_its_period_cap(world):
    source, *_, bundle, evidence = world
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    selected_keys = {r.item_key for r in selected if r.kind == "SHAREHOLDING"}

    period_by_id: dict[str, tuple] = {}
    for holding in source.shareholding.holdings:
        period_by_id[str(holding.observation.id)] = (
            holding.observation.period.end_date,
            holding.observation.period.fiscal_label,
        )
    for count in source.shareholding.shareholder_counts:
        period_by_id[str(count.id)] = (count.period.end_date, count.period.fiscal_label)

    distinct_selected_periods = {period_by_id[k] for k in selected_keys if k in period_by_id}
    assert len(distinct_selected_periods) <= pin._SHAREHOLDING_PERIOD_CAP


async def test_document_chunks_obey_their_cap(world):
    *_, document, bundle, evidence = world
    base_chunk = document.chunks[0]
    extra_chunks = tuple(
        base_chunk.model_copy(
            update={"id": uuid4(), "ordinal": i + 2, "start_char": i, "end_char": i + 1}
        )
        for i in range(pin.MAX_DOCUMENT_CHUNKS + 5)
    )
    extended_document = document.model_copy(update={"chunks": (base_chunk, *extra_chunks)})
    extended_evidence = replace(evidence, documents={document.version_id: extended_document})
    raw_sha = document.artifact.content_sha256 if document.artifact else None
    refs = tuple(
        EvidenceReference(
            kind="DOCUMENT_CHUNK",
            owning_snapshot_id=document.version_id,
            item_key=str(c.id),
            snapshot_payload_sha256=pin.digest(extended_document),
            raw_artifact_sha256=raw_sha,
        )
        for c in extended_document.chunks
    )
    assert len(refs) == pin.MAX_DOCUMENT_CHUNKS + 6

    selected = pin.select_bounded_sources(refs, extended_evidence)
    chunk_selected = [r for r in selected if r.kind == "DOCUMENT_CHUNK"]
    assert len(chunk_selected) == pin.MAX_DOCUMENT_CHUNKS


async def test_references_remain_attached_to_every_selected_item(world, monkeypatch):
    *_, bundle, evidence = world
    monkeypatch.setattr(pin, "MAX_INPUT_BYTES", 10_000)
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    assert selected
    model_input = pin.build_model_input(bundle.model_copy(update={"sources": selected}), evidence)
    assert tuple(i.reference for i in model_input.items) == selected
    for item in model_input.items:
        assert item.reference.kind and item.reference.owning_snapshot_id


async def test_dropped_count_is_accurate_between_exhaustive_and_selected(world, monkeypatch):
    *_, bundle, evidence = world
    monkeypatch.setattr(pin, "MAX_INPUT_BYTES", 20_000)
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    dropped = set(exhaustive) - set(selected)
    assert len(dropped) == len(exhaustive) - len(selected)
    assert dropped.isdisjoint(set(selected))
    assert dropped | set(selected) == set(exhaustive)


# --- Part 6D-2: category-reservation revision -------------------------------------


async def test_shareholding_cannot_be_starved_by_financial_history(world, monkeypatch):
    """Regression proof for the exact bug reported against the first cut of this
    budget: a pure sequential greedy fill let FINANCIAL's hundreds of cheap
    observations exhaust the byte budget before SHAREHOLDING was ever reached."""
    *_, bundle, evidence = world
    # Enough for every mandatory floor (company, calc/valuation, top_metrics, one
    # latest period of every financial family, one latest shareholding period, the
    # document floor) but far short of what full financial recency expansion (5
    # annual periods across four families plus 8 quarters) would need - a naive
    # sequential greedy fill would spend everything on financial history long before
    # reaching shareholding at all.
    monkeypatch.setattr(pin, "MAX_INPUT_BYTES", 200_000)
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    assert any(r.kind == "SHAREHOLDING" for r in selected)


async def test_latest_shareholding_period_always_retained(world, monkeypatch):
    source, *_, bundle, evidence = world
    monkeypatch.setattr(pin, "MAX_INPUT_BYTES", 200_000)
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    selected_keys = {r.item_key for r in selected if r.kind == "SHAREHOLDING"}

    period_by_id = {}
    for holding in source.shareholding.holdings:
        period_by_id[str(holding.observation.id)] = (
            holding.observation.period.end_date,
            holding.observation.period.fiscal_label,
        )
    for count in source.shareholding.shareholder_counts:
        period_by_id[str(count.id)] = (count.period.end_date, count.period.fiscal_label)
    # Use the production ordering directly: shareholding periods here have no
    # end_date at all (only a "Mon YYYY" fiscal_label), so recency depends on
    # parsing that label, not a naive None-check.
    latest_period = max(period_by_id.values(), key=pin._period_identity_key)
    latest_ids = {k for k, p in period_by_id.items() if p == latest_period}
    assert latest_ids & selected_keys


async def test_shareholding_targets_four_periods_when_budget_permits(world):
    source, *_, bundle, evidence = world
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    selected_keys = {r.item_key for r in selected if r.kind == "SHAREHOLDING"}

    period_by_id = {}
    for holding in source.shareholding.holdings:
        period_by_id[str(holding.observation.id)] = (
            holding.observation.period.end_date,
            holding.observation.period.fiscal_label,
        )
    for count in source.shareholding.shareholder_counts:
        period_by_id[str(count.id)] = (count.period.end_date, count.period.fiscal_label)
    distinct_periods = len({period_by_id[k] for k in selected_keys if k in period_by_id})
    available = len(set(period_by_id.values()))
    assert distinct_periods == min(pin._SHAREHOLDING_PERIOD_CAP, available)


async def test_at_least_six_document_chunks_survive_when_available(world):
    *_, document, bundle, evidence = world
    base_chunk = document.chunks[0]
    extra_chunks = tuple(
        base_chunk.model_copy(
            update={"id": uuid4(), "ordinal": i + 2, "start_char": i, "end_char": i + 1}
        )
        for i in range(9)
    )
    extended_document = document.model_copy(update={"chunks": (base_chunk, *extra_chunks)})
    extended_evidence = replace(evidence, documents={document.version_id: extended_document})
    refs = tuple(
        EvidenceReference(
            kind="DOCUMENT_CHUNK",
            owning_snapshot_id=document.version_id,
            item_key=str(c.id),
            snapshot_payload_sha256=pin.digest(extended_document),
            raw_artifact_sha256=document.artifact.content_sha256 if document.artifact else None,
        )
        for c in extended_document.chunks
    )
    assert len(refs) == 10  # >= 6, < MAX_DOCUMENT_CHUNKS

    # A large real financial history (from `evidence`) is still present alongside
    # these document chunks - the floor must survive that competition.
    all_sources = build_source_references(bundle.request, extended_evidence) + refs
    selected = pin.select_bounded_sources(all_sources, extended_evidence)
    chunk_selected = [r for r in selected if r.kind == "DOCUMENT_CHUNK"]
    assert len(chunk_selected) >= min(pin._DOCUMENT_CHUNK_FLOOR, len(refs))


async def test_document_chunk_admission_order_is_reading_order_not_uuid(world):
    """Part 3: chunk admission order must follow (page_number, ordinal) - the chunk's
    real place in the source document - never its UUID, which is unrelated to
    reading order or importance."""
    *_, document, bundle, evidence = world
    base_chunk = document.chunks[0]
    # 25 chunks with random UUIDs but a known, deliberately-scrambled-vs-UUID
    # (page_number, ordinal) reading order.
    candidates = [
        base_chunk.model_copy(
            update={
                "id": uuid4(),
                "page_number": 1,
                "ordinal": i,
                "start_char": i,
                "end_char": i + 1,
            }
        )
        for i in range(25)
    ]
    extended_document = document.model_copy(update={"chunks": tuple(candidates)})
    extended_evidence = replace(evidence, documents={document.version_id: extended_document})
    refs = {
        c.id: EvidenceReference(
            kind="DOCUMENT_CHUNK",
            owning_snapshot_id=document.version_id,
            item_key=str(c.id),
            snapshot_payload_sha256=pin.digest(extended_document),
            raw_artifact_sha256=document.artifact.content_sha256 if document.artifact else None,
        )
        for c in candidates
    }
    ref_tuple = tuple(refs.values())

    selected = pin.select_bounded_sources(ref_tuple, extended_evidence)
    selected_chunk_keys = {r.item_key for r in selected if r.kind == "DOCUMENT_CHUNK"}
    expected_by_reading_order = {str(c.id) for c in candidates[: pin.MAX_DOCUMENT_CHUNKS]}
    by_uuid = sorted(ref_tuple, key=lambda r: r.item_key or "")
    expected_by_uuid_order = {r.item_key for r in by_uuid[: pin.MAX_DOCUMENT_CHUNKS]}
    assert selected_chunk_keys == expected_by_reading_order
    assert selected_chunk_keys != expected_by_uuid_order


async def test_category_reservation_still_respects_max_input_bytes(world, monkeypatch):
    *_, bundle, evidence = world
    monkeypatch.setattr(pin, "MAX_INPUT_BYTES", 15_000)
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    total = sum(len(pin._json(pin._content(r, evidence)).encode("utf-8")) for r in selected)
    assert total <= pin.MAX_INPUT_BYTES


async def test_category_reservation_still_respects_max_items(world, monkeypatch):
    *_, bundle, evidence = world
    monkeypatch.setattr(pin, "MAX_ITEMS", 5)
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    assert len(selected) <= 5


# --- Part 6D-3: financial-family round-robin reservation --------------------------


def _financial_category_by_item(evidence) -> dict[str, str]:
    f = evidence.financial
    mapping: dict[str, str] = {}
    for cat in pin._FINANCIAL_CATEGORY_ORDER:
        for obs in getattr(f, cat):
            mapping[str(obs.id)] = cat
    return mapping


def _financial_period_by_item(evidence) -> dict[str, tuple]:
    f = evidence.financial
    mapping: dict[str, tuple] = {}
    for cat in pin._FINANCIAL_CATEGORY_ORDER:
        for obs in getattr(f, cat):
            mapping[str(obs.id)] = (obs.period.end_date, obs.period.fiscal_label)
    return mapping


def _selected_family(selected, evidence, category: str):
    cats = _financial_category_by_item(evidence)
    return [r for r in selected if r.kind == "FINANCIAL" and cats.get(r.item_key) == category]


@pytest.mark.parametrize("family", ["balance_sheet", "cashflow", "ratios"])
async def test_secondary_annual_family_cannot_fall_to_zero(world, family):
    source, *_, bundle, evidence = world
    assert getattr(source, family), f"fixture has no {family} evidence to test against"
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    assert _selected_family(selected, evidence, family)


@pytest.mark.parametrize("family", ["profit_loss", "balance_sheet", "cashflow", "ratios"])
async def test_annual_family_targets_two_periods_when_budget_permits(world, family):
    # Floors are minimums, not final caps - the default budget is generous enough to
    # complete every family's floor (and sometimes expand a family past it via
    # round-robin target expansion), which is exactly "when budget permits".
    source, *_, bundle, evidence = world
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    periods = _financial_period_by_item(evidence)
    selected_periods = {periods[r.item_key] for r in _selected_family(selected, evidence, family)}
    available = len({periods[str(o.id)] for o in getattr(source, family)})
    assert len(selected_periods) >= min(pin._ANNUAL_FLOOR_PERIODS, available)


async def test_quarterly_reserves_four_quarters_when_budget_permits(world):
    source, *_, bundle, evidence = world
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    periods = _financial_period_by_item(evidence)
    selected_quarterly = _selected_family(selected, evidence, "quarterly")
    selected_periods = {periods[r.item_key] for r in selected_quarterly}
    available = len({periods[str(o.id)] for o in source.quarterly})
    assert len(selected_periods) >= min(pin._QUARTERLY_FLOOR_PERIODS, available)


async def test_profit_loss_cannot_starve_another_financial_family(world, monkeypatch):
    """A budget generous enough for every family's floor, but far short of full
    expansion, must not let profit_loss (admitted first within the annual
    round-robin) consume balance_sheet/cashflow/ratios's own floor allocation."""
    *_, bundle, evidence = world
    monkeypatch.setattr(pin, "MAX_INPUT_BYTES", 200_000)
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    for family in ("balance_sheet", "cashflow", "ratios"):
        assert _selected_family(selected, evidence, family), family


async def test_quarterly_cannot_starve_another_financial_family(world, monkeypatch):
    *_, bundle, evidence = world
    monkeypatch.setattr(pin, "MAX_INPUT_BYTES", 200_000)
    exhaustive = build_source_references(bundle.request, evidence)
    selected = pin.select_bounded_sources(exhaustive, evidence)
    assert _selected_family(selected, evidence, "quarterly")
    for family in ("profit_loss", "balance_sheet", "cashflow", "ratios"):
        assert _selected_family(selected, evidence, family), family


async def test_full_selection_stays_within_max_input_bytes(world):
    *_, bundle, evidence = world
    model_input = pin.build_model_input(bundle, evidence)
    rendered = pin.serialize_model_input(bundle, model_input)
    assert len(rendered.encode("utf-8")) <= pin.MAX_INPUT_BYTES
