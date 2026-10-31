"""V1 RC1 (Tech Mahindra): NSE feed rows stamped with the issuer's earlier equity ISIN.

NSE's announcement, results and corporate-action feeds stamp TECHM's rows with INE669C01028;
the verified current equity ISIN is INE669C01036 (same issuer 669C, equity type 01, a later
serial). Every row - 1,704 announcements, 148 results, 20 actions - was dropped as 'another
security', leaving Events at 0 of 0 and quarterly results 'missing'. Such a row is now kept
only when its symbol is the verified NSE symbol AND the ISIN differs only in its serial.
"""

from types import SimpleNamespace

import pytest

from stock_research_ai.application.exchange_intelligence import row_identity, superseded_isin

ID = SimpleNamespace(nse_symbol="TECHM", isin="INE669C01036", bse_code=None,
                     declared_nse_symbol="TECHM", declared_isin=None, declared_bse_code=None)


def test_the_issuers_earlier_equity_isin_is_recognised_and_nothing_else():
    assert superseded_isin("INE669C01028", "INE669C01036")
    assert not superseded_isin("INE669C01036", "INE669C01036")  # identical: not superseded
    assert not superseded_isin("INE670C01028", "INE669C01036")  # another issuer
    assert not superseded_isin("INE669C08028", "INE669C01036")  # a debt security type


@pytest.mark.parametrize("symbol,isin,expected", [
    ("TECHM", "INE669C01028", "SUPERSEDED_ISIN"),  # kept: verified symbol + issuer code
    (None, "INE669C01028", "MISMATCH"),  # an ISIN alone never binds: refused
    ("INFY", "INE669C01028", "MISMATCH"),  # another symbol
    ("TECHM", "INE009A01021", "MISMATCH"),  # another issuer's ISIN on the right symbol
    ("TECHM", "INE669C01036", "MATCH"),
])
def test_rows_bind_only_by_symbol_and_issuer_together(symbol, isin, expected):
    assert row_identity(ID, symbol=symbol, isin=isin) == expected  # type: ignore[arg-type]
