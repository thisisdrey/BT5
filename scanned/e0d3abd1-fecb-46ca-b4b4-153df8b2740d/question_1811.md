# Q1811: get_price_and_confidence_of_type: price cache update enables later unauthorized value extraction [a-victim-at-the-exact] [freshness]

## Question
Can an unprivileged attacker call `lending_account_liquidate` with a victim at the exact liquidatable boundary under one price view but not another so `get_price_and_confidence_of_type` commits a misleading but accepted cache update that later breaks `liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch` and leads to `High: over-liquidation, under-repayment, or protocol loss from misvaluation`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_and_confidence_of_type`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a victim at the exact liquidatable boundary under one price view but not another
- Exploit idea: Audit permissionless price-cache writes for any caller-controlled dimensions that later trusted instructions consume without recomputing. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch
- Expected Immunefi impact: High: over-liquidation, under-repayment, or protocol loss from misvaluation
- Fast validation: Write the controlled cache state, then immediately invoke dependent instructions and assert none can extract value unless a fresh canonical price agrees. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
