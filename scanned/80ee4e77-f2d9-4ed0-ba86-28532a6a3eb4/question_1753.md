# Q1753: get_price_and_confidence_of_type: price-type routing picks a safer-looking but wrong value [multiple-price-consuming-balances-on] [freshness]

## Question
Can an unprivileged attacker use `lending_account_liquidate` with multiple price-consuming balances on the same account so `get_price_and_confidence_of_type` routes to the wrong price type for the mutation being performed, violating `liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch` and leading to `High: over-liquidation, under-repayment, or protocol loss from misvaluation`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_and_confidence_of_type`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: multiple price-consuming balances on the same account
- Exploit idea: Audit distinctions like spot vs TWAP vs cache vs confidence-ignored pricing to ensure each value-moving path uses the intended conservative source. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch
- Expected Immunefi impact: High: over-liquidation, under-repayment, or protocol loss from misvaluation
- Fast validation: Exercise paths where price-type choice matters economically and assert the selected type matches protocol design for that exact action. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
