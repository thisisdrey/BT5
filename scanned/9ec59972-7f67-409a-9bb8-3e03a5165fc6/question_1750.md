# Q1750: get_price_and_confidence_of_type: price-type routing picks a safer-looking but wrong value [a-same-slot-cache-refresh] [adapter-mismatch]

## Question
Can an unprivileged attacker use `lending_account_liquidate` with a same-slot cache refresh before liquidation so `get_price_and_confidence_of_type` routes to the wrong price type for the mutation being performed, violating `liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch` and leading to `High: over-liquidation, under-repayment, or protocol loss from misvaluation`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_and_confidence_of_type`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a same-slot cache refresh before liquidation
- Exploit idea: Audit distinctions like spot vs TWAP vs cache vs confidence-ignored pricing to ensure each value-moving path uses the intended conservative source. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch
- Expected Immunefi impact: High: over-liquidation, under-repayment, or protocol loss from misvaluation
- Fast validation: Exercise paths where price-type choice matters economically and assert the selected type matches protocol design for that exact action. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
