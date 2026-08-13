# Q1709: get_price_and_confidence_of_type: oracle account selection changes the priced object [an-integration-backed-bank-with] [freshness]

## Question
Can an unprivileged attacker supply an integration-backed bank with multiple plausible reserve/market accounts to `lending_account_liquidate` so `get_price_and_confidence_of_type` prices the wrong object or wrong price source while mutating state for another, violating `liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch` and causing `High: over-liquidation, under-repayment, or protocol loss from misvaluation`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_and_confidence_of_type`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: an integration-backed bank with multiple plausible reserve/market accounts
- Exploit idea: Attack remaining-account binding, oracle-key checks, and asset-tag routing so price lookup cannot be redirected by caller-controlled account order. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch
- Expected Immunefi impact: High: over-liquidation, under-repayment, or protocol loss from misvaluation
- Fast validation: Provide multiple plausible oracle contexts and assert the path rejects unless the selected price source matches the validated bank configuration exactly. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
