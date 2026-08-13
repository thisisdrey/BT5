# Q1597: get_price_of_type: cached and live price paths disagree at a critical boundary [a-price-context-at-freshness] [freshness]

## Question
Can an unprivileged attacker call `lending_account_borrow` with a price context at freshness or confidence boundaries so `get_price_of_type` uses cached pricing in one place and live pricing in another, breaking `the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type` and causing `Critical: unbacked borrowing or solvency loss from misvaluation`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_of_type`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a price context at freshness or confidence boundaries
- Exploit idea: Probe threshold states where a stale cached value would change borrowability, liquidatability, or settlement size versus a fresh lookup. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type
- Expected Immunefi impact: Critical: unbacked borrowing or solvency loss from misvaluation
- Fast validation: Prepare divergent cache/live prices and assert any accepted instruction remains valid under a single fresh pricing view. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
