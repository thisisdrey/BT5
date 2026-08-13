# Q1586: get_price_of_type: cached and live price paths disagree at a critical boundary [remaining-accounts-containing-multiple-same] [adapter-mismatch]

## Question
Can an unprivileged attacker call `lending_account_borrow` with remaining accounts containing multiple same-owner oracle-like accounts so `get_price_of_type` uses cached pricing in one place and live pricing in another, breaking `the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type` and causing `Critical: unbacked borrowing or solvency loss from misvaluation`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_of_type`
- Entrypoint: `lending_account_borrow`
- Attacker controls: remaining accounts containing multiple same-owner oracle-like accounts
- Exploit idea: Probe threshold states where a stale cached value would change borrowability, liquidatability, or settlement size versus a fresh lookup. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type
- Expected Immunefi impact: Critical: unbacked borrowing or solvency loss from misvaluation
- Fast validation: Prepare divergent cache/live prices and assert any accepted instruction remains valid under a single fresh pricing view. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
