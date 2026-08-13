# Q1647: get_price_of_type: integration price adapter accepts mismatched reserve/market context [a-state-where-derivative-or] [freshness]

## Question
Can an unprivileged attacker call `lending_account_borrow` with a state where derivative or integration price helpers can load two plausible contexts so `get_price_of_type` loads or refreshes pricing from a mismatched reserve/market context, violating `the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type` and causing `Critical: unbacked borrowing or solvency loss from misvaluation`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_of_type`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a state where derivative or integration price helpers can load two plausible contexts
- Exploit idea: Probe helper functions that load external reserve/market state via remaining accounts and must bind them to the bank being mutated. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type
- Expected Immunefi impact: Critical: unbacked borrowing or solvency loss from misvaluation
- Fast validation: Mix two similar external contexts and assert the pricing adapter cannot succeed unless the exact configured context is supplied. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
