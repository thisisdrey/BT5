# Q1899: check_primary_oracle_key: integration price adapter accepts mismatched reserve/market context [a-pulse-call-with-omitted] [freshness]

## Question
Can an unprivileged attacker call `lending_pool_pulse_bank_price_cache` with a pulse call with omitted auxiliary accounts affecting the adapter branch so `check_primary_oracle_key` loads or refreshes pricing from a mismatched reserve/market context, violating `permissionless cache refresh must only ever bind the bank to its exact configured primary price source` and causing `High: exploitable cached mispricing that later enables theft or insolvency`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `check_primary_oracle_key`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a pulse call with omitted auxiliary accounts affecting the adapter branch
- Exploit idea: Probe helper functions that load external reserve/market state via remaining accounts and must bind them to the bank being mutated. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: permissionless cache refresh must only ever bind the bank to its exact configured primary price source
- Expected Immunefi impact: High: exploitable cached mispricing that later enables theft or insolvency
- Fast validation: Mix two similar external contexts and assert the pricing adapter cannot succeed unless the exact configured context is supplied. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
