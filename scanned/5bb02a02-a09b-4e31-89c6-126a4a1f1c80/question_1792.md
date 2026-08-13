# Q1792: get_price_and_confidence_of_type: stake or integration NAV path drifts from settled accounting [a-branch-where-optional-accounts] [adapter-mismatch]

## Question
Can an unprivileged attacker use `lending_account_liquidate` with a branch where optional accounts determine whether confidence is read or skipped so `get_price_and_confidence_of_type` values stake/integration exposure from a path that drifts from later settled accounting, breaking `liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch` and causing `High: over-liquidation, under-repayment, or protocol loss from misvaluation`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_and_confidence_of_type`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a branch where optional accounts determine whether confidence is read or skipped
- Exploit idea: Inspect special valuation helpers where derivative asset value is inferred indirectly and must still stay consistent with how deposits/withdrawals settle. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch
- Expected Immunefi impact: High: over-liquidation, under-repayment, or protocol loss from misvaluation
- Fast validation: Construct a boundary state for the derivative/integration asset and assert the accepted action remains safe when value is recomputed from the canonical settlement basis. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
