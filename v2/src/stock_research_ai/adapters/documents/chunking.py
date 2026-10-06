"""Literal heading rules and lossless, page-contained character chunks."""

import hashlib
from uuid import uuid5

from stock_research_ai.domain.documents import DocumentChunk, DocumentPage, DocumentSection

HEADINGS = {
    "management discussion and analysis": "MD&A",
    "management discussion & analysis": "MD&A",
    "business overview": "BUSINESS_OVERVIEW",
    "risk management": "RISK_MANAGEMENT",
    "corporate governance": "CORPORATE_GOVERNANCE",
    "independent auditor’s report": "AUDITOR_REPORT",
    "independent auditor's report": "AUDITOR_REPORT",
    "financial statements": "FINANCIAL_STATEMENTS",
    "notes to accounts": "NOTES_TO_ACCOUNTS",
    "management remarks": "MANAGEMENT_REMARKS",
    "q&a": "Q_AND_A",
    "questions and answers": "Q_AND_A",
    "highlights": "HIGHLIGHTS",
    "strategy": "STRATEGY",
    "guidance": "GUIDANCE",
    "outlook": "OUTLOOK",
    "financial highlights": "FINANCIAL_HIGHLIGHTS",
}


def chunk_pages(
    pages: tuple[DocumentPage, ...], max_chars: int = 1600
) -> tuple[tuple[DocumentSection, ...], tuple[DocumentChunk, ...]]:
    if max_chars < 1:
        raise ValueError("Chunk size must be positive")
    sections: list[DocumentSection] = []
    chunks: list[DocumentChunk] = []
    for page in pages:
        starts: list[tuple[int, str, str | None]] = [(0, "UNKNOWN", None)]
        offset = 0
        for line in page.text.splitlines(keepends=True):
            key = " ".join(line.strip().casefold().split()).rstrip(":")
            if key in HEADINGS:
                if offset == 0:
                    starts.clear()
                starts.append((offset, HEADINGS[key], line.strip()))
            offset += len(line)
        for i, (start, label, heading) in enumerate(starts):
            end = starts[i + 1][0] if i + 1 < len(starts) else len(page.text)
            section = DocumentSection(
                id=uuid5(page.id, f"section:1:{start}:{end}"),
                page_id=page.id,
                label=label,
                heading=heading,
                start_char=start,
                end_char=end,
            )
            sections.append(section)
            for left in range(start, end, max_chars):
                right = min(left + max_chars, end)
                text = page.text[left:right]
                chunks.append(
                    DocumentChunk(
                        id=uuid5(page.id, f"chunk:1:{left}:{right}"),
                        version_id=page.version_id,
                        artifact_id=page.artifact_id,
                        page_id=page.id,
                        page_number=page.number,
                        section_id=section.id,
                        ordinal=len(chunks) + 1,
                        start_char=left,
                        end_char=right,
                        text=text,
                        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                        character_count=len(text),
                        word_count=len(text.split()),
                    )
                )
    return tuple(sections), tuple(chunks)
