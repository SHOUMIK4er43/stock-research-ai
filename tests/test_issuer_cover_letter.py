"""V1 RC1 (live MARUTI): a cover letter's exchange address is not a second issuer.

Maruti's FY26 annual report opens with a letter to both exchanges; its two address columns
merge into '"Exchange Plaza", Bandra - Kurla Complex  BSE Limited', which was read as a
legal-entity heading other than Maruti's, so the report failed issuer binding and none of its
statements, auditor's report or governance disclosures reached the research.
"""

from stock_research_ai.application.document_intelligence import _declared_issuer_names

LETTER = """Vice President  General Manager
National Stock Exchange of India Limited Department of Corporate Services
“Exchange Plaza”, Bandra – Kurla Complex  BSE Limited
Bandra (E),  Phiroze Jeejeebhoy Towers
For Maruti Suzuki India Limited
MARUTI SUZUKI INDIA LIMITED"""


def test_an_addressee_or_address_line_is_not_an_issuer_heading():
    assert _declared_issuer_names(LETTER) == {("maruti", "suzuki", "india")}


def test_another_companys_heading_still_conflicts():
    assert ("hindustan", "unilever") in _declared_issuer_names(
        LETTER + "\nHINDUSTAN UNILEVER LIMITED")


def test_a_letter_reference_number_is_not_a_bse_scrip_code():
    from stock_research_ai.application.document_intelligence import _BSE_HEADER

    assert [m.group(1) for m in _BSE_HEADER.finditer("MSIL: COS: NSE&BSE: 2026/08_06")] == []
    assert [m.group(1) for m in _BSE_HEADER.finditer("BSE Scrip Code: 532540.")] == ["532540"]
