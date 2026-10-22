# HUDCO reporting-basis fixtures

Sanitized public anonymous Screener captures retained by the live product (raw artifacts and
extraction IDs in manifest.json), sanitized like tests/fixtures/screener. They reproduce the live
HUDCO defect: the user's Summary was generated from the standalone page (extraction bb3daa23,
Revenue 13294 / Reserves 19975 / Total assets 166840, standalone) while Detailed Research bound the
consolidated page (Revenue 13150 / Reserves 19974 / Total assets 166838, consolidated).

The sanitized standalone page normalizes to exactly the retained bb3daa23 Summary rows. The
consolidated capture is the latest retained one (2026-10-06); it is the Detailed Research page
evidence, not a reconstruction of the 2026-10-18 run. Tests never refresh these files.

The standalone page's Revenue 13294 is Screener's own definition (the filing's standalone and
consolidated revenue from operations are both 13,150.40); that separate revenue-definition issue
is deliberately preserved, not corrected, by these fixtures.

## TFCI: consolidated statements that are not current

tfci_consolidated.html / tfci_standalone.html (captured 2026-10-21, sanitized the same way) are
Tourism Finance Corporation of India: Screener's consolidated view carries annual statements only
up to Mar 2022 while the standalone view runs to Mar 2026. They reproduce the forensic-audit
defect where the research chose CONSOLIDATED and presented FY2022 as the latest year; the
analytical basis is now STANDALONE (the consolidated statements are not current) in Summary,
Detailed Research and Negatives alike.
