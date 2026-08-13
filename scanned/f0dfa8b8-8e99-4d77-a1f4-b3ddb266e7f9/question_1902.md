# Q1902: check_primary_oracle_key: integration price adapter accepts mismatched reserve/market context [a-bank-using-a-derivative] [adapter-mismatch]

## Question
Can an unprivileged attacker call `lending_pool_pulse_bank_price_cache` with a bank using a derivative or integration-backed price adapter so `check_primary_oracle_key` loads or refreshes pricing from a mismatched reserve/market context, violating `permissionless cache refresh must only ever bind the bank to its exact configured primary price source` and causing `High: exploitable cached mispricing that later enables theft or insolvency`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `check_primary_oracle_key`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a bank using a derivative or integration-backed price adapter
- Exploit idea: Probe helper functions that load external reserve/market state via remaining accounts and must bind them to the bank being mutated. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: permissionless cache refresh must only ever bind the bank to its exact configured primary price source
- Expected Immunefi impact: High: exploitable cached mispricing that later enables theft or insolvency
- Fast validation: Mix two similar external contexts and assert the pricing adapter cannot succeed unless the exact configured context is supplied. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
