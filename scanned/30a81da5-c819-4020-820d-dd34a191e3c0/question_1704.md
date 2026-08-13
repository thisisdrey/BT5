# Q1704: get_price_and_confidence_of_type: oracle account selection changes the priced object [a-state-where-confidence-freshness] [adapter-mismatch]

## Question
Can an unprivileged attacker supply a state where confidence/freshness is just inside or outside policy thresholds to `lending_account_liquidate` so `get_price_and_confidence_of_type` prices the wrong object or wrong price source while mutating state for another, violating `liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch` and causing `High: over-liquidation, under-repayment, or protocol loss from misvaluation`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_and_confidence_of_type`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a state where confidence/freshness is just inside or outside policy thresholds
- Exploit idea: Attack remaining-account binding, oracle-key checks, and asset-tag routing so price lookup cannot be redirected by caller-controlled account order. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch
- Expected Immunefi impact: High: over-liquidation, under-repayment, or protocol loss from misvaluation
- Fast validation: Provide multiple plausible oracle contexts and assert the path rejects unless the selected price source matches the validated bank configuration exactly. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
