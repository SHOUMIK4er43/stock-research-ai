# Financial cell integrity evidence

`hudco_statement_cells.json` retains the unedited pypdf text for physical pages
363–364 (printed 360–361) of the exact BSE HUDCO annual-report attachment cited
by live report `460a8789-ff8e-52e8-afaf-133ff7ccb3c1`. The URL, SHA-256, document
ID, physical page numbers and parser are inside the fixture. These two factual
financial-statement pages are sufficient to reproduce the original Box 3 token
coordinates, including notes 6, 23, 29 and 31. The full PDF is not committed.

The independently checked source columns are FY2026 then FY2025, in INR crore:

| Row | Note | FY2026 | FY2025 |
|---|---|---:|---:|
| Cash and Cash Equivalents | 6 | 2.08 | 5.55 |
| Equity Share Capital | 23 | 2,001.90 | 2,001.90 |
| Finance Cost | 29 | 8,930.69 | 6,747.45 |
| Employee Benefit Expense | 31 | 268.08 | 230.04 |

The fixture expectations are source truth, not values derived from the parser.
Tests construct the issuer-bound extraction in memory because the compact
fixture omits the filing's cover/issuer verification pages. Production issuer
binding is unchanged and has its own regression suite.

`infosys_split_cells.json` retains the unedited standalone P&L text at physical
page 210 of the already retained Infosys FY2026 report (the URL and content hash
are in the fixture). It establishes the real reason numeric reconstruction is
needed: `29, 211` means 29,211, and `67, 4 6 6` means 67,466. Original row
separators and the Notes/year headers distinguish these split monetary cells
from the separate dotted note reference `2.20`. These same monetary expectations
already existed in `test_financial_reconciliation.py`; they are unchanged.

Supplemental secondary observations in the test helper reproduce rounded inputs
retained in the live consolidated report: FY2026/FY2025 revenue 13,150/10,345,
PAT 4,034/2,709, assets 166,838/128,496, reserves 19,974/15,966, and share capital
2,002 (including the FY2023 3-year comparison). They are deliberately kept at
provider precision and do not silently replace them with primary values. This
isolates the extraction fix from the separately known revenue-definition, tax,
equity-definition and reporting-basis issues.

Same-cell split controls use explicit HTML/worksheet cells or original PDF row
separators plus period/Notes headers. An unbounded list of numeric fragments is
not evidence that the fragments belong to one monetary cell. All tests are
offline and use memory or pytest temporary paths; historical databases are never
opened or modified by these tests.
