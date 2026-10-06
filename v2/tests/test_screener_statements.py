from stock_research_ai.adapters.screener.html import Page


def test_source_locators_resolve_to_raw_evidence(yesbank_result, fixture_dir):
    root = Page((fixture_dir / "yesbank_consolidated.html").read_text()).root
    nodes = {n.locator: n for n in root.all()}
    for section in (
        yesbank_result.quarterly,
        yesbank_result.profit_loss,
        yesbank_result.balance_sheet,
        yesbank_result.cashflow,
        yesbank_result.ratios,
    ):
        for observation in section:
            location = observation.provenance.source_location.locator
            assert location in nodes
            assert nodes[location].raw_text == observation.original_value
    assert all(r.period.kind == "duration" for r in yesbank_result.quarterly)
    assert all(r.period.frequency == "quarterly" for r in yesbank_result.quarterly)
    assert all(r.period.kind == "instant" for r in yesbank_result.balance_sheet)
    assert all(h.observation.period.kind == "instant" for h in yesbank_result.shareholding.holdings)
    assert all(h.observation.period.end_date is None for h in yesbank_result.shareholding.holdings)
    assert all(r.period.start_date is None for r in yesbank_result.profit_loss)
    assert all(
        r.period.frequency == "ttm" and r.period.end_date is None
        for r in yesbank_result.profit_loss
        if r.period.fiscal_label == "TTM"
    )
    eps = next(r for r in yesbank_result.quarterly if r.metric_id == "eps")
    assert eps.unit.scale == 1 and eps.unit.currency == "INR"
    revenue = next(r for r in yesbank_result.quarterly if r.metric_id == "revenue")
    assert revenue.unit.scale == 10000000
