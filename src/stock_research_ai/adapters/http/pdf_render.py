"""Dependency-free text-to-PDF rendering for the Markdown report artifacts.

Output is a plain monospace (Courier) PDF: the report's Markdown tables and indentation stay
aligned and no font files or third-party packages are needed. Only the standard PDF base-14
fonts are used, which are Latin-1 encoded, so a few common symbols are transliterated and any
remaining non-Latin-1 character becomes "?" rather than failing the export.
"""

import re

_PAGE_W, _PAGE_H = 595, 842  # A4 in points
_MARGIN = 48
_FONT_SIZE = 8.5
_LEADING = 11.5
_CHAR_W = 0.6 * _FONT_SIZE  # Courier advance width is 600/1000 em
_COLS = int((_PAGE_W - 2 * _MARGIN) // _CHAR_W)
_LINES_PER_PAGE = int((_PAGE_H - 2 * _MARGIN) // _LEADING)

_TRANSLITERATE = str.maketrans(
    {
        "₹": "Rs ",
        "–": "-",
        "—": "-",
        "−": "-",
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "•": "*",
        "…": "...",
        " ": " ",
        "→": "->",
        "≤": "<=",
        "≥": ">=",
        "✓": "ok",
    }
)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(line: str) -> str:
    line = _CONTROL.sub("", line.translate(_TRANSLITERATE).replace("\t", "    "))
    return line.encode("latin-1", errors="replace").decode("latin-1")


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(line: str) -> list[str]:
    if len(line) <= _COLS:
        return [line]
    indent = " " * (len(line) - len(line.lstrip(" ")))
    indent = indent[: _COLS // 2]
    out: list[str] = []
    rest = line
    prefix = ""
    while len(prefix + rest) > _COLS:
        room = _COLS - len(prefix)
        cut = rest.rfind(" ", len(indent) + 1, room + 1)
        if cut <= 0:
            cut = room
        out.append(prefix + rest[:cut].rstrip())
        rest = rest[cut:].lstrip(" ")
        prefix = indent
    out.append(prefix + rest)
    return out


def _layout(markdown: str) -> list[list[tuple[str, bool]]]:
    """Returns pages of (text, bold) lines."""
    lines: list[tuple[str, bool]] = []
    for raw in markdown.splitlines():
        text = _clean(raw.rstrip())
        bold = text.startswith("#")
        if bold:
            text = text.lstrip("#").strip().upper()
            if lines and lines[-1][0]:
                lines.append(("", False))
        for piece in _wrap(text):
            lines.append((piece, bold))
    pages = [lines[i : i + _LINES_PER_PAGE] for i in range(0, len(lines), _LINES_PER_PAGE)]
    return pages or [[("", False)]]


def render_pdf(markdown: str) -> bytes:
    pages = _layout(markdown)
    objects: list[bytes] = []

    def add(body: str | bytes) -> int:
        objects.append(body.encode("latin-1") if isinstance(body, str) else body)
        return len(objects)

    catalog = add("")  # placeholders, filled once page ids are known
    pages_root = add("")
    regular = add("<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
    bold = add(
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold /Encoding /WinAnsiEncoding >>"
    )
    page_ids: list[int] = []
    for number, page in enumerate(pages, start=1):
        ops = ["BT", f"{_LEADING} TL", f"{_MARGIN} {_PAGE_H - _MARGIN - _FONT_SIZE} Td"]
        for text, is_bold in page:
            ops.append(f"/{'F2' if is_bold else 'F1'} {_FONT_SIZE} Tf ({_escape(text)}) '")
        ops.append("ET")
        footer = f"Page {number} of {len(pages)}"
        ops.append(f"BT /F1 7 Tf {_PAGE_W - _MARGIN - 60} 24 Td ({footer}) Tj ET")
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(
            add(
                f"<< /Type /Page /Parent {pages_root} 0 R /MediaBox [0 0 {_PAGE_W} {_PAGE_H}] "
                f"/Resources << /Font << /F1 {regular} 0 R /F2 {bold} 0 R >> >> "
                f"/Contents {content} 0 R >>"
            )
        )
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_root} 0 R >>".encode()
    objects[pages_root - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)
