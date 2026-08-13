# Q1830: check_primary_oracle_key: oracle account selection changes the priced object [a-same-slot-pulse-followed] [adapter-mismatch]

## Question
Can an unprivileged attacker supply a same-slot pulse followed immediately by a borrow or withdraw path to `lending_pool_pulse_bank_price_cache` so `check_primary_oracle_key` prices the wrong object or wrong price source while mutating state for another, violating `permissionless cache refresh must only ever bind the bank to its exact configured primary price source` and causing `High: exploitable cached mispricing that later enables theft or insolvency`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `check_primary_oracle_key`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a same-slot pulse followed immediately by a borrow or withdraw path
- Exploit idea: Attack remaining-account binding, oracle-key checks, and asset-tag routing so price lookup cannot be redirected by caller-controlled account order. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: permissionless cache refresh must only ever bind the bank to its exact configured primary price source
- Expected Immunefi impact: High: exploitable cached mispricing that later enables theft or insolvency
- Fast validation: Provide multiple plausible oracle contexts and assert the path rejects unless the selected price source matches the validated bank configuration exactly. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
