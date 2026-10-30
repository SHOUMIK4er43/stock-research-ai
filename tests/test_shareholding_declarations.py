"""V1 RC1 (Tech Mahindra): a promoter pledge reading from the company's own filing.

TECHM's report said 'promoter pledge data not supplied' (rule NOT EVALUATED) while 21 NSE
shareholding-pattern filings were cited; the feed lists their XBRL but carries no pledge field.
The latest filings' XBRL is now fetched (allowlisted archives host, bounded) and its promoter
encumbrance declarations parsed - bound to the verified company and the filing's own period.
'No encumbrance declared' is a 0 % reading; a declared encumbrance without a parsed quantity is
NOT EVALUATED, never 0.
"""

from test_due_diligence_market import box13, sig
from test_market_intelligence import (
    ISIN,
    SYMBOL,
    base_payloads,
    build,
    raw,
    shareholding_payload,
)

from stock_research_ai.adapters.exchange.source import shareholding_xbrl_urls
from stock_research_ai.domain.due_diligence import Severity, SignalStatus
from stock_research_ai.domain.market_intelligence import PayloadKind

XBRL = "https://nsearchives.nseindia.com/corporate/xbrl/SHP.xml"
TAG = "WhetherAnySharesHeldByPromotersAreEncumbered"


def shp_xml(day: str = "2026-09-30", isin: str = ISIN, pledged: str = "false",
            ndu: str = "false", other: str = "false") -> bytes:
    return (f'<?xml version="1.0"?><xbrli:xbrl xmlns:in-bse-shp="x">'
            f'<in-bse-shp:ISIN contextRef="MainI">{isin}</in-bse-shp:ISIN>'
            f'<in-bse-shp:Symbol contextRef="MainI">{SYMBOL}</in-bse-shp:Symbol>'
            f'<in-bse-shp:DateOfReport contextRef="MainI">{day}</in-bse-shp:DateOfReport>'
            f'<in-bse-shp:{TAG}UnderPledged contextRef="MainI">{pledged}</in-bse-shp:{TAG}'
            f'UnderPledged><in-bse-shp:{TAG}UnderNonDisposalUndertaking contextRef="MainI">{ndu}'
            f'</in-bse-shp:{TAG}UnderNonDisposalUndertaking><in-bse-shp:{TAG}OtherThanByWayOf'
            f'PledgeOrNDU contextRef="MainI">{other}</in-bse-shp:{TAG}OtherThanByWayOfPledgeOrNDU>'
            "</xbrli:xbrl>").encode()


def box12(xml: bytes):
    feed = shareholding_payload(("30-SEP-2026", "34.96", "65.04", "N"))
    return build([*base_payloads(), feed, raw(PayloadKind.NSE_SHAREHOLDING_XBRL, xml, XBRL)])


def test_the_latest_filings_xbrl_urls_are_selected_newest_first():
    feed = raw(PayloadKind.NSE_SHAREHOLDING, [
        {"date": d, "xbrl": f"https://nsearchives.nseindia.com/corporate/xbrl/{d}.xml"}
        for d in ("31-MAR-2026", "30-SEP-2026", "30-JUN-2026", "31-DEC-2025")])
    assert [u.rsplit("/", 1)[1] for u in shareholding_xbrl_urls(feed)] == [
        "30-SEP-2026.xml", "30-JUN-2026.xml", "31-MAR-2026.xml"]


def test_declarations_bind_to_company_and_period():
    (filing,) = box12(shp_xml()).shareholding
    assert (filing.promoter_pledged, filing.promoter_ndu,
            filing.promoter_other_encumbrance) == (False, False, False)
    assert "PROMOTER_PLEDGE" not in filing.unparsed_categories
    assert any(s.locator == "xbrl" for s in filing.sources)
    for wrong in (shp_xml(isin="INE000A01010"), shp_xml(day="2026-06-30")):
        (other,) = box12(wrong).shareholding
        assert other.promoter_pledged is None  # another company's / period's filing: ignored


def test_a_no_encumbrance_declaration_is_a_cited_zero_pledge():
    s = sig(box13(box12(shp_xml())), "PROMOTER_PLEDGE")
    assert (s.status, s.severity, s.value) == (SignalStatus.EVALUATED, Severity.GREEN, 0)
    assert XBRL in s.source_ids


def test_a_declared_encumbrance_is_never_read_as_zero():
    s = sig(box13(box12(shp_xml(pledged="true"))), "PROMOTER_PLEDGE")
    assert (s.status, s.reason) == (SignalStatus.UNAVAILABLE, "PLEDGE_QUANTITY_NOT_PARSED")
    assert s.value is None


def test_without_a_declaration_the_rule_still_says_not_supplied():
    plain = build([*base_payloads(), shareholding_payload(("30-SEP-2026", "34.96", "65.04",
                                                          "N"))])
    assert sig(box13(plain), "PROMOTER_PLEDGE").reason == "PLEDGE_DATA_NOT_SUPPLIED"
