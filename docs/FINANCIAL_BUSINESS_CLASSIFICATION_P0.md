# Financial business classification P0 integrity record

Scope: one defect family, V1 financial-business classification and metric applicability.
Baseline: `56e4ea017667b7f476aed59b646af289ca691bb0` on `astra-development`.
No V2 work, dependency changes, identity changes, period-parser changes, report renderer/layout
changes, P3/PR #3 work, or golden expectation changes.

## Existing taxonomy and root cause (inspection before edits)

1. `domain/financial_canonical.py:Industry` already contained GENERAL, BANK, NBFC and INSURANCE.
   However, `application/financial_reconciliation.py:detect_industry` returned only BANK or
   GENERAL. Any exact `Financing Profit` or `Deposits` row, or a `banks` classification name,
   caused BANK. Otherwise it returned GENERAL. Business descriptions were ignored.
2. Box 5's `Sector` is a coarse applicability bucket: GENERAL / FINANCIAL. BANK, NBFC and
   INSURANCE shared FINANCIAL. Most industrial formulas already refused FINANCIAL, but
   formulas registered only for FINANCIAL incorrectly admitted all three business models.
3. The older calculations/KPI/valuation path has a separate existing `SectorDeclaration`:
   BANK / OTHER_FINANCIAL / NON_FINANCIAL / UNKNOWN. Its automatic broad-industry map recognizes
   exactly `Banks` -> BANK_PROFILE and `IT - Software` -> IT_SERVICES_PROFILE; other labels
   resolve UNKNOWN. Existing caller-declared profiles also support deliberately minimal
   price/EPS/book-value inputs. These vocabularies and profile mappings were not expanded.
4. Box 7 has a separate business-sector family vocabulary (including FINANCIAL, IT_SERVICES,
   manufacturing and other operating families). It consumes Box 4 industry plus Screener
   classification names. Its segment-margin arithmetic did not enforce financial applicability.
5. Box 13 gates signals using NON_FINANCIAL / FINANCIAL, but its separate forensic formula
   table contained applicability descriptions that were not enforced. Direct canonical inputs
   could therefore produce receivables CAGR, profit-without-cash counts, working-capital
   absorption or adjusted PAT excluding other income for financial companies.
6. Summary copies normalized facts into an allowlisted claim catalogue. It did not expose
   classification or gate reported ROCE, industrial operating profit or source FCF. The LLM
   selects IDs; deterministic rendering supplies claims. That grounding architecture is intact.

Flow: retained Screener HTML -> profile breadcrumbs/about text and statement row labels ->
shared deterministic classifier -> Box 4 canonical reconciliation -> Box 5 formulas/rules ->
Box 6 charts and Box 7 business analytics -> Box 10 forecasts -> Box 11 valuation -> Box 13
risks/forensics -> Box 14 claims -> Box 15 renderers. The legacy Summary path receives the
same acquired classification on the extraction, without changing company identity.

## Reproduction and V1 contract

Controlled baseline probes, run before editing:

| Input profile | Before | After |
| --- | --- | --- |
| Commercial bank, Banking/Banks, deposits/advances/interest | BANK | BANK |
| Non-Banking Financial Company, financing income | GENERAL | NBFC |
| Housing Finance, Financing Profit | BANK | NBFC |
| Life/general Insurance, premiums/claims | GENERAL | INSURANCE |
| Manufacturing, sales/EBITDA/inventory | GENERAL | GENERAL |
| IT/software services | GENERAL | GENERAL |
| Financial Services, ambiguous revenue | GENERAL | UNKNOWN |

Reuse the existing Industry enum and add UNKNOWN. HFCs use NBFC; V1 does not need a new
regulatory HFC subtype to withhold industrial or bank-only calculations. Exact normalized
structured labels support a specific financial class. Corroborated business descriptions and
premium/claims structure supply additional evidence. Conflicts across financial types or
acquired datasets yield UNKNOWN. Financing profit, ordinary deposits, NIM, GNPA, AUM or a
loan book alone never proves BANK. Unresolved financial evidence blocks GENERAL fallback.
A general industrial statement signature or supported nonfinancial label preserves GENERAL.

This distinction is economically necessary: NBFCs can conduct lending and some can accept
public deposits, but cannot accept demand deposits like banks. See the
[RBI FAQ on NBFCs](https://systemhealth.rbi.org.in/Scripts/FS_FAQs.aspx_Id%3D92%26fn%3D14.html).
V1 gates the existing formulas; it adds no lending or insurance KPI formula.

UNKNOWN joins the restricted applicability bucket. It is not asserted to be a bank or even
a confirmed financial company. The original Industry value remains available alongside that
coarse bucket. Bank-only formulas now require Industry.BANK as well as the registry gate.
Box 4 keeps non-bank financial financing-profit lines unmapped, not as BANK_FINANCING_PROFIT;
Screener financial Revenue remains a reported aggregate, not industrial revenue from operations.
Raw observations remain retained. Missing and inapplicable values never become zero.

Fresh extraction payloads and Summary inputs carry business_classification. Legacy extraction
payloads can omit it. A price/EPS/book-value-only snapshot has no business-model evidence and
continues to require an explicit caller-declared profile in the older calculations API; an
acquired NBFC/INSURANCE/UNKNOWN classification cannot be widened by that declaration. Summary
without classification is restricted. Old persisted reports are immutable historical artifacts;
regenerate them to apply this fix. No database migration or historical report rewriting occurs.

## Applicability matrix

“Available” always means supported, comparable inputs, dates, currencies, scopes and valid
arithmetic domains are required. “Restricted” means no numeric result: the existing
UNSUPPORTED_SECTOR / NOT_APPLICABLE / UNAVAILABLE vocabulary is retained.

| Metric or rule | GENERAL | BANK | NBFC/HFC | INSURANCE | UNKNOWN |
| --- | --- | --- | --- | --- | --- |
| Reported revenue, earnings/EPS growth | Available | Available | Available | Available for explicit comparable revenue; no premium substitution | Common inputs only |
| EBITDA / EBITDA margin | Available | Restricted | Restricted | Restricted | Restricted |
| Industrial operating/segment EBIT/EBITDA margins | Available | Restricted | Restricted | Restricted | Restricted |
| Working capital, receivable/inventory/payable days | Available | Restricted | Restricted | Restricted | Restricted |
| CFO/EBITDA, CFO/PAT, cash conversion | Available | Restricted | Restricted | Restricted | Restricted |
| Industrial accruals, Altman, Beneish, Piotroski | Available | Restricted | Restricted | Restricted | Restricted |
| Raw CFO, borrowings, cash and balance-sheet facts | Retained | Retained | Retained | Retained | Retained |
| Industrial gross/net debt constructions and leverage thresholds | Available | Restricted | Restricted | Restricted | Restricted |
| EBIT interest cover, net debt/EBITDA | Available | Restricted | Restricted | Restricted | Restricted |
| ROE, ROA, equity/assets | Available | Available | Available | Available | Common inputs only |
| ROCE, asset turnover, industrial DuPont | Available | Restricted | Restricted | Restricted | Restricted |
| Contingent liabilities: raw disclosures | Retained | Retained | Retained | Retained | Retained |
| Contingencies/net-worth industrial RED/YELLOW thresholds | Available | Restricted | Restricted | Restricted | Restricted |
| Other income: reported fact | Retained | Retained | Retained | Retained | Retained |
| Other-income dependence; PAT excluding other income | Available | Restricted | Restricted | Restricted | Restricted |
| Effective/unusual tax, exceptional items, negative equity | Existing checks | Existing checks | Existing checks | Existing checks | Existing common checks |
| Deposit/advance growth, cost-to-income, financing margin, provisions/revenue | Restricted | Existing formulas | Restricted (no approved NBFC convention) | Restricted | Restricted |
| GNPA/NNPA in Box 5 | Restricted | Unavailable: no canonical KPI input | Restricted | Restricted | Restricted |
| Reported bank/NBFC asset-quality facts in Summary/Box 7 | Restricted | Source facts only | Source facts only | No invented KPI | Restricted in Summary |
| FCFF DCF, EV/EBITDA and enterprise multiples | Existing methods | Restricted | Restricted | Restricted | Restricted |
| P/E, book value, P/B | Existing supported methods/facts | Existing supported methods/facts | Reported common facts; no new valuation profile | Reported common facts; no new valuation profile | Reported common facts only |

PBT/PAT margins remain descriptive ratios to the same reported revenue definition, not
industrial operating margins or insurer underwriting ratios. Governance and cited credit-rating
rules remain available. Full financial-sector PAT forecasts are not implemented: Box 10 retains
revenue-only forecasting, so Box 11 cannot manufacture financial-company forward P/E values.
Valuation assumptions remain explicit and profile/method gated; no sector multiples were added.

## Downstream audit and safety questions

- Box 4: classification corrected before canonical mapping; UNKNOWN warning for unresolved
  acquired profiles; classifier version included in dataset identity. Filing/period/identity
  parsing and consolidated/standalone selection remain unchanged.
- Box 5: existing industrial gates now reach NBFC/HFC, insurers and UNKNOWN; additional exact
  BANK check protects bank formulas. Valid common metrics are retained.
- Box 6: consumes the gated Box 5 values; charts cannot obtain an invalid numeric point.
- Box 7: confirmed financial types use the restrictive family. When Box 4 is UNKNOWN,
  Box 7 checks profile classifications: explicit financial breadcrumbs remain restricted,
  while a known operating family (for example IT services) keeps its existing treatment.
  Industrial segment margins are unavailable for financial companies. Source-reported KPIs
  still require their existing exact aliases, units and provenance.
- Box 10: restricted businesses cannot use industrial P&L, debt or working-capital forecast
  drivers. Box 11 withholds FCFF/enterprise-multiple valuation.
- Box 13: enforce its existing formula applicability on the metric table and adjusted values;
  gated risk signals cannot retain an invalid numeric reading or a RED/YELLOW severity.
  Common governance, tax and negative-equity checks remain available.
- Box 14: existing unavailable-value discipline blocks invalid metrics from claims. The
  source Operating Profit row is also gated for restricted businesses. No new layout or
  rendering rule is introduced. Classification breadcrumbs are already exposed by Box 7/9.
- Box 15: existing renderers consume Box 14; no renderer/presentation implementation changed.
- Summary: classification is exposed in its input contract; inappropriate metrics cannot
  enter the grounded claim catalogue. Generated text remains sourced facts, without an
  invented sector narrative or changed grounding architecture.
- Negatives/Risks: the existing industrial thresholds now receive no inapplicable number.

Could the original defect create a false RED/YELLOW risk? **Yes**: an insurer/NBFC falling into
GENERAL could trigger working-capital, cash-conversion, leverage or accrual screens.
Could it create misleading valuation inputs? **Yes**: GENERAL enabled industrial forecasts,
net-debt/EBITDA and FCFF/EV assumptions.
Could it make a healthy financial company look distressed? **Yes**: financing balance sheets
and cash flows have different meanings from industrial working capital and funding.
Could it hide a real financial-sector risk? **Yes**, and gating alone does not supply missing
asset-quality, ALM, capital or solvency evidence. V1 explicitly reports restricted/unsupported
coverage; it does not treat absence of a flag as proof of financial health.

## Retained controls

Replayed both retained files before and after with temporary databases/artifact directories.
Compared sorted (metric, value, exact period key, basis) canonical observations, all calculated
metric codes/statuses/values/periods/bases, and risk codes/statuses/severities/values.

| Control | Classification | Canonical values | Calculated metric records | Risk records | Comparison |
| --- | --- | ---: | ---: | ---: | --- |
| INFY standalone | GENERAL | 402 | 2,063 | 31 | All compared fields identical |
| YESBANK consolidated | BANK | 414 | 680 | 30 | All compared fields identical |

FY ended 2026-03-31 examples, unchanged:

| Metric | INFY standalone | YESBANK consolidated |
| --- | ---: | ---: |
| Revenue growth YoY % | 8.951476 | -2.299557 |
| ROE % | 34.732411 | 7.095595 |
| ROA % | 23.355721 | 0.785401 |
| EBITDA margin % | 27.790806 | UNSUPPORTED_SECTOR, no value |
| Financing profit margin % | UNSUPPORTED_SECTOR, no value | -6.167240 |

Neither retained control produced a RED/YELLOW Box 13 company-risk flag in this replay.
Summary retains INFY's valid claims and YESBANK's financing/NPA facts; bank ROCE and source FCF
are deliberately withheld. Existing golden calculation, sector-KPI and valuation assertions
remain untouched.

NBFC/HFC retained/live coverage: **NOT VERIFIED**; no such retained fixture was found.
Insurance retained/live coverage: **NOT VERIFIED**; no such retained fixture was found.
Both have synthetic profile, statement, formula, risk, forecast, valuation and final-claim
controls. No live network acquisition is part of the automated tests.

## Findings and remaining limits

- Fixed High: incorrect business classification could enable the wrong financial model.
- Fixed High: broad FINANCIAL eligibility admitted bank formulas for insurers/other lenders.
- Fixed Medium: direct forensic-table and Summary paths bypassed metric applicability.
- Residual Medium coverage limitation: no complete NBFC/HFC or insurer financial model,
  canonical solvency/ALM/asset-quality coverage, or retained real-company validation for these
  sectors. This patch withholds unsupported analysis and exposes the limitation.
- Source metadata can itself be incomplete, conflicting or incorrect. Recognized conflicts and
  ambiguous financial evidence yield UNKNOWN. The classifier is a bounded V1 mapping, not a
  regulator-registration lookup or an inference about a group's unreported subsidiaries.
- Legacy caller-declared profiles remain an explicit input contract when a minimal/old
  extraction supplies no business-model evidence; this is not an automatic GENERAL fallback.

## Verification

New tests are in `tests/test_financial_business.py`; existing golden fixtures and expected
values are unchanged. Automated tests use the repository's offline network guard and temporary
stores. Final execution counts, changed files and release-safety recheck are recorded below
once the complete suite finishes.
