# Synthetic market evidence

All CSV/JSON payloads in this directory are hand-authored artificial examples of the
Kite REST schema. Prices and tokens are deliberately synthetic. These are NOT saved
Kite responses and do NOT prove real historical market values. Expected values were
specified independently of the normalizer; AGENTS.md protects them.

Documented schemas:
- https://kite.trade/docs/connect/v3/historical/
- https://kite.trade/docs/connect/v3/market-quotes/

Actual mapping verification (public master read in memory on 2026-09-08):
YESBANK 3050241; INFY 408065; NIFTY 50 256265; NIFTY BANK 260105.
Source: https://api.kite.trade/instruments/NSE (HTTP 200).
These factual observations are not hardcoded into the adapter or synthetic fixtures.

Separate authenticated numerical verification has passed for all four instruments
over January 2–3, 2025. Expectations were hand-transcribed from original Kite response
bodies, independently of the normalizer, and compared against a second acquisition.
Local truth lives in `.local/kite-market-expected.json`; proof lives in
`output/market-live-golden-proof/`. Neither is a tracked fixture. This proves fidelity
to retrieved Kite values, not independent exchange accuracy. Never rename these synthetic expectations to imply
that they are verified real YESBANK/INFY/index history. No real raw historical response
may be added here without retention permission. Personal live evidence belongs in
Git-ignored output/storage, subject to the user's permitted usage.
