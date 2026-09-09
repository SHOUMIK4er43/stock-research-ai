# Part 4 golden calculations

Inputs and outputs are deliberately separate. Input cells were inspected directly in retained
Part 2 HTML before implementing the formulas. Existing Part 2 goldens are unchanged.
YESBANK uses consolidated evidence; INFY uses standalone evidence.

The Jun-2024 YESBANK `Net Profit +` cell is **516**, explicitly inspected at
`#quarters/div[3]/table[1]/tbody[1]/tr[10]/td[6]` in `yesbank_consolidated.html`.
Jun-2025 is 809, hence YoY arithmetic is (809 - 516) * 100 / 516 = 29300 / 516.
Sequential arithmetic is (809 - 745) * 100 / 745 = 6400 / 745.

Expected outputs were specified before production formulas existed, using independently
checked integer numerator/denominator arithmetic and a separate Decimal division check.
They are literal golden values, never serialized CalculationResult objects. The fraction
preserves exact mathematics; the decimal string is the 50-significant-digit HALF_EVEN
representation, before two-place display rounding. Nonterminating rationals have no finite
exact Decimal representation. Do not change these expectations to make code pass.

Annual March-year and quarter-end alignment is explicitly caller-declared for this proof.
The source's unknown duration starts remain unknown. Source Revenue retains precisely that
meaning. Average-assets return uses annual net profit divided by the average of the prior
and current fiscal-year-end total assets; it is not labelled ROA.
