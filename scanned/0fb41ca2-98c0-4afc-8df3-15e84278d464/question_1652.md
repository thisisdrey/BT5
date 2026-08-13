# Q1652: get_price_of_type: stake or integration NAV path drifts from settled accounting [a-bank-whose-cached-and] [adapter-mismatch]

## Question
Can an unprivileged attacker use `lending_account_borrow` with a bank whose cached and live price context diverge at the borrow threshold so `get_price_of_type` values stake/integration exposure from a path that drifts from later settled accounting, breaking `the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type` and causing `Critical: unbacked borrowing or solvency loss from misvaluation`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_of_type`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a bank whose cached and live price context diverge at the borrow threshold
- Exploit idea: Inspect special valuation helpers where derivative asset value is inferred indirectly and must still stay consistent with how deposits/withdrawals settle. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type
- Expected Immunefi impact: Critical: unbacked borrowing or solvency loss from misvaluation
- Fast validation: Construct a boundary state for the derivative/integration asset and assert the accepted action remains safe when value is recomputed from the canonical settlement basis. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
