# Q1827: check_primary_oracle_key: oracle account selection changes the priced object [a-bank-with-multiple-historical] [freshness]

## Question
Can an unprivileged attacker supply a bank with multiple historical or sibling oracle contexts available on-chain to `lending_pool_pulse_bank_price_cache` so `check_primary_oracle_key` prices the wrong object or wrong price source while mutating state for another, violating `permissionless cache refresh must only ever bind the bank to its exact configured primary price source` and causing `High: exploitable cached mispricing that later enables theft or insolvency`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `check_primary_oracle_key`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a bank with multiple historical or sibling oracle contexts available on-chain
- Exploit idea: Attack remaining-account binding, oracle-key checks, and asset-tag routing so price lookup cannot be redirected by caller-controlled account order. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: permissionless cache refresh must only ever bind the bank to its exact configured primary price source
- Expected Immunefi impact: High: exploitable cached mispricing that later enables theft or insolvency
- Fast validation: Provide multiple plausible oracle contexts and assert the path rejects unless the selected price source matches the validated bank configuration exactly. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
