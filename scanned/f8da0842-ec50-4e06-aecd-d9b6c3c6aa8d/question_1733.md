# Q1733: get_price_and_confidence_of_type: confidence or freshness enforcement is applied inconsistently [a-same-slot-cache-refresh] [freshness]

## Question
Can an unprivileged attacker reach `get_price_and_confidence_of_type` through `lending_account_liquidate` with a same-slot cache refresh before liquidation so confidence/freshness checks are enforced on one branch but not the branch that settles value, violating `liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch` and causing `High: over-liquidation, under-repayment, or protocol loss from misvaluation`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_and_confidence_of_type`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a same-slot cache refresh before liquidation
- Exploit idea: Check whether all price-consuming branches use the same freshness and confidence policy before accepting state transitions. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch
- Expected Immunefi impact: High: over-liquidation, under-repayment, or protocol loss from misvaluation
- Fast validation: Feed boundary-age or high-confidence-spread contexts and assert no branch can accept if the canonical policy would reject. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
