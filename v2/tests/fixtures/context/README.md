# Part 3C synthetic fixtures

These payloads are hand-authored format examples, not copies of official responses.
Expected values are independently specified and protected by AGENTS.md. RBI sample
prose and CPI values are deliberately synthetic. Tests create issuer PDF examples
locally with pypdf. No new real provider payload is a tracked fixture.

Live proofs use separate Git-ignored raw evidence and hand-checked expectations.
RBI source: https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=58400
CPI source: https://mcp.mospi.gov.in/ (official MoSPI structured-data bridge)
Publisher/source of record: MoSPI; underlying dataset: CPI, base 2012, All India,
Combined, General-Overall. The API returns status F but the normalizer preserves it
without asserting an unverified code mapping. Missing vintage/release timestamps and
seasonal-adjustment metadata remain UNKNOWN, so live CPI coverage may be PARTIAL
while every numerical observation and lineage check passes.
