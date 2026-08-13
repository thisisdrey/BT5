# Q1772: get_price_and_confidence_of_type: integration price adapter accepts mismatched reserve/market context [a-liquidation-amount-chosen-to] [adapter-mismatch]

## Question
Can an unprivileged attacker call `lending_account_liquidate` with a liquidation amount chosen to amplify small price differences so `get_price_and_confidence_of_type` loads or refreshes pricing from a mismatched reserve/market context, violating `liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch` and causing `High: over-liquidation, under-repayment, or protocol loss from misvaluation`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_and_confidence_of_type`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a liquidation amount chosen to amplify small price differences
- Exploit idea: Probe helper functions that load external reserve/market state via remaining accounts and must bind them to the bank being mutated. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch
- Expected Immunefi impact: High: over-liquidation, under-repayment, or protocol loss from misvaluation
- Fast validation: Mix two similar external contexts and assert the pricing adapter cannot succeed unless the exact configured context is supplied. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
