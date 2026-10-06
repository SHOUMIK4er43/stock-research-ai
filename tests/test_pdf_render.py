from io import BytesIO

from pypdf import PdfReader

from stock_research_ai.adapters.http.pdf_render import render_pdf


def test_pdf_roundtrips_text_and_paginates() -> None:
    markdown = "# Title\n\n" + "\n".join(f"line {i} (paren) \ ₹100 – x" for i in range(300))
    data = render_pdf(markdown)
    reader = PdfReader(BytesIO(data), strict=True)
    assert len(reader.pages) > 1
    text = "".join(page.extract_text() for page in reader.pages)
    assert "TITLE" in text and "line 299 (paren)" in text and "Rs 100" in text


def test_long_lines_wrap_and_empty_input_is_valid() -> None:
    reader = PdfReader(BytesIO(render_pdf("word " * 200)), strict=True)
    assert reader.pages[0].extract_text().count("word") == 200
    assert len(PdfReader(BytesIO(render_pdf(""))).pages) == 1
