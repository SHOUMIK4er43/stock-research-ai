# Independent Part 5A proof inputs

Authored before production valuation code. inputs.json, expected.json and synthetic.html
are separate inputs, literal golden outputs and explicitly synthetic source structure.
No production implementation generates expected values. Prior fixtures are unchanged.

Hand derivation: 110/1.1=100; 121/1.21=100; terminal 121/.1=1210;
1210/1.21=1000; EV=1200; equity=1200-200=1000; /10 shares=100.
With FCFF0=100, u=1+growth, price=(100*u+1000*u*u)/11-20.
At growth=0 price=80 (residual -20); at .2 price=1340/11 (residual 240/11).
Bracket midpoint .1 is the exact hand-specified root, so bisection takes one iteration
and has residual zero. Policy also accepts exact midpoint roots before interval-width convergence.
For non-exact roots both residual and interval tolerances are 1e-30, at 70 working digits.
EPS/BVPS: price 100*1.1^2=121; /20=6.05, /2=60.5; positive square-root growth=10%.
P/E=100/5=20; P/B=100/50=2. Source-reported synthetic P/E=99 is intentionally different.
Real retained annual EPS proofs: INFY 1087/72.03=108700/7203;
YESBANK 22.4/1.12=20. These are not the source-reported ratios 14.2 and 18.6.
The source labels, Mar 2026 annual EPS cells, scopes and original artifact hashes were
independently inspected. Unknown basic/diluted and adjustment basis stay explicit.
Book Value does not establish BVPS: real P/B must refuse. No provider payload is added.

Exact terminating arithmetic compares exactly. Repeating rational and root results use
absolute 1e-45 tolerance at these fixture magnitudes to account for 50-digit storage.
The solver residual uses its separately declared 1e-30 currency-per-share tolerance.
