# Q1763: get_price_and_confidence_of_type: integration price adapter accepts mismatched reserve/market context [a-victim-at-the-exact] [freshness]

## Question
Can an unprivileged attacker call `lending_account_liquidate` with a victim at the exact liquidatable boundary under one price view but not another so `get_price_and_confidence_of_type` loads or refreshes pricing from a mismatched reserve/market context, violating `liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch` and causing `High: over-liquidation, under-repayment, or protocol loss from misvaluation`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_and_confidence_of_type`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a victim at the exact liquidatable boundary under one price view but not another
- Exploit idea: Probe helper functions that load external reserve/market state via remaining accounts and must bind them to the bank being mutated. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch
- Expected Immunefi impact: High: over-liquidation, under-repayment, or protocol loss from misvaluation
- Fast validation: Mix two similar external contexts and assert the pricing adapter cannot succeed unless the exact configured context is supplied. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
