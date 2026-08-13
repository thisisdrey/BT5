# Q1742: get_price_and_confidence_of_type: confidence or freshness enforcement is applied inconsistently [an-integration-backed-bank-with] [adapter-mismatch]

## Question
Can an unprivileged attacker reach `get_price_and_confidence_of_type` through `lending_account_liquidate` with an integration-backed bank with multiple plausible reserve/market accounts so confidence/freshness checks are enforced on one branch but not the branch that settles value, violating `liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch` and causing `High: over-liquidation, under-repayment, or protocol loss from misvaluation`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_and_confidence_of_type`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: an integration-backed bank with multiple plausible reserve/market accounts
- Exploit idea: Check whether all price-consuming branches use the same freshness and confidence policy before accepting state transitions. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch
- Expected Immunefi impact: High: over-liquidation, under-repayment, or protocol loss from misvaluation
- Fast validation: Feed boundary-age or high-confidence-spread contexts and assert no branch can accept if the canonical policy would reject. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
